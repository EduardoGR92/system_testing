from clients.claude import Claude
from clients.swagger import SwaggerReader
import re
import logging
import json
from pathlib import Path
from typing import Dict, Any, List

URL_SWAGGER = 'http://127.0.0.1:8000/openapi.json'

logger = logging.getLogger(__name__)


class AgentValidator:
    """
    Agente validador que verifica la calidad de los features y steps generados.
    """

    def __init__(self, yaml_data: dict = None):
        """
        Inicializa el validador con Swagger y el modelo Claude.

        Args:
            yaml_data: Si se pasa, el validador usa este esquema OpenAPI ya cargado
                       (flujo YAML) en lugar de llamar al live swagger.
        
        Raises:
            Exception: Si hay error al conectar con Swagger o Claude
        """
        try:
            logger.info("Inicializando AgentValidator...")
            self._yaml_data = yaml_data
            self.swagger = None if yaml_data else SwaggerReader(URL_SWAGGER)
            self.model = Claude()
            logger.info("AgentValidator inicializado correctamente")
        except Exception as e:
            logger.error(f"Error al inicializar AgentValidator: {str(e)}")
            raise

    def _get_endpoints_normalizados(self) -> list:
        """Devuelve endpoints normalizados desde yaml_data (YAML flow) o desde live swagger (HU flow)."""
        if self._yaml_data is not None:
            return SwaggerReader.normalizar_endpoints(self._yaml_data)
        return self.swagger.get_endpoints_normalizados()

    @staticmethod
    def _val_sintactica(feature_content: str, steps_content: str) -> Dict[str, Any]:
        """Validacion 1: sintaxis/estructura Gherkin del feature."""
        errors: List[str] = []
        warnings: List[str] = []

        if not feature_content.strip().startswith("Feature:"):
            errors.append("El feature no empieza por 'Feature:'")

        scenario_count = len(re.findall(r"^\s*Scenario(?: Outline)?:", feature_content, flags=re.MULTILINE))
        if scenario_count == 0:
            errors.append("No hay escenarios Gherkin")

        for keyword in ["Given", "When", "Then"]:
            if not re.search(rf"^\s*{keyword}\b", feature_content, flags=re.MULTILINE):
                errors.append(f"Falta al menos un step {keyword}")

        if "from behave import" not in steps_content:
            warnings.append("steps.py no importa behave (opcional si usa otra librería)")

        return {
            "valid": len(errors) == 0,
            "scenario_count": scenario_count,
            "errors": errors,
            "warnings": warnings,
        }

    @staticmethod
    def _decoradores_de_steps(steps_content: str) -> list:
        """Devuelve patrones regex compilados a partir de los decoradores de steps.py."""
        patrones = []
        # Captura el texto del decorador admitiendo comillas escapadas (\' o \") dentro
        patron_dec = r"@(?:given|when|then|step)\(\s*(['\"])((?:\\.|(?!\1).)*)\1"
        for m in re.finditer(patron_dec, steps_content, flags=re.IGNORECASE | re.DOTALL):
            texto = m.group(2)
            # Desescapar comillas internas (\' -> ', \" -> ")
            texto = re.sub(r"\\(['\"])", r"\1", texto)
            # Convertir el texto del decorador a regex: literal salvo los parametros {x} / {x:tipo}
            regex = re.escape(texto)
            regex = re.sub(r"\\\{[^}]*\\\}", ".+?", regex)
            try:
                patrones.append(re.compile("^" + regex + "$"))
            except re.error:
                continue
        return patrones

    @staticmethod
    def _steps_de_feature(feature_content: str) -> list:
        """Extrae el texto de cada step (sin la keyword) de los escenarios del feature."""
        steps = []
        for linea in feature_content.splitlines():
            m = re.match(r"^\s*(Given|When|Then|And|But)\s+(.*\S)\s*$", linea)
            if m:
                steps.append(m.group(2).strip())
        return steps

    def _val_cohesion(self, feature_content: str, steps_content: str) -> Dict[str, Any]:
        """Validacion 2: cohesion entre cada step del escenario y su definicion en steps.py."""
        errors: List[str] = []
        warnings: List[str] = []

        patrones = self._decoradores_de_steps(steps_content)
        for decorator in ["@given", "@when", "@then"]:
            if decorator not in steps_content:
                warnings.append(f"No se encuentra ningun decorador {decorator} en steps.py")

        sin_definir = []
        for texto in self._steps_de_feature(feature_content):
            if not any(p.match(texto) for p in patrones) and texto not in sin_definir:
                sin_definir.append(texto)
        for t in sin_definir:
            errors.append(f"Step sin definicion (undefined) en steps.py: '{t}'")

        return {
            "valid": len(errors) == 0,
            "steps_sin_definir": sin_definir,
            "errors": errors,
            "warnings": warnings,
        }

    def _val_tecnica(self, scenarios_generated: Dict[str, Any]) -> Dict[str, Any]:
        """Validacion 3: coherencia tecnica de los endpoints usados con el contrato OpenAPI."""
        errors: List[str] = []
        warnings: List[str] = []
        try:
            used_endpoints = scenarios_generated.get("used_endpoints", [])
            contract_raw = scenarios_generated.get("contract_endpoints")
            if contract_raw is not None:
                contract_list = contract_raw
            else:
                contract_list = self.swagger.get_endpoints_normalizados()

            openapi_endpoints = {(e.get("method", "").upper(), e.get("path", ""))
                                 for e in contract_list}

            # Mapa (method, path) -> {fields, required, response_fields} del contrato
            contract_fields_map: Dict[str, Any] = {}
            for ep in contract_list:
                key = (ep.get("method", "").upper(), ep.get("path", ""))
                rt = ep.get("request_template") or {}
                fields = rt.get("fields")
                if fields:
                    # Recopilar response_fields de respuestas 2xx
                    resp_fields: Dict[str, Any] = {}
                    for code, resp in (ep.get("responses") or {}).items():
                        if str(code).startswith("2"):
                            resp_fields.update(resp.get("response_fields") or {})
                    contract_fields_map[key] = {
                        "fields": set(fields.keys()),
                        "required": set(rt.get("required") or []),
                        "response_fields": set(resp_fields.keys()),
                    }

            for endpoint in used_endpoints:
                pair = (endpoint.get("method", "").upper(), endpoint.get("path", ""))
                if pair not in openapi_endpoints:
                    errors.append(f"Endpoint no presente en OpenAPI: {pair}")

            # Validar campos de las templates JSON contra el contrato
            feature_content = scenarios_generated.get("feature_content", "")
            self._val_template_fields(feature_content, contract_fields_map, errors, warnings)
            # Validar coherencia de valores entre template y Examples (campos echo)
            self._val_coherencia_template_examples(feature_content, contract_fields_map, errors)
            # Validar que expected_error del Examples sea substring de mensajes reales del contrato
            self._val_expected_error_vs_contrato(feature_content, contract_list, errors)
            # Validar que expected_status de los Examples existan en el contrato para ese endpoint
            self._val_status_codes_vs_contrato(feature_content, contract_list, errors, warnings)

        except Exception as e:
            logger.warning(f"No se pudo validar endpoints contra el contrato: {str(e)}")
            warnings.append(f"No se pudo validar endpoints contra el contrato: {str(e)}")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }

    @staticmethod
    def _val_template_fields(
        feature_content: str,
        contract_fields_map: Dict[str, Any],
        errors: List[str],
        warnings: List[str],
    ) -> None:
        """Comprueba que los campos de cada template JSON no incluyan campos ajenos al contrato."""
        import json as _json
        from pathlib import Path as _Path

        # Parsear tablas Examples para obtener mapeo template -> (method, path)
        template_endpoint: Dict[str, tuple] = {}
        headers: List[str] = []
        for line in feature_content.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                headers = []
                continue
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if not cells:
                continue
            lower = [c.lower() for c in cells]
            if "template" in lower and ("method" in lower or "endpoint" in lower):
                headers = lower
                continue
            if headers and "template" in headers:
                t_idx = headers.index("template")
                m_idx = headers.index("method") if "method" in headers else None
                e_idx = headers.index("endpoint") if "endpoint" in headers else None
                if t_idx < len(cells):
                    tname = cells[t_idx]
                    method = cells[m_idx].upper() if m_idx is not None and m_idx < len(cells) else None
                    path = cells[e_idx] if e_idx is not None and e_idx < len(cells) else None
                    if tname and method and path:
                        template_endpoint[tname] = (method, path)

        if not template_endpoint:
            return

        templates_root = _Path("tests/templates")
        for tname, (method, path) in template_endpoint.items():
            matches = list(templates_root.glob(f"**/{tname}.json"))
            if not matches:
                continue
            try:
                template_data = _json.loads(matches[0].read_text(encoding="utf-8"))
            except Exception:
                continue

            contract_info = contract_fields_map.get((method, path))
            if not contract_info:
                continue

            contract_field_names = contract_info["fields"]
            template_field_names = set(template_data.keys())
            extra = template_field_names - contract_field_names
            if extra:
                errors.append(
                    f"Template '{tname}' ({method} {path}): campos no definidos en el contrato: "
                    + ", ".join(sorted(extra))
                )

    @staticmethod
    def _val_coherencia_template_examples(
        feature_content: str,
        contract_fields_map: Dict[str, Any],
        errors: List[str],
    ) -> None:
        """Verifica que los valores de campos echo en templates coincidan con expected en Examples.
        Un campo echo es aquel que aparece tanto en el request como en el response (la API lo devuelve tal cual).
        """
        import json as _json
        from pathlib import Path as _Path

        NON_ECHO_COLS = {"template", "method", "endpoint", "status_code", "error_message",
                         "campo_faltante", "status", "expected_status"}
        templates_root = _Path("tests/templates")
        headers: List[str] = []
        current_method: str = ""
        current_path: str = ""

        for line in feature_content.splitlines():
            stripped = line.strip()
            # Extraer method/path del step When
            when_m = re.search(r"When\b.+'(GET|POST|PUT|DELETE|PATCH)'.+'/([^']+)'", stripped, re.IGNORECASE)
            if when_m:
                current_method = when_m.group(1).upper()
                current_path = "/" + when_m.group(2).lstrip("/")

            if not stripped.startswith("|"):
                headers = []
                continue

            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if not cells:
                continue
            lower_cells = [c.lower() for c in cells]

            if "template" in lower_cells:
                headers = lower_cells
                continue

            if not headers or "template" not in headers:
                continue

            t_idx = headers.index("template")
            if t_idx >= len(cells):
                continue
            tname = cells[t_idx]

            # Method y path desde la fila o desde el contexto del When
            m_idx = headers.index("method") if "method" in headers else None
            e_idx = headers.index("endpoint") if "endpoint" in headers else None
            row_method = (cells[m_idx].upper() if m_idx is not None and m_idx < len(cells) else current_method)
            row_path = (cells[e_idx] if e_idx is not None and e_idx < len(cells) else current_path)
            if not row_method or not row_path:
                continue

            contract_info = contract_fields_map.get((row_method, row_path), {})
            if not contract_info:
                continue
            req_fields: set = contract_info.get("fields", set())
            resp_fields: set = contract_info.get("response_fields", set())
            echo_fields: set = req_fields & resp_fields  # campos que la API devuelve tal cual recibe

            if not echo_fields:
                continue

            # Cargar template
            matches = list(templates_root.glob(f"**/{tname}.json"))
            if not matches:
                continue
            try:
                template_body = _json.loads(matches[0].read_text(encoding="utf-8"))
            except Exception:
                continue

            # Extraer expected values del row (formato field_X/expected_X y formato clásico)
            expected_values: Dict[str, str] = {}
            i = 0
            while i < len(headers):
                h = headers[i]
                if h.startswith("field_") and i + 1 < len(headers) and headers[i + 1].startswith("expected_"):
                    field_name = cells[i] if i < len(cells) else ""
                    expected_val = cells[i + 1] if i + 1 < len(cells) else ""
                    if field_name:
                        expected_values[field_name.lower()] = expected_val
                    i += 2
                else:
                    if h not in NON_ECHO_COLS and i < len(cells):
                        expected_values[h] = cells[i]
                    i += 1

            # Comparar echo fields
            for field in echo_fields:
                if field not in expected_values or field not in template_body:
                    continue
                expected_val = expected_values[field]
                template_val = template_body[field]
                try:
                    match = float(str(template_val)) == float(expected_val)
                except (ValueError, TypeError):
                    match = str(template_val).strip() == str(expected_val).strip()
                if not match:
                    errors.append(
                        f"COHERENCIA TEMPLATE/EXAMPLES — template '{tname}': "
                        f"campo echo '{field}' = '{template_val}' en el template, "
                        f"pero el Examples espera '{expected_val}'. "
                        f"La API devuelve el valor enviado → el test fallará."
                    )

    @staticmethod
    def _val_expected_error_vs_contrato(
        feature_content: str,
        contract_list: list,
        errors: List[str],
    ) -> None:
        """Detecta expected_error que son mensajes genéricos de frameworks (FastAPI/HTTP)
        en lugar de mensajes específicos del dominio de negocio de la API.
        Los ejemplos del contrato pueden estar incompletos, así que NO bloqueamos
        mensajes que no aparezcan en ellos — solo bloqueamos mensajes claramente
        inventados/genéricos que el generador NO debería haber puesto."""

        # Patrones de mensajes genéricos de frameworks/HTTP que indican mensaje inventado
        GENERIC_PATTERNS = [
            "field required",
            "value is not a valid",
            "invalid value",
            "bad request",
            "validation error",
            "unprocessable entity",
            "type_error",
            "value_error",
            "none is not an allowed value",
        ]

        # Parsear columna expected_error del feature
        headers: List[str] = []
        for line in feature_content.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                headers = []
                continue
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            lower_cells = [c.lower() for c in cells]
            if "expected_error" in lower_cells and "template" in lower_cells:
                headers = lower_cells
                continue
            if not headers or "expected_error" not in headers:
                continue
            e_idx = headers.index("expected_error")
            if e_idx >= len(cells):
                continue
            expected_error = cells[e_idx].strip().lower()
            if not expected_error or expected_error == "expected_error":
                continue
            # Bloquear solo si coincide con un patrón genérico de framework
            for pattern in GENERIC_PATTERNS:
                if pattern in expected_error:
                    errors.append(
                        f"MENSAJE DE ERROR GENERICO — expected_error='{cells[e_idx]}' parece "
                        f"un mensaje de framework (FastAPI/HTTP) en lugar del mensaje de negocio "
                        f"real de la API. Usa el mensaje exacto del contrato, no mensajes "
                        f"genéricos como '{pattern}'."
                    )
                    break

    @staticmethod
    def _rutas_coinciden(contrato: str, feature: str) -> bool:
        """True si 'feature' coincide con 'contrato', tratando {param} como comodín de segmento."""
        import re as _re
        patron = _re.sub(r"\{[^}]+\}", r"[^/]+", _re.escape(contrato))
        return bool(_re.fullmatch(patron, feature))

    @staticmethod
    def _val_status_codes_vs_contrato(
        feature_content: str,
        contract_list: list,
        errors: List[str],
        warnings: List[str],
    ) -> None:
        """Valida que los expected_status de los Examples existan en el contrato para ese endpoint."""
        import re as _re

        # Construir mapa: (method, path_contrato) -> set de códigos
        contract_map: List[tuple] = []
        for ep in contract_list:
            method = ep.get("method", "").upper()
            path = ep.get("path", "")
            codes = {str(c) for c in (ep.get("responses") or {}).keys()}
            contract_map.append((method, path, codes))

        in_examples = False
        headers: List[str] = []
        method_col = endpoint_col = status_col = -1

        for line in feature_content.splitlines():
            stripped = line.strip()
            if _re.match(r"Examples:\s*$", stripped, _re.IGNORECASE):
                in_examples = True
                headers = []
                method_col = endpoint_col = status_col = -1
                continue
            if not stripped.startswith("|"):
                if in_examples:
                    in_examples = False
                continue
            if not in_examples:
                continue

            # Separar celdas (ignorar primera y última celda vacías del |...|)
            raw = stripped.strip("|")
            cells = [c.strip() for c in raw.split("|")]

            if not headers:
                headers = [h.lower() for h in cells]
                method_col   = next((i for i, h in enumerate(headers) if h == "method"),   -1)
                endpoint_col = next((i for i, h in enumerate(headers) if h == "endpoint"), -1)
                status_col   = next((i for i, h in enumerate(headers) if h == "expected_status"), -1)
                continue

            if method_col < 0 or endpoint_col < 0 or status_col < 0:
                continue
            if len(cells) <= max(method_col, endpoint_col, status_col):
                continue

            method   = cells[method_col].upper()
            endpoint = cells[endpoint_col]
            try:
                status = str(int(cells[status_col]))
            except ValueError:
                continue

            # Buscar en el contrato
            matched_codes = None
            for m, p, codes in contract_map:
                if m == method and AgentValidator._rutas_coinciden(p, endpoint):
                    matched_codes = codes
                    break

            if matched_codes is not None and status not in matched_codes:
                errors.append(
                    f"HTTP {status} no documentado en el contrato para {method} {endpoint} "
                    f"(codigos validos: {', '.join(sorted(matched_codes))}). "
                    f"Elimina o corrige ese escenario para que use un codigo real del contrato."
                )

    def static_validate(self, scenarios_generated: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validación estática: agrupa la validacion sintactica, de cohesion y tecnica.

        Args:
            scenarios_generated: Diccionario con feature_content, steps_content, used_endpoints

        Returns:
            dict: Resultado combinado con errores, warnings, conteos y el detalle por categoria
        """
        try:
            logger.info("Ejecutando validación estática (sintactica + cohesion + tecnica)...")
            feature_content = scenarios_generated.get("feature_content", "")
            steps_content = scenarios_generated.get("steps_content", "")

            sintactica = self._val_sintactica(feature_content, steps_content)
            cohesion = self._val_cohesion(feature_content, steps_content)
            tecnica = self._val_tecnica(scenarios_generated)

            errors = sintactica["errors"] + cohesion["errors"] + tecnica["errors"]
            warnings = sintactica["warnings"] + cohesion["warnings"] + tecnica["warnings"]

            result = {
                "valid": len(errors) == 0,
                "scenario_count": sintactica["scenario_count"],
                "errors": errors,
                "warnings": warnings,
                "categorias": {
                    "sintactica": sintactica,
                    "cohesion": cohesion,
                    "tecnica": tecnica,
                },
            }
            logger.info(
                f"Validación estática: {sintactica['scenario_count']} escenarios, "
                f"{len(errors)} errores ({len(cohesion['steps_sin_definir'])} steps sin definir)"
            )
            return result
        except Exception as e:
            logger.error(f"Error en validación estática: {str(e)}")
            return {
                "valid": False,
                "scenario_count": 0,
                "errors": [f"Error en validación estática: {str(e)}"],
                "warnings": [],
                "categorias": {},
            }

    def _resumen_codigos_contrato(self) -> str:
        """Devuelve un resumen 'METODO path -> codigos' a partir del contrato OpenAPI."""
        try:
            lineas = []
            for ep in self._get_endpoints_normalizados():
                metodo = ep.get("method", "").upper()
                path = ep.get("path", "")
                codigos = list((ep.get("responses", {}) or {}).keys())
                if metodo and path:
                    lineas.append(f"{metodo} {path} -> {', '.join(codigos) if codigos else '(sin codigos definidos)'}")
            return "\n".join(lineas) if lineas else "(contrato no disponible)"
        except Exception as exc:
            logger.warning(f"No se pudo extraer codigos del contrato: {exc}")
            return "(contrato no disponible)"

    def _resumen_response_fields(self) -> str:
        """Devuelve un resumen 'METODO path [codigo] -> campos' a partir del contrato OpenAPI normalizado."""
        try:
            endpoints = self._get_endpoints_normalizados()
            lineas = []
            for ep in endpoints:
                metodo = ep.get("method", "").upper()
                path = ep.get("path", "")
                for codigo, resp in (ep.get("responses", {}) or {}).items():
                    campos = resp.get("response_fields", {})
                    if campos:
                        lineas.append(f"{metodo} {path} [{codigo}] -> campos: {list(campos.keys())}")
                    else:
                        example = resp.get("example", {})
                        if example:
                            lineas.append(f"{metodo} {path} [{codigo}] -> campos (ejemplo): {list(example.keys())}")
            return "\n".join(lineas) if lineas else "(response_fields no disponibles)"
        except Exception as exc:
            logger.warning(f"No se pudo extraer response_fields del contrato: {exc}")
            return "(response_fields no disponibles)"

    def _resumen_error_ejemplos(self) -> str:
        """Devuelve la estructura JSON real de las respuestas de error (4xx/5xx) del contrato."""
        try:
            endpoints = self._get_endpoints_normalizados()
            lineas = []
            for ep in endpoints:
                metodo = ep.get("method", "").upper()
                path = ep.get("path", "")
                for codigo, resp in (ep.get("responses", {}) or {}).items():
                    if str(codigo).startswith(("4", "5")):
                        example = resp.get("example")
                        if example:
                            lineas.append(f"{metodo} {path} [{codigo}] ejemplo: {json.dumps(example, ensure_ascii=False)}")
            return "\n".join(lineas) if lineas else "(sin ejemplos de error en el contrato)"
        except Exception as exc:
            logger.warning(f"No se pudo extraer ejemplos de error: {exc}")
            return "(sin ejemplos de error en el contrato)"

    @staticmethod
    def _cargar_templates_del_feature(feature_content: str) -> str:
        """Extrae templates agrupados por escenario para evitar cruces de datos entre escenarios."""
        import re as _re
        import json as _json
        templates_root = Path("tests/templates")

        COLUMNAS_NO_TEMPLATE = {
            "method", "endpoint", "expected_status", "expected_error", "expected_status_value",
            "expected_reason", "expected_amount", "expected_currency", "expected_card_id",
            "expected_status_field", "field_status", "field_amount", "field_currency",
            "field_card_id", "field1", "field2", "field3", "field4", "field5",
            "expected_value1", "expected_value2", "expected_value3", "expected_value4",
            "expected_value5", "expected_error_message", "template",
        }
        VALORES_EXCLUIDOS = {"POST", "GET", "PUT", "DELETE", "PATCH"}

        secciones = []
        bloques = _re.split(r'\n(?=\s*Scenario)', feature_content)
        for bloque in bloques:
            title_m = _re.search(r'Scenario(?:\s+Outline)?:\s*(.+)', bloque)
            if not title_m:
                continue
            title = title_m.group(1).strip()
            example_m = _re.search(r'Examples:\s*\n((?:[ \t]*\|.+\n?)+)', bloque)
            if not example_m:
                continue
            rows = _re.findall(r'\|([^|\n]+(?:\|[^|\n]+)*)\|', example_m.group(1))
            if len(rows) < 2:
                continue
            headers = [h.strip() for h in rows[0].split('|')]
            template_nombres: set = set()
            for data_row in rows[1:]:
                values = [v.strip() for v in data_row.split('|')]
                for header, value in zip(headers, values):
                    if not value or not header:
                        continue
                    if header.strip() in COLUMNAS_NO_TEMPLATE:
                        continue
                    if value in VALORES_EXCLUIDOS or value.startswith('/') or value[0:1].isdigit():
                        continue
                    if '_' in value:
                        template_nombres.add(value)
            if not template_nombres:
                continue
            infos = []
            for nombre in sorted(template_nombres):
                matches = list(templates_root.glob(f"**/{nombre}.json"))
                if not matches:
                    continue
                try:
                    body = _json.loads(matches[0].read_text(encoding="utf-8"))
                    infos.append(f"  {nombre}.json -> {_json.dumps(body, ensure_ascii=False)}")
                except Exception:
                    pass
            if infos:
                secciones.append(f"[Escenario: {title}]\n" + "\n".join(infos))
        return "\n\n".join(secciones) if secciones else "(sin templates encontrados)"

    def semantic_validate(self, generated: Dict[str, Any], hu: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validación semántica usando Claude.
        Verifica cobertura, coherencia, y alineación con la HU.
        
        Args:
            generated: Diccionario con feature_content y steps_content
            hu: Diccionario con información de la Historia de Usuario
            
        Returns:
            dict: Resultado de validación semántica
        """
        try:
            _ref = hu.get('ref', 'unknown')
            logger.info(f"Ejecutando validación semántica para {_ref}...")
            contrato_codigos = self._resumen_codigos_contrato()
            contrato_response_fields = self._resumen_response_fields()
            contrato_error_ejemplos = self._resumen_error_ejemplos()
            feature_content = generated.get('feature_content', '')
            templates_reales = self._cargar_templates_del_feature(feature_content)
            prompt = f"""Eres un agente validador BDD experto en QA de APIs.
Valida si el feature y los steps cubren correctamente la HU.
Devuelve SOLO JSON válido sin markdown, sin texto adicional.

Analiza y puntua especificamente:
1. COBERTURA DE CRITERIOS: cada criterio de aceptacion de la HU debe estar cubierto por
   al menos un escenario. Indica criterios sin cubrir y escenarios que falten (casos
   positivos y negativos: datos validos, datos invalidos, campos obligatorios ausentes).
2. COHERENCIA SEMANTICA: los Given/When/Then deben corresponder a la logica de negocio
   de la HU; los nombres de escenario deben ser descriptivos y claros.
3. ESCENARIOS INVENTADOS: marca escenarios sin relacion con la HU o redundantes.
4. CODIGOS HTTP Y ASSERTS: los codigos de respuesta esperados deben coincidir con los que
   DEFINE EL CONTRATO OpenAPI (ver 'CODIGOS DEL CONTRATO' mas abajo). El contrato es la
   UNICA fuente de verdad: si el contrato define 200 para una operacion, el escenario que
   espera 200 es CORRECTO y NO debes penalizarlo ni sugerir 201 u otro codigo por "buenas
   practicas REST". Solo marca un codigo como incorrecto si NO aparece entre los codigos
   que el contrato define para ese endpoint/metodo. Los Then deben comprobar tanto el
   status code como los campos relevantes de la respuesta.
5. ESTRUCTURA BDD: cada escenario tiene Given/When/Then completos y usa steps coherentes.
6. IMPLEMENTACION DE STEPS: revisa el codigo Python de los steps (campo 'Steps generados').
   IMPORTANTE: los problemas de implementacion de steps.py NO se pueden corregir regenerando
   el feature — reportalos SIEMPRE como 'warnings' (nunca como 'errors'), para que no bloqueen
   el score ni provoquen bucles de regeneracion infinita.
   CRITICO: los 'warnings' son SOLO para problemas reales. ESTA PROHIBIDO añadir un warning
   para confirmar que algo esta correcto (ej: 'el step X ya maneja Y correctamente', 'esto es
   adecuado', 'es correcto'). Si algo esta bien, NO lo menciones en warnings. Solo reporta
   problemas reales que necesiten atencion.
   Detecta y reporta como WARNING:
   a) PATH DE ERROR INCORRECTO: el step accede a response_data['error'] sin buscar tambien
      en response_data['detail']['error']. Debe ser robusto a ambas estructuras.
   b) COMPARACION CASE-SENSITIVE EN ERRORES: si el step hace `assert expected in error_message`
      sin `.lower()`, reportalo como WARNING.
   c) COHERENCIA TEMPLATE/EXAMPLES — CAMPOS ECO: los campos que aparecen TANTO en el
      request body (template JSON) COMO en response_fields del contrato son "campos eco":
      la API los recibe y los devuelve sin modificar (ej: amount, currency, card_id,
      country_id, merchant_id, reason). Para estos campos, el valor expected en la columna
      del Examples DEBE SER OBLIGATORIAMENTE identico al valor del mismo campo en el
      template JSON — si difieren, el test fallara siempre porque la API devolvera el valor
      del template, no el del Examples.
      CRITICO — DONDE REPORTAR: una discrepancia de campo eco NO va en warnings, va en
      improvement_suggestions con lenguaje imperativo (ej: "Corrige el valor expected del
      campo 'amount' en el escenario 'X' de '<template>.json' (tiene 100.5) a 150.75 para
      que coincida con el template"). Es accionable y el generador puede corregirlo.
      EXCLUYE de esta verificacion los campos que son SOLO de respuesta (no estan en el
      template): 'status', 'payment_id', 'reason', o cualquier campo ausente del JSON del
      template. Si el campo NO esta en el template, no aplica la verificacion.
      CRITICO — CORRESPONDENCIA TEMPLATE/ESCENARIO: un feature puede tener multiples escenarios,
      cada uno con sus propias columnas de template en Examples (ej: template_cancelacion,
      template_autorizacion). Al verificar la coherencia de un escenario concreto, SOLO usa los
      templates que aparecen en las columnas Examples de ESE escenario especifico. JAMAS
      compares los valores expected de un escenario contra templates de otros escenarios del mismo
      feature (ej: el template 'payment_authorize_not_found' pertenece al escenario de "pago
      inexistente", NO al escenario de "autorizar exitosamente" aunque aparezca en el mismo feature).
      Si no puedes determinar con certeza que un template pertenece a un escenario concreto, OMITE
      la verificacion de ese par escenario/template — es mejor no reportar que reportar un falso positivo.
      CRITICO — CAMPOS YA VALIDADOS CON STEPS ESPECIALIZADOS: si el feature contiene un step del
      tipo "el campo '<X>' de la respuesta coincide con..." (por ejemplo "coincide con el pago creado"),
      el campo X ESTA SIENDO VALIDADO correctamente mediante un step especializado para campos dinamicos.
      NUNCA generes una improvement_suggestion pidiendo anadir validacion de ese campo — ya existe.
      En general: antes de sugerir "anade validacion del campo X", busca en el feature si ya hay
      algun Then step que referencie ese campo (con 'es', 'coincide', 'contiene', etc.). Si lo hay,
      no lo sugiereas.

Reglas de puntuacion (0.0 a 1.0):
- "coverage_score": proporcion de criterios de aceptacion cubiertos por escenarios.
  IMPORTANTE: cuenta UNICAMENTE los criterios EXPLICITAMENTE declarados en el campo
  'acceptance_criteria' o 'description' de la HU. NO inventes criterios adicionales
  basados en buenas practicas REST ni en tu conocimiento general. Si la HU no declara
  un criterio concreto, NO lo cuentes como criterio faltante.
- "semantic_score": coherencia semantica, naming y correccion de los asserts/codigos.
  CRITICO — REGLA ABSOLUTA: los warnings de implementacion de steps (puntos a, b, c
  anteriores) JAMAS pueden reducir el semantic_score ni en una decima. Son advertencias
  informativas sobre el codigo Python, completamente independientes de la calidad del
  feature BDD. Si el UNICO motivo para bajar el semantic_score son warnings de steps,
  el semantic_score DEBE ser 1.0 obligatoriamente.
  El semantic_score SOLO puede bajar por problemas en el propio feature .gherkin:
  escenarios sin sentido, nombres poco descriptivos, codigos HTTP incorrectos segun el
  contrato, o asserts que validan campos que no existen en el contrato. Si no hay ninguno
  de estos problemas en el feature, el semantic_score es 1.0 sin excepcion.
Pon "valid": false SOLO si coverage_score < 0.70, hay escenarios sin Given/When/Then
o hay codigos HTTP claramente incorrectos segun el contrato.
En "errors" lista problemas graves, en "warnings" problemas menores y en
"improvement_suggestions" recomendaciones accionables y concretas (menciona el escenario o criterio).
CRITICO — FORMATO DE improvement_suggestions: cada sugerencia DEBE estar redactada en modo IMPERATIVO
y describir una accion concreta a realizar (ej: "Anade un escenario que valide..."). ESTA TERMINANTEMENTE
PROHIBIDO usar lenguaje opcional o condicionado en improvement_suggestions: no uses frases como
"podrias anadir", "seria util", "aunque no es obligatorio", "no afecta al coverage_score", "opcional",
"si el contrato lo incluye", "si existe en la respuesta", "si aplica", "si procede", "deberia verificarse",
"podria ser util", "considera anadir". Si incluyes una sugerencia en la lista, es porque DEBE
implementarse de forma incondicional — si depende de una condicion o es realmente opcional, NO la
incluyas en improvement_suggestions, ponla en "warnings" en su lugar.
CRITICO — CAMPOS FUERA DEL CONTRATO: JAMAS incluyas en improvement_suggestions la validacion de
un campo que NO aparezca en los response_fields del contrato para ese endpoint/codigo HTTP concreto.
Da igual si el campo existe en otro endpoint, en el request body o en "la respuesta real" — si no
esta en los response_fields del contrato para ese codigo, NO lo sugieras. Si tienes duda de si un
campo esta en el contrato, OMITE la sugerencia. Una sugerencia de este tipo causaria un fallo
seguro en ejecucion y ademas viola la regla de origen. Si el feature YA valida un campo que NO
esta en los response_fields del contrato, eso es un WARNING (el test puede fallar), NO una
improvement_suggestion sobre añadir mas validaciones del mismo tipo.
CRITICO — ORIGEN DE LAS SUGERENCIAS: cada improvement_suggestion DEBE estar respaldada por uno de
estos dos origenes: (1) un criterio de aceptacion explicito de la HU que no esta cubierto, o (2) algo
documentado en el contrato OpenAPI (un campo de response_fields, un codigo de respuesta, un mensaje de
error de examples). ESTA TERMINANTEMENTE PROHIBIDO sugerir mejoras basadas en buenas practicas REST
generales, convenios de la industria o tu conocimiento externo si no aparecen en la HU ni en el
contrato. Si una mejora no puede citarse directamente de la HU o del contrato, NO va en
improvement_suggestions — va en warnings o se omite.
CRITICO — ESCENARIOS DE PATH PARAM INVALIDO: cuando sugieras anadir un escenario que pruebe un
path parameter con valor invalido, el endpoint del escenario DEBE incluir el valor invalido como
path param en la URL (ej: '/payments/{{payment_id}}' con payment_id='abc', no '/payments/abc' como
endpoint literal). Si el path param se omite, la ruta no coincide con el endpoint correcto y la
API devuelve un error de routing incorrecto. Especifica esto explicitamente en la sugerencia.
CRITICO — PATH PARAMS DE TIPO STRING: NUNCA sugieras escenarios de "formato invalido" o "valor
malformado" para path parameters cuyo tipo es string (texto libre). Un path param de tipo string
acepta cualquier valor sin validacion de formato — la API simplemente buscara ese valor y devolvera
404 si no existe, NO 400. Solo sugiere escenarios de "formato invalido → 400" para path params
cuyo tipo es integer (entero), donde un valor no numerico como 'abc' realmente provoca error de
parsing. Si el path param es string, el unico error valido es 404 (no encontrado).
Ademas, para justificar el coverage_score, rellena OBLIGATORIAMENTE:
- "total_criterios": numero total de criterios de aceptacion de la HU (entero).
- "criterios_cubiertos": lista de textos de los criterios que SI estan cubiertos por algun escenario.
- "criterios_sin_cubrir": lista de textos de los criterios que NO estan cubiertos.
- "escenarios_faltantes": lista de escenarios concretos que faltan (positivos/negativos).
- "escenarios_detectados": lista de los titulos de escenario presentes en el feature.

CODIGOS DEL CONTRATO (OpenAPI) - fuente de verdad de los status code validos por endpoint:
{contrato_codigos}

CAMPOS DE RESPUESTA DEL CONTRATO (OpenAPI) - universo COMPLETO y EXHAUSTIVO de campos por endpoint/codigo:
{contrato_response_fields}
IMPORTANTE: estos campos son TODO lo que devuelve la API. Si un campo no aparece aqui, NO EXISTE en la respuesta.
No sugieras validar campos ausentes de esta lista aunque "deberían" estar segun buenas practicas REST.
Ejemplo: si POST /payments [200] -> campos: [status, amount, currency, card_id], entonces payment_id NO existe y NO debe validarse.

ESTRUCTURA DE RESPUESTAS DE ERROR DEL CONTRATO - como devuelve la API los errores 4xx/5xx:
{contrato_error_ejemplos}
Verifica que los steps de validacion de errores accedan a los campos usando esta estructura exacta.

TEMPLATES JSON REFERENCIADOS EN EL FEATURE (contenido real de los archivos):
{templates_reales}
USA ESTOS VALORES REALES para verificar la coherencia entre los expected del Examples y el template.
SOLO reporta incoherencia si el valor esperado en el Examples DIFIERE del valor en el template.

HU:
{json.dumps(hu, indent=2, ensure_ascii=False)}

Feature generado:
{feature_content}

Steps generados:
{generated.get('steps_content', '')}

Devuelve EXACTAMENTE este JSON:
{{
  "valid": true,
  "coverage_score": 0.85,
  "semantic_score": 0.90,
  "total_criterios": 5,
  "criterios_cubiertos": ["criterio cubierto 1"],
  "criterios_sin_cubrir": ["criterio sin cubrir 1"],
  "escenarios_detectados": ["Scenario: titulo del escenario"],
  "escenarios_faltantes": ["Escenario negativo de campo obligatorio ausente"],
  "errors": [],
  "warnings": [],
  "improvement_suggestions": []
}}"""
            response = self.model.generate_feature(prompt)
            response_text = response.text if hasattr(response, 'text') else str(response)

            # Parsear JSON de la respuesta
            try:
                result = json.loads(response_text)
            except json.JSONDecodeError:
                # Si no es JSON válido, extraer el JSON del response
                import re as regex
                json_match = regex.search(r'[{\[].*[}\]]', response_text, regex.DOTALL)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    result = {}

            result.setdefault("errors", [])
            result.setdefault("warnings", [])
            logger.info("Validación semántica completada")
            return result
        except Exception as exc:
            logger.error(f"Error en validación semántica: {str(exc)}")
            return {
                "valid": False,
                "coverage_score": 0.0,
                "semantic_score": 0.0,
                "errors": [f"Error en validación semántica: {str(exc)}"],
                "warnings": [],
                "improvement_suggestions": [],
            }

    @staticmethod
    def _calcular_cobertura_openapi(generated: dict) -> float:
        """Calcula cobertura como % de codigos de respuesta documentados que tienen escenario generado."""
        import re as _re
        feature_content = generated.get("feature_content", "") or ""
        used_endpoints = generated.get("used_endpoints") or []

        # Extraer códigos expected_status de las tablas Examples del feature
        exercised_codes: set = set()
        in_examples = False
        headers: list = []
        status_col = -1
        for line in feature_content.splitlines():
            stripped = line.strip()
            if _re.match(r"Examples:\s*", stripped, _re.IGNORECASE):
                in_examples = True
                headers = []
                status_col = -1
                continue
            if not stripped.startswith("|"):
                if in_examples:
                    in_examples = False
                continue
            if not in_examples:
                continue
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if not headers:
                headers = [h.lower() for h in cells]
                status_col = headers.index("expected_status") if "expected_status" in headers else -1
            elif status_col >= 0 and status_col < len(cells):
                try:
                    exercised_codes.add(str(int(cells[status_col])))
                except (ValueError, IndexError):
                    pass

        # Recopilar codigos documentados en los endpoints usados
        documented_codes: set = set()
        for ep in used_endpoints:
            for code in (ep.get("responses") or {}):
                documented_codes.add(str(code))

        if not documented_codes:
            return 1.0
        covered = len(exercised_codes & documented_codes)
        return round(covered / len(documented_codes), 3)

    def validate_complete(self, generated: Dict[str, Any], hu: Dict[str, Any], validar_semantica: bool = True) -> Dict[str, Any]:
        """
        Ejecuta validación completa (estática + semántica).
        
        Args:
            generated: Diccionario con feature_content, steps_content, used_endpoints
            hu: Diccionario con información de la HU
            validar_semantica: Si False, omite semantic_validate y puntua funcional como 1.0
            
        Returns:
            dict: Resultado combinado de ambas validaciones
        """
        try:
            logger.info("Iniciando validación completa...")
            static_result = self.static_validate(generated)
            if validar_semantica:
                semantic_result = self.semantic_validate(generated, hu)
            else:
                _cov = self._calcular_cobertura_openapi(generated)
                semantic_result = {
                    "valid": _cov >= 0.70,
                    "coverage_score": _cov,
                    "semantic_score": 1.0,
                    "criterios_cubiertos": [],
                    "criterios_sin_cubrir": [],
                    "escenarios_faltantes": [],
                    "escenarios_detectados": [],
                    "errors": [],
                    "warnings": [],
                    "improvement_suggestions": [],
                }

            categorias = static_result.get("categorias", {}) or {}
            result = {
                "hu_id": hu.get("ref", "UNKNOWN"),
                "static_validation": static_result,
                "semantic_validation": semantic_result,
                # Las 4 validaciones que realiza el sistema, de forma explicita:
                "validaciones": {
                    "sintactica": categorias.get("sintactica", {}),       # 1. sintaxis Gherkin
                    "cohesion": categorias.get("cohesion", {}),           # 2. escenario <-> pasos
                    "tecnica": categorias.get("tecnica", {}),             # 3. contrato API
                    "funcional": semantic_result,                          # 4. respecto a la HU
                },
                "overall_valid": static_result["valid"] and (
                    semantic_result.get("coverage_score", 0) >= 0.70
                    or semantic_result.get("valid", False)
                ),
                "overall_score": (
                    (1.0 if categorias.get("sintactica", {}).get("valid", False) else 0.0) +
                    (1.0 if categorias.get("cohesion", {}).get("valid", False) else 0.0) +
                    (1.0 if categorias.get("tecnica", {}).get("valid", False) else 0.0) +
                    (semantic_result.get("coverage_score", 0) + semantic_result.get("semantic_score", 0)) / 2  # funcional: media de cobertura + coherencia semantica
                ) / 4
            }
            logger.info(f"Validación completa finalizada. Válido: {result['overall_valid']}")
            return result
        except Exception as e:
            logger.error(f"Error en validación completa: {str(e)}")
            return {
                "overall_valid": False,
                "error": str(e),
                "static_validation": {"valid": False, "errors": [str(e)]},
                "semantic_validation": {"valid": False, "errors": [str(e)]}
            }

    @staticmethod
    def _static_validate_openapi(data: Dict[str, Any]) -> Dict[str, Any]:
        """Validacion estatica del esquema OpenAPI: endpoints y codigos de respuesta."""
        errors: List[str] = []
        warnings: List[str] = []
        endpoints: List[str] = []
        response_codes: Dict[str, List[str]] = {}
        metodos_validos = {"get", "post", "put", "delete", "patch"}

        paths = data.get("paths", {}) or {}
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, info in methods.items():
                if method.lower() not in metodos_validos:
                    continue
                clave = f"{method.upper()} {path}"
                endpoints.append(clave)
                responses = info.get("responses", {}) if isinstance(info, dict) else {}
                codigos = [str(c) for c in responses.keys()]
                response_codes[clave] = codigos
                if not codigos:
                    errors.append(f"{clave}: sin codigos de respuesta definidos")
                elif not any(c.startswith(("4", "5")) for c in codigos):
                    warnings.append(f"{clave}: sin respuestas de error (4xx/5xx)")
                if isinstance(info, dict) and not info.get("summary"):
                    warnings.append(f"{clave}: sin summary")

        if not endpoints:
            errors.append("El esquema no define endpoints")

        return {
            "valid": len(errors) == 0,
            "endpoints_count": len(endpoints),
            "endpoints": endpoints,
            "response_codes": response_codes,
            "errors": errors,
            "warnings": warnings,
        }

    def _semantic_validate_openapi(self, data: Dict[str, Any], static_result: Dict[str, Any]) -> Dict[str, Any]:
        """Validacion semantica del esquema OpenAPI usando Claude."""
        try:
            resumen = json.dumps({
                "info": data.get("info", {}),
                "endpoints": static_result.get("endpoints", []),
                "response_codes": static_result.get("response_codes", {}),
            }, indent=2, ensure_ascii=False)

            prompt = f"""Eres un validador experto de APIs REST y contratos OpenAPI.
Evalua el siguiente esquema y devuelve SOLO JSON valido sin markdown, sin texto adicional.

Analiza y puntua especificamente:
1. ENDPOINTS: coherencia semantica, nombres claros y consistentes, diseno RESTful
   (uso correcto de metodos HTTP segun la accion, rutas y recursos coherentes,
   uso adecuado de parametros de ruta/query).
2. CODIGOS DE RESPUESTA: para cada endpoint, comprueba si los codigos posibles son
   razonables y completos. Cada endpoint deberia declarar al menos un 2xx de exito y
   los 4xx/5xx pertinentes (400 datos invalidos, 401/403 auth, 404 no encontrado,
   409 conflicto, 422 validacion, 5xx error servidor) segun su naturaleza.
   Señala codigos faltantes o sobrantes.
3. MANEJO DE ERRORES: consistencia del formato de error entre endpoints.
4. SEMANTICA Y COMPLETITUD: el contrato refleja una logica de negocio coherente y completa.

Reglas de puntuacion (0.0 a 1.0):
- "coverage_score": que tan completa es la cobertura de codigos de respuesta por endpoint.
- "semantic_score": coherencia semantica, naming y diseno RESTful.
Pon "valid": false si detectas problemas graves (endpoints sin respuestas, ausencia
total de manejo de errores o incoherencias serias).
En "errors" lista problemas graves, en "warnings" problemas menores y en
"improvement_suggestions" recomendaciones accionables y concretas (menciona el endpoint).

Esquema (resumen con endpoints y codigos de respuesta por endpoint):
{resumen}

Devuelve EXACTAMENTE este JSON:
{{
  "valid": true,
  "coverage_score": 0.85,
  "semantic_score": 0.90,
  "errors": [],
  "warnings": [],
  "improvement_suggestions": []
}}"""
            response = self.model.generate_feature(prompt)
            response_text = response.text if hasattr(response, 'text') else str(response)

            try:
                result = json.loads(response_text)
            except json.JSONDecodeError:
                json_match = re.search(r'[{\[].*[}\]]', response_text, re.DOTALL)
                result = json.loads(json_match.group()) if json_match else {}

            result.setdefault("errors", [])
            result.setdefault("warnings", [])
            result.setdefault("improvement_suggestions", [])
            return result
        except Exception as exc:
            logger.error(f"Error en validacion semantica OpenAPI: {str(exc)}")
            return {
                "valid": False,
                "coverage_score": 0.0,
                "semantic_score": 0.0,
                "errors": [f"Error en validacion semantica OpenAPI: {str(exc)}"],
                "warnings": [],
                "improvement_suggestions": [],
            }

    def validate_openapi(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Valida un esquema OpenAPI (estatica + semantica): endpoints usados,
        codigos de respuesta posibles y coherencia semantica.

        Args:
            data: Esquema OpenAPI ya parseado (dict)

        Returns:
            dict: Resultado combinado de ambas validaciones
        """
        try:
            logger.info("Validando esquema OpenAPI...")
            static_result = self._static_validate_openapi(data)
            semantic_result = self._semantic_validate_openapi(data, static_result)
            return {
                "static_validation": static_result,
                "semantic_validation": semantic_result,
                "overall_valid": static_result["valid"] and semantic_result.get("valid", False),
                "overall_score": (
                    semantic_result.get("coverage_score", 0) +
                    semantic_result.get("semantic_score", 0)
                ) / 2,
            }
        except Exception as e:
            logger.error(f"Error al validar OpenAPI: {str(e)}")
            return {
                "overall_valid": False,
                "error": str(e),
                "static_validation": {"valid": False, "errors": [str(e)]},
                "semantic_validation": {"valid": False, "errors": [str(e)]},
            }

    @staticmethod
    def _observados_de_features(features_dir: Path) -> set:
        """Extrae el conjunto de (metodo, endpoint, codigo) ejercitados por los .feature.

        Parsea las tablas Examples de los Scenario Outline generados, buscando las
        columnas 'method', 'endpoint' y 'status_code'.
        """
        observados = set()
        if not features_dir.exists():
            return observados
        for ff in features_dir.rglob("*.feature"):
            try:
                lineas = ff.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            i = 0
            while i < len(lineas):
                if not lineas[i].strip().lower().startswith("examples"):
                    i += 1
                    continue
                # Localizar la fila de cabecera de la tabla
                j = i + 1
                while j < len(lineas) and not lineas[j].strip().startswith("|"):
                    j += 1
                if j >= len(lineas):
                    break
                header = [c.strip().lower() for c in lineas[j].strip().strip("|").split("|")]
                idx_m = header.index("method") if "method" in header else None
                idx_e = header.index("endpoint") if "endpoint" in header else None
                idx_s = next((k for k, h in enumerate(header)
                              if h in ("status_code", "statuscode", "status", "codigo")), None)
                j += 1
                while j < len(lineas) and lineas[j].strip().startswith("|"):
                    celdas = [c.strip() for c in lineas[j].strip().strip("|").split("|")]
                    m = celdas[idx_m].upper() if idx_m is not None and idx_m < len(celdas) else None
                    e = celdas[idx_e] if idx_e is not None and idx_e < len(celdas) else None
                    s = celdas[idx_s] if idx_s is not None and idx_s < len(celdas) else None
                    if m and e:
                        observados.add((m, e, s))
                    j += 1
                i = j
        return observados

    @staticmethod
    def cobertura_openapi(data: Dict[str, Any], features_dir: str = "tests/features") -> Dict[str, Any]:
        """Calcula la cobertura de los features generados respecto al contrato OpenAPI.

        Devuelve el porcentaje de endpoints del contrato con al menos un escenario y el
        porcentaje de pares (endpoint, codigo de respuesta) verificados por algun escenario.
        """
        metodos_validos = {"GET", "POST", "PUT", "DELETE", "PATCH"}
        spec = []  # (method, path, [codes])
        for path, methods in (data.get("paths", {}) or {}).items():
            if not isinstance(methods, dict):
                continue
            for method, info in methods.items():
                if method.upper() not in metodos_validos:
                    continue
                info = info if isinstance(info, dict) else {}
                codes = [str(c) for c in (info.get("responses", {}) or {}).keys()]
                spec.append((method.upper(), path, codes))

        observados = AgentValidator._observados_de_features(Path(features_dir))

        def _coincide(method: str, endpoint: str):
            candidatos = [p for (m, p, _) in spec if m == method]
            # 1) coincidencia exacta
            if endpoint in candidatos:
                return endpoint
            # 2) coincidencia contra plantilla con parametros {x}
            for p in candidatos:
                if "{" not in p:
                    continue
                partes = [
                    "[^/]+" if seg.startswith("{") and seg.endswith("}") else re.escape(seg)
                    for seg in p.split("/")
                ]
                if re.match("^" + "/".join(partes) + "$", endpoint):
                    return p
            return None

        endpoints_cubiertos = set()
        pares_cubiertos = set()
        for (m, ep, code) in observados:
            p = _coincide(m, ep)
            if not p:
                continue
            endpoints_cubiertos.add((m, p))
            if code:
                pares_cubiertos.add((m, p, str(code)))

        total_endpoints = {(m, p) for (m, p, _) in spec}
        total_pares = {(m, p, c) for (m, p, cs) in spec for c in cs}

        ep_cov = len(endpoints_cubiertos) / len(total_endpoints) if total_endpoints else 0.0
        code_cov = len(pares_cubiertos) / len(total_pares) if total_pares else 0.0

        endpoints_sin_cubrir = sorted(f"{m} {p}" for (m, p) in (total_endpoints - endpoints_cubiertos))
        codigos_sin_cubrir = sorted(f"{m} {p} -> {c}" for (m, p, c) in (total_pares - pares_cubiertos))

        return {
            "endpoints_total": len(total_endpoints),
            "endpoints_cubiertos": len(endpoints_cubiertos),
            "endpoint_coverage": round(ep_cov, 4),
            "codigos_total": len(total_pares),
            "codigos_cubiertos": len(pares_cubiertos),
            "code_coverage": round(code_cov, 4),
            "endpoints_sin_cubrir": endpoints_sin_cubrir,
            "codigos_sin_cubrir": codigos_sin_cubrir,
        }

    @staticmethod
    def load_generated(feature_path: str, steps_path: str) -> Dict[str, Any]:
        """
        Carga los archivos feature y steps generados.
        
        Args:
            feature_path: Ruta al archivo .feature
            steps_path: Ruta al archivo steps.py
            
        Returns:
            dict: Contenido de ambos archivos
            
        Raises:
            FileNotFoundError: Si alguno de los archivos no existe
        """
        try:
            feature_file = Path(feature_path)
            steps_file = Path(steps_path)

            if not feature_file.exists():
                raise FileNotFoundError(f"Archivo feature no encontrado: {feature_path}")
            if not steps_file.exists():
                raise FileNotFoundError(f"Archivo steps no encontrado: {steps_path}")

            return {
                "feature_file": feature_path,
                "feature_content": feature_file.read_text(encoding="utf-8"),
                "steps_file": steps_path,
                "steps_content": steps_file.read_text(encoding="utf-8"),
            }
        except Exception as e:
            logger.error(f"Error al cargar archivos generados: {str(e)}")
            raise


if __name__ == '__main__':
    import sys
    from clients.ingest import extract_info
    from agents.agent_generator import AgentGenerator

    try:
        # Ejemplo: validar HU#1
        hu_number = int(sys.argv[1]) if len(sys.argv) > 1 else 1

        logger.info(f"Flujo completo: Ingestión → Generación → Validación de HU#{hu_number}")

        # 1. Extraer HU
        logger.info("1. Extrayendo HU...")
        hu_data = extract_info(hu_number)
        if not hu_data:
            logger.error(f"No se encontró HU#{hu_number}")
            sys.exit(1)
        hu = hu_data[0]

        # 2. Generar feature
        logger.info("2. Generando feature...")
        generator = AgentGenerator()
        feature_result = generator.generate_feature(hu)

        # 3. Guardar archivos
        logger.info("3. Guardando archivos generados...")
        feature_path = generator.save_feature(hu, feature_result)

        # 4. Validar
        logger.info("4. Validando feature y steps...")
        validator = AgentValidator()

        # Cargar archivos generados
        generated = AgentValidator.load_generated(
            feature_path,
            "output/steps.py"
        )
        generated["used_endpoints"] = feature_result.get("endpoints_swagger", [])

        # Ejecutar validación completa
        validation_result = validator.validate_complete(generated, hu)

        # Mostrar resultados
        print("\n" + "=" * 60)
        print("RESULTADO DE VALIDACIÓN")
        print("=" * 60)
        print(json.dumps(validation_result, indent=2, ensure_ascii=False))

        if not validation_result.get("overall_valid"):
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error en flujo completo: {str(e)}")
        sys.exit(1)
