# Genera metricas (Excel) y evidencias basicas a partir del resultado del AgentValidator
import json
import os
import re
import sys
import shutil
import tempfile
import logging
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# openpyxl y docx se cargan de forma lazy en _init_xl() para no penalizar el arranque

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")
HU_METRICS_FILE = OUTPUT_DIR / "metricas_HU.xlsx"
OPENAPI_METRICS_FILE = OUTPUT_DIR / "metricas_OpenAPI.xlsx"
METRICS_FILE = HU_METRICS_FILE  # alias de compatibilidad
EVIDENCIAS_DIR = OUTPUT_DIR / "evidencias"

CABECERAS_OPENAPI = [
    "Fecha",
    "Titulo",
    "Version",
    "Nº endpoints",
    "Endpoints",
    "Codigos de respuesta",
    "Validacion estatica (valido)",
    "Errores estaticos",
    "Warnings estaticos",
    "Validacion semantica (valido)",
    "Coverage score",
    "Semantic score",
    "Sugerencias de mejora",
    "Valido global",
    "Score global",
]

# Cabeceras del Excel: reflejan la informacion que devuelve el AgentValidator
CABECERAS = [
    "Fecha",
    "HU",
    "Asunto",
    # --- Validacion 1: Sintactica ---
    "Sintactica (valido)",
    "N escenarios",
    "Errores sintacticos",
    "Warnings sintacticos",
    # --- Validacion 2: Cohesion ---
    "Cohesion (valido)",
    "Steps sin definir",
    "Warnings cohesion",
    # --- Validacion 3: Tecnica ---
    "Tecnica (valido)",
    "Errores tecnicos",
    "Warnings tecnicos",
    # --- Validacion 4: Funcional ---
    "Funcional (valido)",
    "Coverage score",
    "Semantic score",
    "N criterios HU",
    "Criterios cubiertos",
    "Criterios sin cubrir",
    "Escenarios detectados",
    "Escenarios faltantes",
    "Errores funcionales",
    "Warnings funcionales",
    "Sugerencias de mejora",
    # --- Global ---
    "Valido global",
    "Score global",
    "Feature",
    # --- Ejecucion ---
    "Escenarios ejecutados",
    "Passed",
    "Failed",
    "% Passed",
    "% Failed",
    # --- Reporte ---
    "Evidencias generadas",
]


def _join(items) -> str:
    """Convierte una lista en un texto legible para una celda."""
    if isinstance(items, (list, tuple)):
        return "; ".join(str(i) for i in items)
    return str(items) if items else ""


def _fila_desde_validacion(hu: dict, validation_result: dict, feature_path, ejecucion: dict = None, evidencias_path=None) -> list:
    """Construye la fila del Excel con los datos de las 4 validaciones y la ejecucion."""
    vals = validation_result.get("validaciones", {}) or {}
    sint = vals.get("sintactica", {}) or {}
    cohe = vals.get("cohesion", {}) or {}
    tecn = vals.get("tecnica", {}) or {}
    func = vals.get("funcional", {}) or {}

    # Fallback: si viene el formato antiguo con static_validation
    if not sint and not cohe and not tecn:
        estatica = validation_result.get("static_validation", {}) or {}
        cats = estatica.get("categorias", {}) or {}
        sint = cats.get("sintactica", estatica)
        cohe = cats.get("cohesion", {})
        tecn = cats.get("tecnica", {})
    if not func:
        func = validation_result.get("semantic_validation", {}) or {}

    ejecucion = ejecucion or {}
    total = ejecucion.get("total", 0)
    passed = ejecucion.get("passed", 0)
    failed = ejecucion.get("failed", 0) + ejecucion.get("errors", 0)
    pct_passed = round(passed / total * 100, 2) if total else 0
    pct_failed = round(failed / total * 100, 2) if total else 0

    return [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        f"HU-{hu.get('ref', 'UNKNOWN')}" if str(hu.get('ref', '')).isdigit() else str(hu.get('ref', 'UNKNOWN')),
        hu.get("subject", ""),
        # Validacion 1: Sintactica
        "SI" if sint.get("valid") else "NO",
        sint.get("scenario_count", 0),
        _join(sint.get("errors", [])),
        _join(sint.get("warnings", [])),
        # Validacion 2: Cohesion
        "SI" if cohe.get("valid") else "NO",
        _join(cohe.get("steps_sin_definir", cohe.get("errors", []))),
        _join(cohe.get("warnings", [])),
        # Validacion 3: Tecnica
        "SI" if tecn.get("valid") else "NO",
        _join(tecn.get("errors", [])),
        _join(tecn.get("warnings", [])),
        # Validacion 4: Funcional
        "SI" if func.get("valid") else "NO",
        func.get("coverage_score", 0),
        func.get("semantic_score", 0),
        func.get("total_criterios", ""),
        _join(func.get("criterios_cubiertos", [])),
        _join(func.get("criterios_sin_cubrir", [])),
        _join(func.get("escenarios_detectados", [])),
        _join(func.get("escenarios_faltantes", [])),
        _join(func.get("errors", [])),
        _join(func.get("warnings", [])),
        _join(func.get("improvement_suggestions", [])),
        # Global
        "SI" if validation_result.get("overall_valid") else "NO",
        validation_result.get("overall_score", 0),
        str(feature_path) if feature_path else "",
        # Ejecucion
        total,
        passed,
        failed,
        pct_passed,
        pct_failed,
        # Reporte
        "SI" if evidencias_path else "NO",
    ]


# Indices de columna (1-based) segun CABECERAS, para estilos y formato condicional
# Col 4=Sintactica, 8=Cohesion, 11=Tecnica, 14=Funcional, 25=Global
# Col 15=Coverage, 16=Semantic, 26=Score global
# Col 28=Escenarios ejecutados, 29=Passed, 30=Failed, 31=% Passed, 32=% Failed, 33=Evidencias
COL_HU = 2
COL_VALIDOS = (4, 8, 11, 14, 25, 33)  # sint / cohe / tecn / func / global / evidencias (SI/NO)
COL_SCORES = (15, 16, 26)           # coverage / semantic / score global (0..1)
COL_PCT = (31, 32)                  # % passed / % failed (0..100)
COL_PASSED, COL_FAILED = 29, 30
COL_EVIDENCIAS = 33                 # evidencias generadas (SI/NO)
COL_CENTRADAS = (4, 5, 8, 11, 14, 15, 16, 17, 25, 26, 28, 29, 30, 31, 32, 33)

_THIN = None
_BORDER = None
_XL_INIT = False


