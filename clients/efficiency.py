"""Medición reproducible por ejecución de los tres casos de uso del TFM."""

import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)
DEFAULT_MODEL = "claude-sonnet-5-5"
# Claude API estándar, USD por millón de tokens, 2026-11-17.
# https://platform.claude.com/docs/en/about-claude/pricing
RATES = {
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 4.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 4.0, 0.20),
}
_run = ContextVar("efficiency_run", default=None)
_unit = ContextVar("efficiency_unit", default="")
_phase = ContextVar("efficiency_phase", default="")


def configured_model():
    model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL).strip()
    if model not in RATES:
        raise ValueError(f"Modelo no admitido: {model}. Configura Claude Sonnet 5 o 5.5.")
    return model


def usage_record(model, usage):
    """Devuelve tokens facturados y coste estimado; sin usage no inventa ceros."""
    if usage is None:
        return {"input_tokens": None, "output_tokens": None,
                "cache_creation_tokens": None, "cache_read_tokens": None,
                "total_tokens": None, "cost_usd": None, "cache_cost_estimated": False}
    inp = getattr(usage, "input_tokens", None)
    out = getattr(usage, "output_tokens", None)
    created = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache = getattr(usage, "cache_creation", None)
    one_hour = getattr(cache, "ephemeral_1h_input_tokens", 0) or 0
    five_min = getattr(cache, "ephemeral_5m_input_tokens", 0) or 0
    # Si falta el desglose, se supone TTL de cinco minutos (predeterminado).
    unclassified = max(0, created - one_hour - five_min)
    five_min += unclassified
    rates = RATES[model]
    cost = None if inp is None or out is None else (
        inp*rates[0] + out*rates[1] + five_min*rates[2] + one_hour*rates[3] + read*rates[4]
    ) / 1_000_000
    return {"input_tokens": inp, "output_tokens": out,
            "cache_creation_tokens": created, "cache_read_tokens": read,
            "total_tokens": None if inp is None or out is None else inp+out+created+read,
            "cost_usd": cost, "cache_cost_estimated": bool(unclassified)}


@contextmanager
def contexto_llm(unit, phase):
    """Identifica la HU/endpoint y fase sin guardar contenido ni credenciales."""
    unit_token = _unit.set(str(unit))
    phase_token = _phase.set(phase)
    try:
        yield
    finally:
        _phase.reset(phase_token)
        _unit.reset(unit_token)


def record_attempt(record):
    run = _run.get()
    if run is not None:
        run["requests"].append({**record, "unit": _unit.get(),
                                 "phase": _phase.get() or record["role"]})


def registrar_tiempo_fase(fase, segundos):
    """Acumula los tiempos de una fase dentro de la ejecución activa."""
    run = _run.get()
    if run is not None:
        run["phase_seconds"][fase] = run["phase_seconds"].get(fase, 0.0) + segundos


def timed_input(prompt=""):
    """Separa la espera de decisiones humanas del tiempo de proceso."""
    start = time.perf_counter()
    try:
        return input(prompt)
    finally:
        run = _run.get()
        if run is not None:
            run["human_seconds"] += time.perf_counter() - start


def summarize(run, elapsed):
    records = run["requests"]
    known = [r for r in records if r["cost_usd"] is not None]
    por_agente = {}
    for role, fase in (("Generador", "generation"), ("Validador", "validation")):
        peticiones = [r for r in known if r["role"] == role]
        por_agente[f"{fase}_input_tokens"] = sum(
            r["input_tokens"] + r["cache_creation_tokens"] + r["cache_read_tokens"]
            for r in peticiones)
        por_agente[f"{fase}_output_tokens"] = sum(r["output_tokens"] for r in peticiones)
    return {
        "wall_seconds": elapsed,
        "human_seconds": run["human_seconds"],
        "process_seconds": max(0.0, elapsed-run["human_seconds"]),
        "llm_seconds": sum(r["seconds"] for r in records),
        "requests": len(records),
        "successful": sum(r["status"] == "ok" for r in records),
        "without_usage": len(records)-len(known),
        "input_tokens": sum(r["input_tokens"] for r in known),
        "output_tokens": sum(r["output_tokens"] for r in known),
        "cache_creation_tokens": sum(r["cache_creation_tokens"] for r in known),
        "cache_read_tokens": sum(r["cache_read_tokens"] for r in known),
        "total_tokens": sum(r["total_tokens"] for r in known),
        "cost_usd": sum(r["cost_usd"] for r in known),
        **por_agente,
    }


def metrics_path(case):
    """Reutiliza exactamente los mismos Excel que el reporter de calidad."""
    from reporter import HU_METRICS_FILE, OPENAPI_METRICS_FILE
    return OPENAPI_METRICS_FILE if case == "OpenAPI YAML" else HU_METRICS_FILE


