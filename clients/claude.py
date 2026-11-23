from anthropic import Anthropic
from clients.efficiency import configured_model, RATES, record_attempt, usage_record
import os
import logging
import json
import re
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Usar variables de entorno para la API Key (más seguro)
CLAUDE_API_KEY=os.getenv('CLAUDE_API_KEY', '')

# Validar que la API Key está configurada
if not CLAUDE_API_KEY:
    logger.warning("CLAUDE_API_KEY no está configurada. Usar variables de entorno.")


class Claude:
    """Cliente Claude: petición a Anthropic y tratamiento de sus respuestas."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, role: str = "Claude"):
        """
        Inicializa el cliente Claude.
        
        Args:
            api_key: Clave de API de Anthropic (usa variable de entorno si no se proporciona)
            model: Modelo a usar (por defecto claude-sonnet-5-5)
            
        Raises:
            ValueError: Si no hay API key disponible
        """
        try:
            self.api_key = api_key or os.getenv('CLAUDE_API_KEY', '')
            if not self.api_key:
                raise ValueError(
                    "CLAUDE_API_KEY no está configurada. "
                    "Configura la variable de entorno CLAUDE_API_KEY o pásala como parámetro."
                )
            
            # Los reintentos se gestionan aquí, no dentro del SDK, para contarlos todos.
            self.client = Anthropic(api_key=self.api_key, max_retries=0)
            self.model = model or configured_model()
            if self.model not in RATES:
                raise ValueError("Sólo se admite Claude Sonnet 5 o 5.5")
            self.role = role
            self.conversation_history = []
            self.usage_records = []
            self.retry_attempts = 5
            self.retry_delay = 3  # segundos
            
            logger.info(f"Cliente Claude inicializado con modelo: {self.model}")
        except Exception as e:
            logger.error(f"Error al inicializar cliente Claude: {str(e)}")
            raise

    @staticmethod
    def parse_json_object(text: str) -> dict:
        """Extrae un objeto JSON de una respuesta, incluso con marcas Markdown."""
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            obj = None
            for i, character in enumerate(cleaned):
                if character != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(cleaned[i:])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    obj = candidate
                    break
        if not isinstance(obj, dict):
            raise ValueError("El LLM no devolvió un objeto JSON completo válido")
        return obj

    def generate_json(self, prompt: str, max_tokens: int = 6000) -> dict:
        """Genera y procesa el JSON de artefactos con Claude."""
        limit = int(os.getenv("LLM_MAX_GENERATION_TOKENS", str(max_tokens)))
        logger.info("Generando JSON con %s (límite: %s tokens)", self.model, limit)
        raw = self._request(prompt, max_tokens=limit, json_mode=True)
        return self.parse_json_object(raw)

    def generate_feature(self, prompt: str, max_tokens: int = 6000) -> str:
        """Genera una respuesta en formato texto con Claude."""
        limit = int(os.getenv("LLM_MAX_VALIDATION_TOKENS", str(max_tokens)))
        logger.info("Generando texto con %s (límite: %s tokens)", self.model, limit)
        return self._request(prompt, max_tokens=limit, json_mode=False)

    def _request(self, prompt: str, max_tokens: int, json_mode: bool = False) -> str:
        """Prepara los mensajes para la API de Anthropic."""
        messages = [{"role": "user", "content": prompt}]
        temperature = 0.1 if json_mode else 0.7
        return self._make_request(messages, max_tokens=max_tokens, temperature=temperature)

    def _make_request(self, messages: list, max_tokens: int = 2048, 
                     temperature: float = 0.7, **kwargs) -> str:
        """
        Realiza una solicitud a Claude con retry logic.
        
        Args:
            messages: Lista de mensajes para la conversación
            max_tokens: Máximo de tokens en la respuesta
            temperature: Control de creatividad (0.0-1.0)
            **kwargs: Argumentos adicionales para la API
            
        Returns:
            str: Contenido de la respuesta
            
        Raises:
            RuntimeError: Si la solicitud falla después de reintentos
        """
        for attempt in range(self.retry_attempts):
            usage = None
            status = "error"
            error_type = None
            start = time.perf_counter()
            try:
                request_params = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": messages,
                    **kwargs,
                }
                # Claude Sonnet 5.5 utiliza adaptive thinking por defecto y
                # rechaza temperature/top_p/top_k no predeterminados (HTTP 400).
                if self.model != "claude-sonnet-5-5":
                    request_params["temperature"] = temperature
                response = self.client.messages.create(**request_params)
                usage = getattr(response, 'usage', None)

                # La respuesta de modelos con pensamiento puede contener varios
                # bloques (incluidos bloques de thinking). Unir SÓLO los de texto.
                content = "\n".join(
                    block.text for block in (getattr(response, "content", None) or [])
                    if getattr(block, "type", None) == "text" and getattr(block, "text", "")
                ).strip()
                if not content:
                    raise ValueError("Claude no devolvió texto: comprueba max_tokens y las opciones de razonamiento")

                if getattr(response, "stop_reason", "") == "max_tokens":
                    raise ValueError("La respuesta de Claude llegó al límite de tokens; aumenta max_tokens.")
                logger.debug(f"Respuesta recibida de Claude ({len(content)} chars)")
                status = "ok"
                return content

            except ValueError as e:
                # Respuesta incompleta o truncada: reintentar sin modificar el límite no ayuda.
                error_type = type(e).__name__
                raise
            except Exception as e:
                ename = e.__class__.__name__ if hasattr(e, '__class__') else str(type(e))
                error_type = ename
                logger.error(f"Error en petición a Claude ({ename}): {str(e)}")
                # Si el nombre de la excepción indica un problema de autenticación, devolver mensaje claro
                if 'AuthenticationError' in ename or 'AuthenticationError' in str(e):
                    raise RuntimeError(f"Error de autenticación: {str(e)}")
                # Errores 400 (créditos, permisos, request inválido): no reintentar
                if '400' in str(e) or 'invalid_request_error' in str(e) or 'credit balance' in str(e).lower():
                    raise RuntimeError(f"Error en solicitud a Claude (no reintentable): {str(e)}")

                # Si aún quedan intentos, esperar e intentar de nuevo
                if attempt < self.retry_attempts - 1:
                    # Errores 500/529 (servidor sobrecargado): esperar más antes de reintentar
                    is_server_error = "500" in str(e) or "529" in str(e) or "overloaded" in str(e).lower() or "internal server" in str(e).lower()
                    base = 10 if is_server_error else self.retry_delay
                    wait_time = base * (2 ** attempt)
                    logger.warning(f"Intento {attempt + 1}/{self.retry_attempts} falló. Reintentando en {wait_time}s: {str(e)}")
                    # El backoff forma parte del tiempo del proceso, no de la petición API.
                    delay = wait_time
                else:
                    logger.error(f"Error después de {self.retry_attempts} intentos: {str(e)}")
                    raise RuntimeError(f"Error en solicitud a Claude: {str(e)}")
            finally:
                record = {"model": self.model, "role": self.role, "attempt": attempt + 1,
                          "status": status, "error": error_type, "seconds": time.perf_counter()-start,
                          **usage_record(self.model, usage)}
                self.usage_records.append(record)
                record_attempt(record)
            time.sleep(delay)