def _init_xl():
    """Carga lazy de openpyxl y python-docx. Se ejecuta solo la primera vez."""
    global _XL_INIT, _THIN, _BORDER, _COLOR_OK, _COLOR_KO, _COLOR_SKIP
    global Workbook, load_workbook, Document, Pt, RGBColor
    global BarChart, PieChart, Reference, DataLabelList
    global Font, PatternFill, Alignment, Border, Side
    global ColorScaleRule, CellIsRule, get_column_letter
    if _XL_INIT:
        return
    from openpyxl import Workbook, load_workbook
    from openpyxl.chart import BarChart, PieChart, Reference
    from openpyxl.chart.label import DataLabelList
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.formatting.rule import ColorScaleRule, CellIsRule
    from openpyxl.utils import get_column_letter
    from docx import Document
    from docx.shared import Pt, RGBColor
    _THIN = Side(style="thin", color="D9D9D9")
    _BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
    _COLOR_OK   = RGBColor(0x1E, 0x7A, 0x1E)
    _COLOR_KO   = RGBColor(0xC0, 0x00, 0x00)
    _COLOR_SKIP = RGBColor(0x80, 0x80, 0x80)
    _XL_INIT = True


def _estilizar_cabecera(ws):
    """Aplica estilo a la fila de cabecera y ajusta el ancho de las columnas."""
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(bold=True, color="FFFFFF", size=9)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _BORDER
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "C2"
    anchos = {6: 18, 7: 18, 9: 18, 10: 18, 12: 18, 13: 18, 18: 20, 19: 20, 20: 20, 21: 20, 22: 18, 23: 18, 24: 20}
    for col in ws.columns:
        idx = col[0].column
        if idx in anchos:
            ws.column_dimensions[get_column_letter(idx)].width = anchos[idx]
            continue
        longitud = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(idx)].width = min(max(longitud + 1, 8), 16)


def _estilizar_filas(ws):
    """Aplica zebra, bordes, alineacion y formatos de numero a las filas de datos."""
    zebra = PatternFill("solid", fgColor="F2F6FB")
    data_font = Font(size=8)
    for fila in range(2, ws.max_row + 1):
        ws.row_dimensions[fila].height = 14
        for col in range(1, len(CABECERAS) + 1):
            celda = ws.cell(row=fila, column=col)
            celda.border = _BORDER
            celda.font = data_font
            celda.alignment = Alignment(
                horizontal="center" if col in COL_CENTRADAS else "left",
                vertical="center",
                wrap_text=False,
            )
            if fila % 2 == 0:
                celda.fill = zebra
        for col in COL_SCORES:
            ws.cell(row=fila, column=col).number_format = "0%"
        for col in COL_PCT:
            ws.cell(row=fila, column=col).number_format = '0.0"%"'


def hoja_datos_metricas(wb, nombre, nombre_datos, cabeceras):
    """Mantiene la tabla original como fuente interna, migrando históricos existentes."""
    if nombre_datos in wb.sheetnames:
        return wb[nombre_datos]
    if nombre in wb.sheetnames:
        ws = wb[nombre]
        if ws.cell(1, 1).value not in (None, cabeceras[0]):
            raise ValueError(f"No se puede migrar la hoja {nombre}: cabecera desconocida")
        ws.title = nombre_datos
    else:
        ws = wb.create_sheet(nombre_datos)
    if ws.cell(1, 1).value is None:
        for i, titulo in enumerate(cabeceras, 1):
            ws.cell(1, i, titulo)
    return ws