def _actualizar_costes_calidad(wb, run, totals):
    """Añade los costes por unidad a validación y el coste global a evaluación."""
    from copy import copy
    from openpyxl.styles import Alignment, PatternFill
    from reporter import crear_vista_metricas

    def normalizar(ref):
        valor = str(ref or "").strip()
        return valor[3:] if valor.upper().startswith("HU-") else valor

    etiqueta_coste = "Coste estimado (USD)"
    if "Datos validacion" in wb.sheetnames:
        ws = wb["Datos validacion"]
        columna = next((c.column for c in ws[1] if c.value == etiqueta_coste), ws.max_column + 1)
        ws.cell(1, columna, etiqueta_coste)
        for fila in range(2, ws.max_row + 1):
            ref = normalizar(ws.cell(fila, 2).value)
            llamadas = [r for r in run["requests"] if normalizar(r.get("unit")) == ref]
            conocidos = [r["cost_usd"] for r in llamadas if r.get("cost_usd") is not None]
            celda = ws.cell(fila, columna)
            celda.value = sum(conocidos) if conocidos else None
            celda.number_format = '0.00" (parcial)"' if len(conocidos) < len(llamadas) else '0.00'
        crear_vista_metricas(wb, ws, "Metricas validacion", lambda v, n: str(v[1] or f"HU {n}"))

    if "Evaluacion" in wb.sheetnames:
        ev = wb["Evaluacion"]
        etiquetas = {etiqueta_coste, "Coste parcial (USD)"}
        fila = next((r for r in range(2, ev.max_row + 1) if ev.cell(r, 2).value in etiquetas), ev.max_row + 1)
        coste_completo = not totals["without_usage"]
        valores = ["Eficiencia", etiqueta_coste if coste_completo else "Coste parcial (USD)",
                   "Numérico", totals["cost_usd"], "OK" if coste_completo else "N/A"]
        for c, valor in enumerate(valores, 1):
            celda = ev.cell(fila, c)
            celda.value = valor
            celda._style = copy(ev.cell(2, c)._style)
            if c != 2:
                celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ev.cell(fila, 4).number_format = '0.00'
        ev.cell(fila, 5).fill = PatternFill("solid", fgColor="C6EFCE" if coste_completo else "F2F2F2")


