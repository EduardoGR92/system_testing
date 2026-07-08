import requests
import yaml
class SwaggerReader:
    def __init__(self,swagger_url:str="payments.yaml"):
        self.swagger_url=swagger_url

    def load_swagger(self):
            # Soporta URL HTTP(S) o ruta local a YAML/JSON
            if str(self.swagger_url).lower().startswith(('http://','https://')):
                response = requests.get(self.swagger_url)
                response.raise_for_status()
                return response.json()
            else:
                # Leer archivo local (YAML o JSON)
                from pathlib import Path
                p = Path(self.swagger_url)
                if not p.exists():
                    raise FileNotFoundError(f"Swagger file not found: {self.swagger_url}")
                text = p.read_text(encoding='utf-8')
                try:
                    # Intentar parsear como JSON
                    import json
                    return json.loads(text)
                except Exception:
                    # Intentar parsear como YAML
                    return yaml.safe_load(text)


    def get_endpoints(self):
        endpoints=[]

        swagger_data= self.load_swagger()

        paths=swagger_data["paths"]

        for path, methods in paths.items():
            for method, details in methods.items():
                endpoint_info={
                    "path": path,
                    "method": method.upper(),
                    "summary": details.get("summary", ""),
                    "parameters": details.get("parameters", []),
                    "requestBody": details.get("requestBody", {}),
                    "responses": details.get("responses", {})
                }
                endpoints.append(endpoint_info)

        return endpoints

    def get_endpoints_normalizados(self):
        """Devuelve los endpoints con el template del body ya normalizado (\\$ref resuelto)."""
        return self.normalizar_endpoints(self.load_swagger())

    @staticmethod
    def normalizar_endpoints(swagger_data: dict) -> list:
        """Normaliza un esquema OpenAPI ya parseado en una lista de endpoints
        con la informacion basica y el template del body resuelto desde components/schemas.

        Cada endpoint: {method, path, summary, path_params, responses, request_template}
        donde request_template es {schema, required, fields} o None si no hay body.
        """
        metodos_validos = {"get", "post", "put", "delete", "patch"}
        schemas = (swagger_data.get("components", {}) or {}).get("schemas", {}) or {}
        endpoints = []
        for path, methods in (swagger_data.get("paths", {}) or {}).items():
            if not isinstance(methods, dict):
                continue
            for method, details in methods.items():
                if method.lower() not in metodos_validos or not isinstance(details, dict):
                    continue
                path_params = [
                    p.get("name")
                    for p in (details.get("parameters", []) or [])
                    if p.get("in") == "path"
                ]
                responses = {
                    str(code): SwaggerReader._resolver_response(det, schemas)
                    for code, det in (details.get("responses", {}) or {}).items()
                }
                endpoints.append({
                    "method": method.upper(),
                    "path": path,
                    "summary": details.get("summary", ""),
                    "path_params": path_params,
                    "responses": responses,
                    "request_template": SwaggerReader._resolver_template(
                        details.get("requestBody", {}) or {}, schemas
                    ),
                })
        return endpoints

    @staticmethod
    def _resolver_response(det, schemas: dict = None) -> dict:
        """Normaliza una respuesta a {description, example?, schema?, response_fields?}.

        Incluye 'example' y/o 'schema' (nombre del \\$ref) solo si existen, para que
        el generador pueda distinguir condiciones similares (p.ej. 400 vs 422).
        Si el \\$ref apunta a un schema conocido, resuelve tambien 'response_fields'
        con el mapa campo->tipo, igual que request_template.fields para el body.
        """
        if not isinstance(det, dict):
            return {"description": ""}
        info = {"description": det.get("description", "")}
        content = (det.get("content", {}) or {}).get("application/json", {}) or {}
        if "example" in content:
            info["example"] = content["example"]
        if "examples" in content:
            named = content["examples"]
            if isinstance(named, dict):
                extracted = [v["value"] for v in named.values() if isinstance(v, dict) and "value" in v]
                if len(extracted) == 1:
                    info["example"] = extracted[0]
                elif extracted:
                    info["examples"] = extracted
        schema = content.get("schema")
        if isinstance(schema, dict):
            if schema.get("$ref"):
                nombre = schema["$ref"].split("/")[-1]
                info["schema"] = nombre
                if schemas and nombre in schemas:
                    definicion = schemas[nombre] or {}
                    propiedades = definicion.get("properties", {}) or {}
                    info["response_fields"] = {
                        campo: (detalle.get("type") or "object")
                        for campo, detalle in propiedades.items()
                        if isinstance(detalle, dict)
                    }
                    if "example" not in info and "example" in definicion:
                        info["example"] = definicion["example"]
            elif schema.get("type") == "array":
                items = schema.get("items", {}) or {}
                item_ref = items.get("$ref", "")
                nombre = item_ref.split("/")[-1] if item_ref else None
                info["is_list"] = True
                if nombre:
                    info["schema"] = nombre
                if schemas and nombre and nombre in schemas:
                    definicion = schemas[nombre] or {}
                    propiedades = definicion.get("properties", {}) or {}
                    info["response_fields"] = {
                        campo: (detalle.get("type") or "object")
                        for campo, detalle in propiedades.items()
                        if isinstance(detalle, dict)
                    }
                    if "example" not in info and "example" in definicion:
                        info["example"] = definicion["example"]
        return info

    @staticmethod
    def _resolver_template(request_body: dict, schemas: dict):
        """Resuelve el \\$ref del requestBody a un esqueleto de template (campos, tipos, requeridos)."""
        try:
            schema = request_body["content"]["application/json"]["schema"]
        except (KeyError, TypeError):
            return None
        ref = schema.get("$ref", "") if isinstance(schema, dict) else ""
        nombre = ref.split("/")[-1] if ref else None
        if not nombre or nombre not in schemas:
            return None
        definicion = schemas[nombre] or {}
        propiedades = definicion.get("properties", {}) or {}
        fields = {}
        for campo, detalle in propiedades.items():
            if not isinstance(detalle, dict):
                fields[campo] = "object"
                continue
            tipo = detalle.get("type")
            if not tipo and detalle.get("$ref"):
                tipo = detalle["$ref"].split("/")[-1]
            fields[campo] = tipo or "object"
        return {
            "schema": nombre,
            "required": definicion.get("required", []) or [],
            "fields": fields,
        }