def crear_vista_metricas(wb, fuente, nombre, identificar, *, ocultar=(), renombrar=None, sumar=None, formatos=None, filtrar=None):
    """Presenta cada registro en una columna y cada métrica en una fila."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    import math

    indice = wb.index(wb[nombre]) if nombre in wb.sheetnames else wb.index(fuente)
    if nombre in wb.sheetnames:
        wb.remove(wb[nombre])
    vista = wb.create_sheet(nombre, indice)
    azul = PatternFill("solid", fgColor="1F4E78")
    claro = PatternFill("solid", fgColor="E8F0F7")
    verde = PatternFill("solid", fgColor="C6EFCE")
    rojo = PatternFill("solid", fgColor="FFC7CE")
    renombrar = renombrar or {}
    sumar = sumar or {}
    formatos = formatos or {}
    cabeceras = [celda.value for celda in fuente[1]]
    visibles = [(i, titulo) for i, titulo in enumerate(cabeceras) if titulo not in ocultar]
    vista.cell(1, 1, "Métrica")
    for idx, (_, titulo) in enumerate(visibles, 2):
        vista.cell(idx, 1, renombrar.get(titulo, titulo))
    vista.column_dimensions['A'].width = 37
    filas = (registros for registros in fuente.iter_rows(min_row=2)
             if filtrar is None or filtrar([celda.value for celda in registros]))
    for c, registros in enumerate(filas, 2):
        valores = [celda.value for celda in registros]
        if not any(v is not None for v in valores):
            continue
        vista.cell(1, c, identificar(valores, c - 1))
        vista.column_dimensions[get_column_letter(c)].width = 40
        for r, (i, titulo) in enumerate(visibles, 2):
            original = registros[i]
            valor = original.value
            if titulo in sumar and valor is not None:
                valor += sum(valores[cabeceras.index(extra)] or 0 for extra in sumar[titulo])
            celda = vista.cell(r, c, valor)
            celda.number_format = formatos.get(titulo, original.number_format)
            celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            celda.font = Font(size=10)
            if original.value == "SI":
                celda.fill = verde
            elif original.value == "NO":
                celda.fill = rojo
            elif r % 2 == 0:
                celda.fill = claro
            if isinstance(original.value, str):
                lineas = sum(max(1, math.ceil(len(linea) / 37)) for linea in original.value.split('\n'))
                vista.row_dimensions[r].height = max(vista.row_dimensions[r].height or 18, min(180, 15 * lineas + 4))
    for celda in vista[1]:
        celda.fill = azul
        celda.font = Font(bold=True, color="FFFFFF", size=11)
        celda.alignment = Alignment(horizontal="left" if celda.column == 1 else "center", vertical="center", wrap_text=True)
    vista.row_dimensions[1].height = 32
    for r in range(2, vista.max_row + 1):
        celda = vista.cell(r, 1)
        celda.font = Font(bold=True, size=10)
        celda.fill = claro
        celda.alignment = Alignment(vertical="top", wrap_text=True)
        vista.row_dimensions[r].height = max(vista.row_dimensions[r].height or 18, 18)
    vista.freeze_panes = "B2"
    vista.sheet_view.showGridLines = False
    fuente.sheet_state = "hidden"
    return vista


def _formato_condicional(ws):
    """Colorea celdas de validez (SI/NO) y aplica escalas de color a scores y %."""
    ultima = ws.max_row
    if ultima < 2:
        return
    verde = PatternFill("solid", fgColor="C6EFCE")
    rojo = PatternFill("solid", fgColor="FFC7CE")
    font_verde = Font(color="006100", bold=True)
    font_rojo = Font(color="9C0006", bold=True)

    for col in COL_VALIDOS:
        letra = get_column_letter(col)
        rango = f"{letra}2:{letra}{ultima}"
        ws.conditional_formatting.add(
            rango, CellIsRule(operator="equal", formula=['"SI"'], fill=verde, font=font_verde))
        ws.conditional_formatting.add(
            rango, CellIsRule(operator="equal", formula=['"NO"'], fill=rojo, font=font_rojo))

    # Escala de color para scores (0..1) y % passed
    escala_buena = lambda: ColorScaleRule(
        start_type="num", start_value=0, start_color="F8696B",
        mid_type="num", mid_value=0.5, mid_color="FFEB84",
        end_type="num", end_value=1, end_color="63BE7B")
    for col in COL_SCORES:
        letra = get_column_letter(col)
        ws.conditional_formatting.add(f"{letra}2:{letra}{ultima}", escala_buena())

    letra_p = get_column_letter(COL_PCT[0])
    ws.conditional_formatting.add(f"{letra_p}2:{letra_p}{ultima}", ColorScaleRule(
        start_type="num", start_value=0, start_color="F8696B",
        mid_type="num", mid_value=50, mid_color="FFEB84",
        end_type="num", end_value=100, end_color="63BE7B"))
    letra_f = get_column_letter(COL_PCT[1])
    ws.conditional_formatting.add(f"{letra_f}2:{letra_f}{ultima}", ColorScaleRule(
        start_type="num", start_value=0, start_color="63BE7B",
        mid_type="num", mid_value=50, mid_color="FFEB84",
        end_type="num", end_value=100, end_color="F8696B"))
    # Rojo solido en % Failed cuando > 0 (prevalece sobre la escala)
    ws.conditional_formatting.add(
        f"{letra_f}2:{letra_f}{ultima}",
        CellIsRule(operator="greaterThan", formula=["0"], fill=rojo, font=font_rojo))

    # Rojo en columna Failed cuando > 0
    letra_failed = get_column_letter(COL_FAILED)
    ws.conditional_formatting.add(
        f"{letra_failed}2:{letra_failed}{ultima}",
        CellIsRule(operator="greaterThan", formula=["0"], fill=rojo, font=font_rojo))


def _crear_dashboard(wb, ws):
    """Crea (o recrea) una hoja 'Dashboard' con KPIs y graficos resumen."""
    if ws.max_row < 2:
        return
    if "Dashboard" in wb.sheetnames:
        wb.remove(wb["Dashboard"])
    dash = wb.create_sheet("Dashboard", 0)

    import re as _re
    # Auto-detectar flujo OpenAPI: todas las HUs tienen formato METHOD_path
    _metodos_http = {"GET", "POST", "PUT", "DELETE", "PATCH"}
    def _cel_es_endpoint(val):
        s = str(val or "")
        return any(s.startswith(m + "_") or s.startswith("HU-" + m + "_") for m in _metodos_http)
    _es_openapi = ws.max_row > 1 and all(
        _cel_es_endpoint(ws.cell(row=r, column=COL_HU).value)
        for r in range(2, ws.max_row + 1)
    )
    _label_items = "Endpoints" if _es_openapi else "HUs"
    _label_item = "Endpoint" if _es_openapi else "HU"

    n = ws.max_row - 1
    def _to_num(v, t=float): 
        try: return t(v) if v not in (None, "") else 0
        except (ValueError, TypeError): return 0
    total_passed = sum(_to_num(ws.cell(row=r, column=COL_PASSED).value, int) for r in range(2, ws.max_row + 1))
    total_failed = sum(_to_num(ws.cell(row=r, column=COL_FAILED).value, int) for r in range(2, ws.max_row + 1))
    scores = [_to_num(ws.cell(row=r, column=26).value) for r in range(2, ws.max_row + 1)]
    cobertura = [_to_num(ws.cell(row=r, column=15).value) for r in range(2, ws.max_row + 1)]
    avg_score = round(sum(scores) / n, 3) if n else 0
    avg_cov = round(sum(cobertura) / n, 3) if n else 0
    total_tests = total_passed + total_failed
    _hay_ejecucion = total_tests > 0

    # KPIs
    titulo = dash.cell(row=1, column=1, value="Resumen de validacion y ejecucion")
    titulo.font = Font(bold=True, size=14, color="1F4E78")
    kpis = [
        (f"{_label_items} procesados", n, "0"),
        ("Score global medio", avg_score, "0%"),
        ("Cobertura media", avg_cov, "0%"),
    ]
    if _hay_ejecucion:
        kpis += [
            ("Escenarios passed", total_passed, "0"),
            ("Escenarios failed", total_failed, "0"),
            ("% global passed", (total_passed / total_tests * 100) if total_tests else 0, '0.0"%"'),
        ]
    for i, (nombre, valor, fmt) in enumerate(kpis):
        fila = 3 + i
        c_nombre = dash.cell(row=fila, column=1, value=nombre)
        c_nombre.font = Font(bold=True)
        c_nombre.fill = PatternFill("solid", fgColor="DDEBF7")
        c_nombre.border = _BORDER
        c_valor = dash.cell(row=fila, column=2, value=valor)
        c_valor.number_format = fmt
        c_valor.alignment = Alignment(horizontal="center")
        c_valor.border = _BORDER
    dash.column_dimensions["A"].width = 24
    dash.column_dimensions["B"].width = 14
    dash.column_dimensions["C"].width = 4  # separador

    # Grafico de barras Passed vs Failed por Endpoint/HU
    barras = BarChart()
    barras.type = "col"
    barras.style = 10
    barras.title = f"Passed vs Failed por {_label_item}"
    barras.y_axis.title = "Escenarios"
    barras.x_axis.title = _label_item
    datos = Reference(ws, min_col=COL_PASSED, max_col=COL_FAILED, min_row=1, max_row=ws.max_row)
    categorias = Reference(ws, min_col=COL_HU, min_row=2, max_row=ws.max_row)
    barras.add_data(datos, titles_from_data=True)
    barras.set_categories(categorias)
    barras.visible_cells_only = False  # La fuente tabular interna queda oculta.
    barras.height = 10
    barras.width = 18
    dash.add_chart(barras, "D2")

    if _hay_ejecucion:
        # Datos auxiliares para el pie — fila 14-15, debajo de los KPIs y fuera del gráfico de barras
        _fila_aux = 14
        dash.cell(row=_fila_aux, column=1, value="Passed").font = Font(bold=True)
        dash.cell(row=_fila_aux, column=2, value=total_passed)
        dash.cell(row=_fila_aux + 1, column=1, value="Failed").font = Font(bold=True)
        dash.cell(row=_fila_aux + 1, column=2, value=total_failed)

        # Grafico circular a la derecha del gráfico de barras, en la misma fila.
        pie = PieChart()
        pie.title = "Total Passed vs Failed"
        etiquetas = Reference(dash, min_col=1, min_row=_fila_aux, max_row=_fila_aux + 1)
        valores = Reference(dash, min_col=2, min_row=_fila_aux, max_row=_fila_aux + 1)
        pie.add_data(valores, titles_from_data=False)
        pie.set_categories(etiquetas)
        pie.dataLabels = DataLabelList()
        pie.dataLabels.showPercent = True
        pie.dataLabels.showSerName = False
        pie.dataLabels.showCatName = True
        pie.height = 10
        pie.width = 12
        dash.add_chart(pie, "P2")


def _crear_hoja_evaluacion(wb, ws):
    """Crea (o recrea) la hoja 'Evaluacion' con las 12 metricas de la rubrica y sus valores reales."""
    if ws.max_row < 2:
        return
    if "Evaluacion" in wb.sheetnames:
        wb.remove(wb["Evaluacion"])
    ev = wb.create_sheet("Evaluacion")

    def _to_num(v, t=float):
        try: return t(v) if v not in (None, "") else 0
        except (ValueError, TypeError): return 0

    n = ws.max_row - 1  # numero de filas de datos

    # Indices de columna (1-based) segun CABECERAS
    C_SINT   = 4   # Sintactica (valido)
    C_NESCS  = 5   # N escenarios
    C_COHE   = 8   # Cohesion (valido)
    C_TECN   = 11  # Tecnica (valido)
    C_COV    = 15  # Coverage score
    C_SCORE  = 26  # Score global
    C_SUGS   = 24  # Sugerencias de mejora
    C_EXEC   = 28  # Escenarios ejecutados
    C_PASS   = 29  # Passed
    C_FAIL   = 30  # Failed
    C_PCTOK  = 31  # % Passed
    C_EVID   = 33  # Evidencias generadas

    def _col_vals(col):
        return [ws.cell(row=r, column=col).value for r in range(2, ws.max_row + 1)]

    def _pct_si(col):
        vals = _col_vals(col)
        si = sum(1 for v in vals if str(v or "").strip().upper() == "SI")
        return round(si / n, 3) if n else 0

    total_escs = sum(_to_num(v, int) for v in _col_vals(C_NESCS))
    pct_sint   = _pct_si(C_SINT)
    pct_cohe   = _pct_si(C_COHE)
    pct_tecn   = _pct_si(C_TECN)
    avg_cov    = round(sum(_to_num(v) for v in _col_vals(C_COV)) / n, 3) if n else 0
    avg_score  = round(sum(_to_num(v) for v in _col_vals(C_SCORE)) / n, 3) if n else 0
    sugs_count = sum(1 for v in _col_vals(C_SUGS) if v and str(v).strip())
    total_exec = sum(_to_num(v, int) for v in _col_vals(C_EXEC))
    total_pass = sum(_to_num(v, int) for v in _col_vals(C_PASS))
    total_fail = sum(_to_num(v, int) for v in _col_vals(C_FAIL))
    pct_ok     = round(total_pass / total_exec, 3) if total_exec else None
    pct_evid   = _pct_si(C_EVID)

    # Definicion de las 12 metricas de la rubrica
    # Tupla: (categoria, nombre, tipo, valor, umbral, resumen_txt, invertido)
    # invertido=True -> OK cuando valor <= umbral (ej: fallidos: OK si == 0)
    METRICAS = [
        ("Validacion", "Cantidad de escenarios generados",      "Numerico",   total_escs,  1,    f"{total_escs} escenarios",                                        False),
        ("Validacion", "Validacion sintactica del .feature",    "Booleano",   pct_sint,    0.8,  f"{pct_sint:.0%} HUs/Endpoints correctos",                         False),
        ("Validacion", "Coherencia entre escenarios y steps",   "Booleano",   pct_cohe,    0.8,  f"{pct_cohe:.0%} HUs/Endpoints correctos",                         False),
        ("Validacion", "Coherencia con especificaciones OpenAPI","Booleano",  pct_tecn,    0.8,  f"{pct_tecn:.0%} HUs/Endpoints correctos",                         False),
        ("Validacion", "Cobertura funcional",                   "Porcentual", avg_cov,     0.7,  f"{avg_cov:.0%} media",                                             False),
        ("Validacion", "Score general de validacion",           "Numerico",   avg_score,   0.7,  f"{avg_score:.0%} medio",                                          False),
        ("Refinamiento","Feedback generado",                    "Texto",      sugs_count,  0,    f"{sugs_count} HU/Endpoints con sugerencias" if sugs_count > 0 else "Sin sugerencias",  False),
        ("Ejecucion",  "Numero de escenarios ejecutados",       "Numerico",   total_exec,  1,    f"{total_exec} ejecutados",                                        False),
        ("Ejecucion",  "Numero de escenarios pasados",          "Numerico",   total_pass,  1,    f"{total_pass} pasados",                                           False),
        ("Ejecucion",  "Numero de escenarios fallidos",         "Numerico",   total_fail,  0,    f"{total_fail} fallidos",                                          True),
        ("Ejecucion",  "Porcentaje de exito de la ejecucion",   "Porcentual", pct_ok,      0.9,  f"{pct_ok:.0%}" if pct_ok is not None else "Sin ejecutar",          False),
        ("Reporte",    "Generacion de evidencias Word",         "Booleano",   pct_evid,    1.0,  f"{pct_evid:.0%} generadas",                                       False),
    ]

    # Estilos
    _fill_titulo = PatternFill("solid", fgColor="1F4E78")
    _fill_val    = PatternFill("solid", fgColor="E2EFDA")
    _fill_ref    = PatternFill("solid", fgColor="FFF2CC")
    _fill_exec   = PatternFill("solid", fgColor="DDEBF7")
    _fill_rep    = PatternFill("solid", fgColor="F4CCCC")
    _fill_ok     = PatternFill("solid", fgColor="C6EFCE")
    _fill_ko     = PatternFill("solid", fgColor="FFC7CE")
    _fill_na     = PatternFill("solid", fgColor="F2F2F2")
    _cat_fills   = {"Validacion": _fill_val, "Refinamiento": _fill_ref, "Ejecucion": _fill_exec, "Reporte": _fill_rep}
    _font_hdr    = Font(bold=True, color="FFFFFF", size=10)
    _font_bold   = Font(bold=True, size=9)
    _font_normal = Font(size=9)

    # Cabecera
    cabeceras = ["Categoria", "Metrica de evaluacion", "Tipo", "Valor resumen", "Estado"]
    for c, txt in enumerate(cabeceras, 1):
        cell = ev.cell(row=1, column=c, value=txt)
        cell.fill = _fill_titulo
        cell.font = _font_hdr
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _BORDER
    ev.row_dimensions[1].height = 20

    # Filas de datos
    for i, (cat, nombre, tipo, valor, umbral, resumen_txt, invertido) in enumerate(METRICAS):
        fila = 2 + i
        _cat_fill = _cat_fills.get(cat, _fill_na)

        # Estado: OK / KO  (invertido=True -> OK cuando valor <= umbral)
        if not isinstance(valor, (int, float)):
            estado = "N/A"
            _est_fill = _fill_na
        elif invertido:
            estado = "OK" if valor <= umbral else "KO"
            _est_fill = _fill_ok if valor <= umbral else _fill_ko
        elif valor >= umbral:
            estado = "OK"
            _est_fill = _fill_ok
        else:
            estado = "KO"
            _est_fill = _fill_ko

        valores_celdas = [cat, nombre, tipo, resumen_txt, estado]
        for c, v in enumerate(valores_celdas, 1):
            cell = ev.cell(row=fila, column=c, value=v)
            cell.font = _font_bold if c == 2 else _font_normal
            cell.alignment = Alignment(horizontal="left" if c == 2 else "center", vertical="center", wrap_text=(c == 2))
            cell.border = _BORDER
            if c == 1:
                cell.fill = _cat_fill
            elif c == 5:
                cell.fill = _est_fill

    # Anchos de columna
    for col, ancho in [(1, 14), (2, 40), (3, 13), (4, 30), (5, 10)]:
        ev.column_dimensions[get_column_letter(col)].width = ancho
    ev.freeze_panes = "A2"


def reiniciar_metricas_validacion(filepath: Path):
    """Conserva el histórico LLM; reinicia sólo el informe de calidad de la ejecución."""
    if not filepath.exists():
        return
    _init_xl()
    wb = load_workbook(filepath)
    # El comportamiento anterior borraba todo el archivo. Ahora conserva Eficiencia,
    # Peticiones y cualquier otra hoja no gestionada por el informe de validación.
    for name in ("Metricas validacion", "Datos validacion", "Dashboard", "Evaluacion"):
        if name in wb.sheetnames:
            wb.remove(wb[name])
    ws = wb.create_sheet("Datos validacion", 0)
    ws.append(CABECERAS)
    vista = crear_vista_metricas(wb, ws, "Metricas validacion", lambda v, n: str(v[1] or f"HU {n}"))
    wb.active = vista
    wb.save(filepath)
    wb.close()


def _guardar_metricas_excel(fila: list, filepath: Path = None):
    """Crea o actualiza el Excel anadiendo una fila de metricas, con estilo y graficos."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = filepath or HU_METRICS_FILE

    if filepath.exists():
        wb = load_workbook(filepath)
    else:
        wb = Workbook()
        wb.active.title = "Datos validacion"
        wb.active.append(CABECERAS)
    ws = hoja_datos_metricas(wb, "Metricas validacion", "Datos validacion", CABECERAS)

    # Sobreescribir fila si la HU (col 2) ya existe; si no, añadir al final
    # Normalizar: ignorar prefijo "HU-" para que "POST_payments" == "HU-POST_payments"
    def _norm_hu(v):
        s = str(v or "").strip()
        return s[3:] if s.upper().startswith("HU-") else s
    hu_valor = fila[1] if len(fila) > 1 else None
    hu_norm = _norm_hu(hu_valor) if hu_valor is not None else None
    fila_existente = None
    if hu_norm is not None:
        for row in ws.iter_rows(min_row=2):
            if _norm_hu(row[1].value) == hu_norm:
                fila_existente = row
                break
    if fila_existente is not None:
        for idx, celda in enumerate(fila_existente):
            celda.value = fila[idx] if idx < len(fila) else None
    else:
        ws.append(fila)
    _estilizar_cabecera(ws)
    _estilizar_filas(ws)
    _formato_condicional(ws)
    _crear_dashboard(wb, ws)
    _crear_hoja_evaluacion(wb, ws)
    vista = crear_vista_metricas(wb, ws, "Metricas validacion", lambda v, n: str(v[1] or f"HU {n}"))
    wb.active = vista
    wb.save(filepath)
    logger.info(f"Metricas guardadas en: {filepath}")