def save_metrics(run, totals, path=None):
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from reporter import hoja_datos_metricas, crear_vista_metricas

    path = Path(path or metrics_path(run["case"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        wb = load_workbook(path)
    else:
        wb = Workbook()
        wb.active.title = "Eficiencia"
    specs = {
        "Eficiencia": ["ID ejecución", "Fecha", "Caso de uso", "Referencia", "Modelo", "Estado", "Tiempo total (s)", "Espera usuario (s)", "Tiempo proceso (s)", "Tiempo API (s)", "Peticiones API", "Respuestas de texto completas", "Peticiones sin usage", "Tokens entrada sin caché", "Tokens salida", "Tokens escritura caché", "Tokens lectura caché", "Tokens generación (entrada)", "Tokens generación (salida)", "Tokens validación (entrada)", "Tokens validación (salida)", "Tokens totales conocidos", "Coste estimado conocido (USD)"],
        "Peticiones": ["ID ejecución", "Nº petición", "Modelo", "Agente", "Fase", "HU / endpoint", "Intento", "Estado", "Tiempo API (s)", "Tokens entrada sin caché", "Tokens salida", "Tokens escritura caché", "Tokens lectura caché", "Tokens totales", "Coste estimado (USD)", "Error (tipo)", "TTL caché estimado"],
    }
    fuentes = {}
    for name, headers in specs.items():
        ws = hoja_datos_metricas(wb, name, f"Datos {name.lower()}", headers)
        fuentes[name] = ws
        # Migra el histórico anterior sin sobrescribir las columnas de total ni coste.
        if name == "Eficiencia" and ws.cell(1, 18).value == "Tokens totales conocidos":
            ws.insert_cols(18, 4)
        for i, header in enumerate(headers, 1):
            cell = ws.cell(1, i, header)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="245B85")
            ws.column_dimensions[get_column_letter(i)].width = max(18, len(header)+3)
        ws.freeze_panes = "A2"
    ws = fuentes["Eficiencia"]
    ws.append([run["id"], run["date"], run["case"], run["reference"], run["model"], run["status"],
               *[round(totals[k], 3) for k in ("wall_seconds", "human_seconds", "process_seconds", "llm_seconds")],
               *[totals[k] for k in ("requests", "successful", "without_usage", "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens", "generation_input_tokens", "generation_output_tokens", "validation_input_tokens", "validation_output_tokens", "total_tokens", "cost_usd")]])
    ws.cell(ws.max_row, len(specs["Eficiencia"])).number_format = "0.000000"
    ws.auto_filter.ref = ws.dimensions
    ws = fuentes["Peticiones"]
    for i, r in enumerate(run["requests"], 1):
        ws.append([run["id"], i, r["model"], r["role"], r["phase"], r["unit"], r["attempt"], r["status"], round(r["seconds"], 3),
                   *[r[k] for k in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens", "total_tokens", "cost_usd")], r.get("error"), r["cache_cost_estimated"]])
        ws.cell(ws.max_row, 15).number_format = "0.000000"
    ws.auto_filter.ref = ws.dimensions
    opciones_vista = {
        "ocultar": {"ID ejecución", "Peticiones sin usage", "Tokens entrada sin caché", "Tokens salida", "Tokens escritura caché", "Tokens lectura caché"},
        "renombrar": {"Tokens totales conocidos": "Tokens totales",
                       "Coste estimado conocido (USD)": "Coste estimado (USD)"},
        "formatos": {"Coste estimado conocido (USD)": '0.00'},
    }
    vista_eficiencia = crear_vista_metricas(
        wb, fuentes["Eficiencia"], "Eficiencia", lambda v, n: f"Ejecución {n} · {v[3] or v[2]}",
        filtrar=lambda v: v[0] == run["id"], **opciones_vista)
    # Se conserva el detalle interno de llamadas, sin exponer una pestaña adicional.
    fuentes["Peticiones"].sheet_state = "hidden"
    if "Peticiones" in wb.sheetnames:
        wb.remove(wb["Peticiones"])
    _actualizar_costes_calidad(wb, run, totals)
    if "Dashboard" in wb.sheetnames:
        from copy import copy
        dash = wb["Dashboard"]
        etiquetas_coste = {"Coste estimado (USD)", "Coste parcial (USD)"}
        fila = next((r for r in range(3, 10) if dash.cell(r, 1).value in etiquetas_coste), None)
        if fila is None:
            fila = next(r for r in range(3, 10) if dash.cell(r, 1).value is None)
        etiqueta = dash.cell(fila, 1)
        valor = dash.cell(fila, 2)
        etiqueta.value = "Coste parcial (USD)" if totals["without_usage"] else "Coste estimado (USD)"
        valor.value = totals["cost_usd"]
        etiqueta._style = copy(dash.cell(3, 1)._style)
        valor._style = copy(dash.cell(3, 2)._style)
        valor.number_format = '"$"#,##0.00'
    if wb.active is None or wb.active.sheet_state != "visible":
        wb.active = vista_eficiencia
    # Guardado atómico: no deja el histórico a medias si falla la escritura.
    staging = path.with_name(f".{path.stem}.{uuid4().hex}.xlsx")
    try:
        wb.save(staging)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    return path


def _formatear_tiempo(cs):
    """Muestra segundos o minutos y segundos conservando dos decimales."""
    minutos, resto = divmod(cs, 6000)
    return f"{minutos} min {resto / 100:.2f} s" if minutos else f"{resto / 100:.2f} s"


def medir_ejecucion(case):
    """Mide una ejecución completa, incluso si se cancela o falla."""
    def decorate(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if _run.get() is not None:
                return func(*args, **kwargs)
            reference = str(args[0]) if args else str(kwargs.get("hu_number", ""))
            if case == "OpenAPI YAML":
                reference = "payments.yaml"
            run = {"id": uuid4().hex, "date": datetime.now().isoformat(timespec="seconds"),
                   "case": case, "reference": reference,
                   "model": configured_model(), "status": "error", "human_seconds": 0.0,
                   "requests": [], "phase_seconds": {}}
            token = _run.set(run)
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                run["status"] = (result.get("status", "completado") if isinstance(result, dict)
                                 else "completado" if result else "sin resultado")
                return result
            except KeyboardInterrupt:
                run["status"] = "cancelado"
                raise
            finally:
                totals = summarize(run, time.perf_counter()-start)
                _run.reset(token)
                print(f"\n{'='*52}")
                print(f"   📊 Resumen de tiempos y eficiencia")
                print(f"   {case} | {run['model']}")
                fases = [(etiqueta, round(run["phase_seconds"][fase] * 100))
                         for fase, etiqueta in (("Ingesta", "Ingesta"), ("Generacion", "Generación"),
                                                ("Validacion", "Validación"), ("Ejecucion", "Ejecución"))
                         if fase in run["phase_seconds"]]
                total_cs = round(totals['wall_seconds'] * 100)
                otros_cs = total_cs - sum(cs for _, cs in fases)
                # Evita discrepancias de un céntimo por el redondeo de cada fase.
                if otros_cs < 0 and fases:
                    idx = max(range(len(fases)), key=lambda i: fases[i][1])
                    etiqueta, cs = fases[idx]
                    fases[idx] = (etiqueta, cs + otros_cs)
                    otros_cs = 0
                for etiqueta, cs in fases:
                    print(f"   {etiqueta:<20}: {_formatear_tiempo(cs)}")
                # Incluye inicialización, decisiones entre fases y trabajo no clasificado.
                if otros_cs:
                    print(f"   {'Otros tiempos':<20}: {_formatear_tiempo(otros_cs)}")
                print(f"   {'Tiempo total':<20}: {_formatear_tiempo(total_cs)}")
                print(f"   {'Peticiones API':<20}: {totals['requests']}")
                print(f"   {'Tokens totales':<20}: {totals['total_tokens']:,}")
                print(f"   {'Coste estimado':<20}: ${totals['cost_usd']:.2f} USD")
                if totals['without_usage']:
                    print(f"   ⚠️ {totals['without_usage']} petición(es) sin usage: tokens/coste son parciales.")
                try:
                    print(f"   Excel: {save_metrics(run, totals)}")
                except Exception:
                    logger.exception("No se pudo guardar el Excel de eficiencia")
                print('='*52)
        return wrapper
    return decorate
