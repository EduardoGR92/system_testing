"""
Hooks de Behave para capturar llamadas HTTP durante la ejecucion.
Escribe los pares request/response en BEHAVE_CALLS_FILE (JSON) para
que reporter.py pueda incluirlos en el Word de evidencias.
"""
import json
import os

_calls: list = []


def before_all(context):
    global _calls
    _calls = []


def after_step(context, step):
    """Captura la llamada HTTP realizada en los steps 'when'."""
    if step.step_type != "when":
        return
    response = getattr(context, "response", None)
    if response is None:
        return

    calls_file = os.getenv("BEHAVE_CALLS_FILE")
    if not calls_file:
        return

    scenario_name = getattr(context.scenario, "name", "") if hasattr(context, "scenario") else ""

    # --- Request ---
    req = getattr(response, "request", None)
    request_data: dict = {}
    if req:
        body = None
        if req.body:
            try:
                body = json.loads(req.body)
            except (json.JSONDecodeError, TypeError, ValueError):
                body = str(req.body)
        request_data = {
            "method": req.method or "",
            "url": req.url or "",
            "headers": dict(req.headers or {}),
            "body": body,
        }

    # --- Response ---
    try:
        resp_body = response.json()
        resp_body_str = json.dumps(resp_body, ensure_ascii=False)
    except Exception:
        resp_body_str = response.text or ""

    response_data = {
        "status_code": response.status_code,
        "body": resp_body_str,
    }

    _calls.append({
        "scenario": scenario_name,
        "request": request_data,
        "response": response_data,
    })

    # Escribir de forma incremental para no perder datos si behave falla
    try:
        with open(calls_file, "w", encoding="utf-8") as fh:
            json.dump(_calls, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