def _generar_evidencias(hu: dict, validation_result: dict, feature_path):
    """Prepara la carpeta de evidencias de la HU (solo contendra el Word de ejecucion)."""
    hu_id = hu.get("ref", "UNKNOWN")
    # Flujo HU: ref numerico -> carpeta HU{id}. Flujo YAML: ref string -> carpeta sin prefijo HU
    nombre_carpeta = f"HU{hu_id}" if str(hu_id).isdigit() else str(hu_id)
    carpeta = EVIDENCIAS_DIR / nombre_carpeta
    carpeta.mkdir(parents=True, exist_ok=True)
    return str(carpeta)


def _extraer_endpoints(data: dict) -> list:
    """Extrae la lista de endpoints (metodo, path, summary) del esquema OpenAPI."""
    endpoints = []
    for path, methods in (data.get("paths", {}) or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, info in methods.items():
            if method.upper() in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                summary = info.get("summary", "") if isinstance(info, dict) else ""
                endpoints.append({"method": method.upper(), "path": path, "summary": summary})
    return endpoints


def _formatear_codigos(response_codes: dict) -> str:
    """Formatea el mapa de codigos de respuesta por endpoint para una celda."""
    if not isinstance(response_codes, dict):
        return ""
    return "; ".join(f"{ep}: {','.join(cods)}" for ep, cods in response_codes.items())


def registrar_openapi(data: dict, validation_result: dict = None, yaml_path="payments.yaml") -> dict:
    """
    Genera evidencias basicas del esquema OpenAPI (endpoints.json, validacion JSON y copia del YAML).
    Las metricas por endpoint se registran en metricas.xlsx via registrar_resultado.

    Args:
        data: Esquema OpenAPI ya parseado (dict)
        validation_result: Resultado de AgentValidator.validate_openapi (opcional)
        yaml_path: Ruta del archivo YAML original (para copiarlo como evidencia)

    Returns:
        dict: Ruta de la carpeta de evidencias
    """
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        return {
            "metricas_excel": str(OPENAPI_METRICS_FILE),
        }
    except Exception as e:
        logger.error(f"Error al registrar evidencias OpenAPI: {str(e)}")
        return {}


def _extraer_filas_feature(feature_path: str) -> dict:
    """Devuelve {(scenario_name, row_idx): {col: valor, ...}} con todas las columnas de cada fila."""
    mapping = {}
    try:
        content = Path(feature_path).read_text(encoding="utf-8")
    except Exception:
        return mapping
    scenario_name = None
    in_examples = False
    headers: list = []
    row_count = 0
    for line in content.splitlines():
        stripped = line.strip()
        m = re.match(r"Scenario Outline:\s*(.*)", stripped)
        if m:
            scenario_name = m.group(1).strip()
            in_examples = False
            headers = []
            row_count = 0
            continue
        if re.match(r"Examples:\s*", stripped, re.IGNORECASE):
            in_examples = True
            headers = []
            row_count = 0
            continue
        if not in_examples or not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.split("|") if c.strip()]
        if not headers:
            headers = cells
        else:
            row_count += 1
            if scenario_name:
                mapping[(scenario_name, row_count)] = dict(zip(headers, cells))
    return mapping


