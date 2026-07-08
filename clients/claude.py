from anthropic import Anthropic
from anthropic.types import AuthenticationError
import os
import logging
import json
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Usar variables de entorno para la API Key (más seguro)
CLAUDE_API_KEY = os.getenv('CLAUDE_API_KEY', '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')
CLAUDE_MODEL = os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-5')

# Validar que la API Key está configurada
if not CLAUDE_API_KEY:
    logger.warning("CLAUDE_API_KEY no está configurada. Usar variables de entorno.")


class Claude:
    """
    Cliente para interactuar con Claude API de Anthropic.
    Soporta generación de features, parsing de JSON y conversaciones multi-turno.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        """
        Inicializa el cliente Claude.
        
        Args:
            api_key: Clave de API de Anthropic (usa variable de entorno si no se proporciona)
            model: Modelo a usar (por defecto claude-3-5-sonnet-20241022)
            
        Raises:
            ValueError: Si no hay API key disponible
        """
        try:
            self.api_key = api_key or CLAUDE_API_KEY
            if not self.api_key:
                raise ValueError(
                    "CLAUDE_API_KEY no está configurada. "
                    "Configura la variable de entorno CLAUDE_API_KEY o pásala como parámetro."
                )
            
            self.client = Anthropic(api_key=self.api_key)
            self.model = model or CLAUDE_MODEL
            self.conversation_history = []
            self.retry_attempts = 5
            self.retry_delay = 3  # segundos
            
            logger.info(f"Cliente Claude inicializado con modelo: {self.model}")
        except Exception as e:
            logger.error(f"Error al inicializar cliente Claude: {str(e)}")
            raise

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
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=messages,
                    **kwargs
                )

                if not getattr(response, 'content', None) or len(response.content) == 0:
                    raise ValueError("Respuesta vacía de Claude")

                # response.content is expected to be a sequence with text attribute
                try:
                    content = response.content[0].text
                except Exception:
                    # Fallback: try to stringify the response
                    try:
                        content = str(response)
                    except Exception:
                        content = ""

                logger.debug(f"Respuesta recibida de Claude ({len(content)} chars)")
                return content

            except Exception as e:
                ename = e.__class__.__name__ if hasattr(e, '__class__') else str(type(e))
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
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Error después de {self.retry_attempts} intentos: {str(e)}")
                    raise RuntimeError(f"Error en solicitud a Claude: {str(e)}")

    def generate_feature(self, prompt: str, max_tokens: int = 2048) -> str:
        """
        Genera un feature Gherkin a partir de un prompt.
        
        Args:
            prompt: Prompt descriptivo para generar el feature
            max_tokens: Máximo de tokens en la respuesta
            
        Returns:
            str: Feature generado en formato Gherkin
        """
        try:
            logger.info("Generando feature Gherkin...")
            messages = [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
            
            response = self._make_request(messages, max_tokens=max_tokens)
            logger.info("Feature generado exitosamente")
            return response
            
        except Exception as e:
            logger.error(f"Error al generar feature: {str(e)}")
            raise

    def generate_json(self, prompt: str, max_tokens: int = 6000) -> Dict[str, Any]:
        """
        Genera una respuesta JSON estructurada.
        
        Args:
            prompt: Prompt que solicita respuesta JSON
            max_tokens: Máximo de tokens en la respuesta
            
        Returns:
            dict: Respuesta parseada como JSON
            
        Raises:
            ValueError: Si la respuesta no es JSON válido
        """
        try:
            logger.info("Generando respuesta JSON estructurada...")
            messages = [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
            
            response = self._make_request(messages, max_tokens=max_tokens, temperature=0.1)
            response.strip()
            # Intentar parsear JSON
            try:
                result = json.loads(response)
                logger.info("JSON parseado exitosamente")
                return result
            except json.JSONDecodeError:
                # Si no es JSON puro, intentar extraer JSON del texto
                logger.warning("Intentando extraer JSON de respuesta no estructurada")
                import re
                json_match = re.search(r'[{\[].*[}\]]', response, re.DOTALL)
                if json_match:
                    try:
                        result = json.loads(json_match.group())
                        logger.info("JSON extraído y parseado exitosamente")
                        return result
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON extraído no válido: {str(e)}")
                        raise ValueError(f"No se pudo parsear JSON: {str(e)}")
                else:
                    raise ValueError("No se encontró JSON en la respuesta")
                    
        except Exception as e:
            logger.error(f"Error al generar JSON: {str(e)}")
            raise

    def chat(self, message: str, max_tokens: int = 2048, clear_history: bool = False) -> str:
        """
        Mantiene una conversación multi-turno con Claude.
        
        Args:
            message: Mensaje del usuario
            max_tokens: Máximo de tokens en la respuesta
            clear_history: Si True, limpia el historial de conversación
            
        Returns:
            str: Respuesta de Claude
        """
        try:
            if clear_history:
                self.conversation_history = []
                logger.info("Historial de conversación limpiado")
            
            # Agregar mensaje del usuario al historial
            self.conversation_history.append({
                "role": "user",
                "content": message
            })
            
            logger.info(f"Enviando mensaje a Claude (historial: {len(self.conversation_history)} mensajes)")
            response = self._make_request(self.conversation_history, max_tokens=max_tokens)
            
            # Agregar respuesta de Claude al historial
            self.conversation_history.append({
                "role": "assistant",
                "content": response
            })
            
            return response
            
        except Exception as e:
            logger.error(f"Error en conversación: {str(e)}")
            raise

    def clear_conversation(self):
        """Limpia el historial de conversación."""
        self.conversation_history = []
        logger.info("Historial de conversación limpiado")

    def set_retry_params(self, attempts: int = 3, delay: float = 1.0):
        """
        Configura parámetros de reintentos.
        
        Args:
            attempts: Número de intentos
            delay: Delay inicial en segundos
        """
        self.retry_attempts = attempts
        self.retry_delay = delay
        logger.info(f"Parámetros de reintento: {attempts} intentos, {delay}s delay")