def _extraer_ejemplos_feature(feature_path: str) -> dict:
    """Devuelve un dict {(scenario_name, row_idx): template_name} leyendo el feature."""
    mapping = {}
    try:
        content = Path(feature_path).read_text(encoding="utf-8")
    except Exception:
        return mapping
    scenario_name = None
    in_examples = False
    headers: list = []
    row_count = 0
    for line in content.splitlines():
        stripped = line.strip()
        m = re.match(r"Scenario Outline:\s*(.*)", stripped)
        if m:
            scenario_name = m.group(1).strip()
            in_examples = False
            headers = []
            row_count = 0
            continue
        if re.match(r"Examples:\s*", stripped, re.IGNORECASE):
            in_examples = True
            headers = []
            row_count = 0
            continue
        if not in_examples or not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.split("|") if c.strip()]
        if not headers:
            headers = [c.lower() for c in cells]
        else:
            row_count += 1
            if "template" in headers:
                t_idx = headers.index("template")
                if t_idx < len(cells) and scenario_name:
                    mapping[(scenario_name, row_count)] = cells[t_idx]
    return mapping


def _parsear_resultados_xml(xml_dir: Path) -> dict:
    """Lee los XML de resultados generados por behave y resume el resultado de la ejecucion."""
    total = passed = failed = errors = skipped = 0
    escenarios = []
    for xml_file in xml_dir.glob("*.xml"):
        try:
            root = ET.parse(xml_file).getroot()
        except ET.ParseError:
            continue
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for suite in suites:
            for case in suite.findall("testcase"):
                total += 1
                nodo = case.find("failure")
                if nodo is not None:
                    failed += 1
                    estado = "failed"
                else:
                    nodo = case.find("error")
                    if nodo is not None:
                        errors += 1
                        estado = "error"
                    elif case.find("skipped") is not None:
                        skipped += 1
                        estado = "skipped"
                    else:
                        passed += 1
                        estado = "passed"
                detalle = ""
                if nodo is not None:
                    mensaje = nodo.get("message", "") or ""
                    texto = (nodo.text or "").strip()
                    detalle = "\n".join(p for p in (mensaje, texto) if p).strip()
                escenarios.append({
                    "nombre": case.get("name", ""),
                    "estado": estado,
                    "detalle": detalle,
                })
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "escenarios": escenarios,
    }


def _parrafo_mono(doc, texto: str):
    """Anade un parrafo en fuente monoespaciada pequena."""
    parrafo = doc.add_paragraph(texto)
    for run in parrafo.runs:
        run.font.name = "Consolas"
        run.font.size = Pt(8)
    return parrafo


def _construir_curl(req: dict) -> str:
    """Construye el comando curl equivalente a la peticion HTTP capturada."""
    if not req:
        return ""
    metodo = (req.get("method") or "GET").upper()
    url = req.get("url", "")
    partes = [f"curl -X {metodo} '{url}'"]

    headers = req.get("headers") or {}
    # Cabeceras que no aportan al reproducir la peticion manualmente
    omitir = {"content-length", "host", "connection", "accept-encoding", "user-agent"}
    for nombre, valor in headers.items():
        if nombre.lower() in omitir:
            continue
        partes.append(f"-H '{nombre}: {valor}'")

    body = req.get("body")
    if body:
        if not isinstance(body, str):
            try:
                body = json.dumps(body, ensure_ascii=False)
            except (TypeError, ValueError):
                body = str(body)
        body = body.replace("'", "'\\''")
        partes.append(f"-d '{body}'")

    return " \\\n  ".join(partes)


_COLOR_OK   = None
_COLOR_KO   = None
_COLOR_SKIP = None


def _celda_color(celda, texto: str, color, negrita: bool = False):
    """Escribe texto con color y negrita opcional en una celda de tabla Word."""
    celda.text = ""
    run = celda.paragraphs[0].add_run(texto)
    run.font.color.rgb = color
    run.bold = negrita
    run.font.size = Pt(9)


def _generar_word_ejecucion(hu: dict, feature_path, resumen: dict, salida: str, destino: Path, llamadas: list = None, validation_result: dict = None):
    """Genera un documento Word con las evidencias de ejecucion de las pruebas."""
    llamadas = llamadas or []
    escenarios_bdd = resumen.get("escenarios") or []
    total   = resumen.get("total", 0)
    passed  = resumen.get("passed", 0)
    failed  = resumen.get("failed", 0) + resumen.get("errors", 0)
    skipped = resumen.get("skipped", 0)
    pct_ok  = round(passed / total * 100, 1) if total else 0

    doc = Document()
    doc.add_heading(f"Evidencias de ejecucion - HU-{hu.get('ref', 'UNKNOWN')}", level=1)
    doc.add_paragraph(f"Asunto: {hu.get('subject', '')}")
    doc.add_paragraph(f"Feature: {Path(feature_path).name}")
    doc.add_paragraph(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ── Resultado de validaciones (4 capas) ─────────────────────────────
    if validation_result:
        doc.add_heading("Validaciones", level=2)
        vals = (validation_result.get("validaciones") or {})
        capas = [
            ("1. Sintactica",  vals.get("sintactica", {})),
            ("2. Cohesion",    vals.get("cohesion", {})),
            ("3. Tecnica",     vals.get("tecnica", {})),
            ("4. Funcional",   vals.get("funcional", {})),
        ]
        tval = doc.add_table(rows=1, cols=3)
        tval.style = "Table Grid"
        hdr = tval.rows[0].cells
        for i, txt in enumerate(["Capa", "Resultado", "Detalle"]):
            hdr[i].text = txt
            hdr[i].paragraphs[0].runs[0].bold = True
            hdr[i].paragraphs[0].runs[0].font.size = Pt(9)
        for nombre, capa in capas:
            valido = capa.get("valid", None)
            if isinstance(capa.get("coverage_score"), float):
                pct = round(capa["coverage_score"] * 100)
                etiqueta = f"{pct}%"
                color = _COLOR_OK if pct >= 70 else _COLOR_KO
            elif valido is True:
                etiqueta, color = "OK", _COLOR_OK
            elif valido is False:
                etiqueta, color = "KO", _COLOR_KO
            else:
                etiqueta, color = "-", _COLOR_SKIP
            row = tval.add_row().cells
            row[0].text = nombre
            row[0].paragraphs[0].runs[0].font.size = Pt(9)
            _celda_color(row[1], etiqueta, color, negrita=True)
            errs  = capa.get("errors") or []
            warns = capa.get("warnings") or []
            sugs  = capa.get("improvement_suggestions") or []
            detalle_parts = []
            if errs:  detalle_parts.append("ERR: " + "; ".join(errs[:3]))
            if warns: detalle_parts.append("WARN: " + "; ".join(warns[:3]))
            if sugs:  detalle_parts.append("SUG: " + "; ".join(sugs[:2]))
            detalle_txt = " | ".join(detalle_parts)[:400]
            row[2].text = detalle_txt
            row[2].paragraphs[0].runs[0].font.size = Pt(8)
        score = validation_result.get("overall_score", 0)
        doc.add_paragraph(f"Score global: {round(score * 100, 1)}%  |  Valido: {'Si' if validation_result.get('overall_valid') else 'No'}")

    # ── Resumen global ──────────────────────────────────────────────────
    doc.add_heading("Resumen", level=2)
    p_res = doc.add_paragraph()
    def _run_res(txt, color):
        r = p_res.add_run(txt)
        r.bold = True
        r.font.color.rgb = color
        r.font.size = Pt(11)
    _run_res(f"OK: {passed}  ", _COLOR_OK)
    _run_res(f"KO: {failed}  ", _COLOR_KO if failed else _COLOR_OK)
    _run_res(f"SKIP: {skipped}  ", _COLOR_SKIP)
    _run_res(f"TOTAL: {total}  ({pct_ok}% OK)", RGBColor(0x00, 0x00, 0x00))

    # Cargar mapping template->fila para nombres legibles y datos completos de cada fila
    try:
        _ej_map = _extraer_ejemplos_feature(str(feature_path))
    except Exception:
        _ej_map = {}
    try:
        _filas_map = _extraer_filas_feature(str(feature_path))
    except Exception:
        _filas_map = {}

    def _display_nombre(nombre_raw: str) -> str:
        m_r = re.match(r"^(.*?)\s*--\s*@(\d+)\.(\d+)(.*?)$", nombre_raw)
        if m_r:
            base = m_r.group(1).strip()
            ridx = int(m_r.group(3))
            tpl  = _ej_map.get((base, ridx), "")
            return f"{base}: {tpl}" if tpl else base
        return nombre_raw

    # ── Tabla de escenarios ─────────────────────────────────────────────
    doc.add_heading("Resultado por escenario", level=2)
    if escenarios_bdd:
        tabla = doc.add_table(rows=1, cols=3)
        tabla.style = "Table Grid"
        hdr = tabla.rows[0].cells
        for i, txt in enumerate(("Escenario", "Resultado", "Detalle")):
            hdr[i].text = txt
            hdr[i].paragraphs[0].runs[0].bold = True
            hdr[i].paragraphs[0].runs[0].font.size = Pt(9)
        for esc in escenarios_bdd:
            estado = esc.get("estado", "unknown")
            if estado == "passed":
                etiqueta, color = "OK", _COLOR_OK
            elif estado in ("failed", "error"):
                etiqueta, color = "KO", _COLOR_KO
            else:
                etiqueta, color = "SKIP", _COLOR_SKIP
            row = tabla.add_row().cells
            # Nombre legible + validaciones esperadas de la fila del Examples
            nombre_raw = esc.get("nombre", "")
            m_r = re.match(r"^(.*?)\s*--\s*@(\d+)\.(\d+)(.*?)$", nombre_raw)
            p_esc = row[0].paragraphs[0]
            p_esc.clear()
            if m_r:
                base = m_r.group(1).strip()
                ridx = int(m_r.group(3))
                tpl  = _ej_map.get((base, ridx), "")
                titulo = f"{base}: {tpl}" if tpl else base
                r_tit = p_esc.add_run(titulo)
                r_tit.bold = True
                r_tit.font.size = Pt(9)
                # Añadir líneas con cada columna esperada (excepto template y status)
                fila_data = _filas_map.get((base, ridx), {})
                SKIP_COLS = {"template", "expected_status", "status_code"}
                for col, val in fila_data.items():
                    if col.lower() in SKIP_COLS:
                        continue
                    # Quitar prefijo expected_ para mostrar nombre limpio
                    col_display = re.sub(r"^expected_", "", col)
                    p_sub = row[0].add_paragraph(f"  · {col_display}: {val}")
                    p_sub.runs[0].font.size = Pt(8)
                    p_sub.runs[0].font.color.rgb = RGBColor(0x40, 0x40, 0x40)
            else:
                r_tit = p_esc.add_run(nombre_raw)
                r_tit.bold = True
                r_tit.font.size = Pt(9)
            _celda_color(row[1], etiqueta, color, negrita=True)
            detalle = esc.get("detalle", "")
            row[2].text = detalle[:300] + ("…" if len(detalle) > 300 else "")
            row[2].paragraphs[0].runs[0].font.size = Pt(8)
    else:
        doc.add_paragraph("(sin datos de escenarios de ejecucion)")

    # ── Detalle de llamadas HTTP ────────────────────────────────────────
    doc.add_heading("Llamadas por escenario", level=2)
    if llamadas:
        # Indice rapido scenario_name -> llamada
        llamadas_idx = {c.get("scenario", ""): c for c in llamadas}
        for esc in (escenarios_bdd or [{"nombre": c.get("scenario", ""), "estado": c.get("status", "")} for c in llamadas]):
            nombre = esc.get("nombre", "")
            estado = esc.get("estado", "unknown")
            color  = _COLOR_KO if estado in ("failed", "error") else (_COLOR_SKIP if estado == "skipped" else _COLOR_OK)
            etiqueta = "KO" if estado in ("failed", "error") else ("SKIP" if estado == "skipped" else "OK")

            sep = doc.add_paragraph()
            r = sep.add_run(f"[{etiqueta}]  {_display_nombre(nombre)}")
            r.bold = True
            r.font.color.rgb = color
            r.font.size = Pt(10)

            c = llamadas_idx.get(nombre)
            if c:
                req  = c.get("request") or {}
                resp = c.get("response") or {}
                if req:
                    doc.add_paragraph(f"  ➤ {req.get('method', '')} {req.get('url', '')}")
                    _parrafo_mono(doc, _construir_curl(req))
                    _parrafo_mono(doc, "Body: " + (json.dumps(req.get("body"), ensure_ascii=False) if req.get("body") else "(vacio)"))
                if resp:
                    sc = resp.get("status_code", "")
                    sc_color = _COLOR_OK if str(sc).startswith("2") else _COLOR_KO
                    p_sc = doc.add_paragraph("  ◀ Response ")
                    r_sc = p_sc.add_run(str(sc))
                    r_sc.bold = True
                    r_sc.font.color.rgb = sc_color
                    _parrafo_mono(doc, resp.get("body") or "(vacio)")

            # Validaciones esperadas (Then) extraídas del Examples
            m_r2 = re.match(r"^(.*?)\s*--\s*@(\d+)\.(\d+)(.*?)$", nombre)
            if m_r2:
                base2 = m_r2.group(1).strip()
                ridx2 = int(m_r2.group(3))
                fila_data = _filas_map.get((base2, ridx2), {})
            else:
                fila_data = {}
            if fila_data:
                es_ko = estado in ("failed", "error")
                detalle_fallo = esc.get("detalle", "") if es_ko else ""
                detalle_lower = detalle_fallo.lower()

                p_val = doc.add_paragraph("  ✔ Validaciones esperadas (Then):")
                p_val.runs[0].bold = True
                p_val.runs[0].font.size = Pt(9)

                def _linea_val(texto: str, fallo: bool):
                    p = doc.add_paragraph(f"    {'❌' if fallo else '·'} {texto}")
                    if p.runs:
                        p.runs[0].font.size = Pt(8)
                        if fallo:
                            p.runs[0].font.color.rgb = _COLOR_KO
                            p.runs[0].bold = True

                # Código de respuesta
                exp_status = fila_data.get("expected_status") or fila_data.get("status_code", "")
                if exp_status:
                    fallo_sc = es_ko and (
                        f"codigo de respuesta" in detalle_lower or
                        f"status" in detalle_lower and "expected" in detalle_lower and str(exp_status) in detalle_lower
                    )
                    _linea_val(f"Código respuesta = {exp_status}", fallo_sc)

                # Resto de columnas expected_*
                for col, val in fila_data.items():
                    if col.lower() in {"template", "expected_status", "status_code"}:
                        continue
                    col_display = re.sub(r"^expected_", "", col)
                    # Detectar si esta columna es la que falló buscando su nombre en el detalle
                    fallo_col = es_ko and col_display.lower() in detalle_lower
                    _linea_val(f"{col_display} = {val}", fallo_col)

                # Mostrar el mensaje de error completo si el escenario falló
                if es_ko and detalle_fallo:
                    p_err = doc.add_paragraph(f"  ⚠ Motivo del fallo: {detalle_fallo.splitlines()[0]}")
                    if p_err.runs:
                        p_err.runs[0].font.color.rgb = _COLOR_KO
                        p_err.runs[0].font.size = Pt(8)
                        p_err.runs[0].bold = True

            if not c:
                doc.add_paragraph("  (sin llamada HTTP registrada)")

            doc.add_paragraph("")
    else:
        doc.add_paragraph("No se registraron llamadas HTTP. Salida de behave:")
        _parrafo_mono(doc, salida)

    doc.save(destino)
    logger.info(f"Evidencias de ejecucion (Word) generadas en: {destino}")


def ejecutar_pruebas(hu: dict, feature_path, carpeta_evidencias: Path, validation_result: dict = None) -> dict:
    """Ejecuta behave sobre el feature indicado y genera un Word con las evidencias."""
    feature_name = Path(feature_path).name
    xml_dir = Path(tempfile.mkdtemp(prefix="behave_resultados_"))
    calls_file = xml_dir / "calls.json"

    # Normaliza acentos a su vocal base (ó→o, é→e, etc.) para evitar problemas
    # de encoding en Windows al pasar el argumento -i a behave.
    import unicodedata as _ud
    _norm = _ud.normalize("NFD", feature_name).encode("ascii", "ignore").decode("ascii")
    safe_pattern = re.escape(_norm)
    cmd = [
        sys.executable, "-m", "behave", "tests",
        "-i", safe_pattern,
        "--junit", "--junit-directory", str(xml_dir),
        "--no-color", "--no-capture",
    ]
    env = os.environ.copy()
    env["BEHAVE_CALLS_FILE"] = str(calls_file)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        salida = (proc.stdout or "") + "\n" + (proc.stderr or "")
        codigo = proc.returncode
    except subprocess.TimeoutExpired:
        salida = "ERROR: la ejecucion de behave supero el tiempo limite (300s)"
        codigo = -1
    except Exception as e:
        salida = f"ERROR al ejecutar behave: {e}"
        codigo = -1

    resumen = _parsear_resultados_xml(xml_dir)
    resumen["exit_code"] = codigo

    # Cargar mapping template por fila del feature
    try:
        ejemplos_map = _extraer_ejemplos_feature(str(feature_path))
    except Exception:
        ejemplos_map = {}

    # Mostrar resultado por escenario en consola
    print(f"\n   🧪 Resultados de ejecución:")
    if resumen["escenarios"]:
        # Contar por nombre base para asignar el índice de fila
        _row_counter: dict = {}
        for sc in resumen["escenarios"]:
            estado = sc["estado"]
            if estado == "passed":
                icono = "✅"
            elif estado in ("failed", "error"):
                icono = "❌"
            else:
                icono = "⏭️ "
            # Quitar el sufijo -- @X.Y que agrega behave
            nombre_raw = sc["nombre"]
            m_row = re.match(r"^(.*?)\s*--\s*@(\d+)\.(\d+)(.*?)$", nombre_raw)
            if m_row:
                base_name = m_row.group(1).strip()
                row_idx = int(m_row.group(3))
                template = ejemplos_map.get((base_name, row_idx), "")
                display = f"{base_name}: {template}" if template else base_name
            else:
                display = nombre_raw
            print(f"      {icono} {display}")
            if estado in ("failed", "error") and sc.get("detalle"):
                primera = sc["detalle"].splitlines()[0] if sc["detalle"] else ""
                if primera:
                    print(f"         ↳ {primera}")
        total = resumen["total"]
        p = resumen["passed"]
        f = resumen["failed"] + resumen["errors"]
        s = resumen["skipped"]
        print(f"\n   📊 Total: {total} | ✅ {p} passed | ❌ {f} failed | ⏭️  {s} skipped")
    else:
        print(f"      ⚠️  No se encontraron resultados JUnit (salida: {salida[:200]})")

    llamadas = []
    if calls_file.exists():
        try:
            llamadas = json.loads(calls_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            llamadas = []

    destino = carpeta_evidencias / f"evidencias_ejecucion_HU{hu.get('ref', 'UNKNOWN')}.docx"
    _generar_word_ejecucion(hu, feature_path, resumen, salida, destino, llamadas, validation_result)
    shutil.rmtree(xml_dir, ignore_errors=True)

    logger.info(
        f"Ejecucion behave: {resumen['passed']} passed, "
        f"{resumen['failed']} failed, {resumen['errors']} errors"
    )
    return resumen


def registrar_resultado(hu: dict, validation_result: dict, feature_path=None, ejecutar: bool = True, metrics_file: Path = None) -> dict:
    """
    Registra las metricas del validador en Excel, genera evidencias y (opcionalmente) ejecuta las pruebas.

    Args:
        hu: Diccionario con la informacion de la HU
        validation_result: Resultado devuelto por AgentValidator.validate_complete
        feature_path: Ruta del feature generado (para copiarlo y ejecutarlo)
        ejecutar: Si True, ejecuta behave sobre el feature; si False, solo registra metricas/validacion

    Returns:
        dict: Rutas del Excel y de la carpeta de evidencias, y el resumen de ejecucion
    """
    _init_xl()
    ejecucion = {}
    carpeta_evidencias = None

    # 1. Ejecucion de pruebas (siempre se captura aunque Excel falle despues)
    try:
        if ejecutar and feature_path:
            carpeta_evidencias = _generar_evidencias(hu, validation_result, feature_path)
            ejecucion = ejecutar_pruebas(hu, feature_path, Path(carpeta_evidencias), validation_result)
    except Exception as e:
        logger.error(f"Error al ejecutar pruebas: {str(e)}")

    # 2. Guardado de metricas en Excel (fallo aislado: no descarta el resultado de ejecucion)
    try:
        fila = _fila_desde_validacion(hu, validation_result, feature_path, ejecucion, carpeta_evidencias)
        _fp = metrics_file or HU_METRICS_FILE
        _guardar_metricas_excel(fila, filepath=_fp)
        metricas_excel = str(_fp)
    except Exception as e:
        logger.error(f"Error al guardar metricas en Excel: {str(e)}")
        metricas_excel = None

    return {
        "metricas_excel": metricas_excel,
        "evidencias": carpeta_evidencias,
        "ejecucion": ejecucion,
    }
