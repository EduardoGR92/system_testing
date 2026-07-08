from dotenv import load_dotenv
import os
import json
import logging
import sys
import time
from pathlib import Path
import yaml

# Imports pesados se cargan de forma lazy (dentro de las funciones que los usan)
# para que el menu aparezca sin espera al arrancar.

# Configurar logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Cargar variables de entorno desde .env
load_dotenv()

# Verificar que la API key está configurada
if not os.getenv('CLAUDE_API_KEY'):
    raise ValueError(
        "❌ CLAUDE_API_KEY no está configurada en .env\n"
        "1. Obtén una clave en: https://console.anthropic.com/account/keys\n"
        "2. Edita .env y añade: CLAUDE_API_KEY=sk-ant-..."
    )


# Umbral de score global a partir del cual se considera "ok" (no regenerar mas)
SCORE_OK = 0.95


def _fmt_tiempo(segundos: float) -> str:
    """Formatea un tiempo en segundos; pasa a min+s si supera 60 s."""
    if segundos >= 60:
        m = int(segundos // 60)
        s = segundos % 60
        return f"{m}m {s:.0f}s"
    return f"{segundos:.1f}s"


def _imprimir_resumen_tiempos(tiempos: dict):
    """Imprime el resumen de tiempos en formato estandar para los 3 flujos."""
    _iconos = {
        "Ingesta":    "📥",
        "Generacion": "🤖",
        "Validacion": "✅",
        "Ejecucion":  "🧪",
        "TOTAL":      "🕐",
    }
    print(f"\n{'='*52}")
    print(f"   ⏱️  Resumen de tiempos:")
    for fase, secs in tiempos.items():
        icono = _iconos.get(fase, "⏱️ ")
        print(f"      {icono} {fase:<28}: {_fmt_tiempo(secs)}")
    print(f"{'='*52}")


def _preguntar_sobrescribir(hu_id, feature_existente) -> bool:
    """Pregunta al usuario si desea sobrescribir una HU ya existente."""
    print(f"La HU #{hu_id} ya existe: {feature_existente}")
    respuesta = input(" ¿Deseas sobrescribirla? (s/n): ").strip().lower()
    return respuesta in ("s", "si", "sí", "y", "yes")


def _mostrar_score_validacion(validation_result: dict, mostrar_detalle: bool = True):
    """Muestra el score semantico/cobertura y explica que falta para el 100%."""
    semantica = validation_result.get("semantic_validation", {}) or {}
    coverage = semantica.get("coverage_score", 0) or 0
    semantic = semantica.get("semantic_score", 0) or 0
    overall = validation_result.get("overall_score", 0) or 0

    validaciones = validation_result.get("validaciones") or {}
    sint = "✅" if (validaciones.get("sintactica") or {}).get("valid") else "❌"
    cohe = "✅" if (validaciones.get("cohesion") or {}).get("valid") else "❌"
    tecn = "✅" if (validaciones.get("tecnica") or {}).get("valid") else "❌"
    func_obj = validaciones.get("funcional") or {}
    func_cov = func_obj.get("coverage_score", coverage) or coverage
    func_sem = func_obj.get("semantic_score", semantic) or semantic
    func = f"{(func_cov + func_sem) / 2:.0%}"
    print(f"   📈 Sint {sint} | Cohe {cohe} | Tecn {tecn} | Func {func} | Score global: {overall:.0%}")

    # Mostrar siempre los errores de las capas estáticas si hay alguno
    for capa_nombre, capa_key in [("Sint", "sintactica"), ("Cohe", "cohesion"), ("Tecn", "tecnica")]:
        capa = validaciones.get(capa_key) or {}
        for err in (capa.get("errors") or []):
            print(f"      ❌ [{capa_nombre}] {err}")
        for w in (capa.get("warnings") or []):
            print(f"      ⚠️  [{capa_nombre}] {w}")

    sugerencias_always = (semantica.get("improvement_suggestions") or [])
    if sugerencias_always:
        print("   💡 Sugerencias de mejora:")
        for i, s in enumerate(sugerencias_always, 1):
            print(f"      {i}. {s}")

    if not mostrar_detalle:
        return

    # Calcular el "gap" hasta el 100% y explicar a que se debe
    gap = round((1 - coverage) * 100)
    sin_cubrir = semantica.get("criterios_sin_cubrir", []) or []
    faltantes = semantica.get("escenarios_faltantes", []) or []
    sugerencias = semantica.get("improvement_suggestions", []) or []
    warnings = semantica.get("warnings", []) or []
    errores = semantica.get("errors", []) or []

    if gap <= 0 and not (sin_cubrir or faltantes or sugerencias or warnings or errores):
        print("   ✅ Cobertura completa: no falta nada por cubrir.")
        return

    # Mostrar, ordenado, que es lo que mas penaliza el score global
    penalizaciones = []
    cov_pen = round((1 - coverage) * 100)
    if cov_pen > 0:
        detalle = f"Cobertura -{cov_pen}%"
        if sin_cubrir:
            detalle += f" ({len(sin_cubrir)} criterio(s) sin cubrir)"
        elif faltantes:
            detalle += f" ({len(faltantes)} escenario(s) faltan)"
        penalizaciones.append((cov_pen, detalle))
    sem_pen = round((1 - semantic) * 100)
    if sem_pen > 0:
        detalle = f"Semantica -{sem_pen}%"
        if errores:
            detalle += f" ({len(errores)} error(es))"
        elif warnings:
            detalle += f" ({len(warnings)} advertencia(s))"
        penalizaciones.append((sem_pen, detalle))
    if penalizaciones:
        penalizaciones.sort(reverse=True)
        print(f"   🔻 Lo que mas penaliza (mayor primero): {' | '.join(d for _, d in penalizaciones)}")

    if gap > 0:
        print(f"   ⚠️  Falta ~{gap}% de cobertura. Para llegar al 100%:")
    if sin_cubrir:
        print("      Criterios sin cubrir:")
        for c in sin_cubrir:
            print(f"        - {c}")
    if faltantes:
        print("      Escenarios que faltan:")
        for e in faltantes:
            print(f"        - {e}")
    if errores:
        print("      Errores a corregir:")
        for e in errores:
            print(f"        - {e}")
    if warnings:
        print("      Advertencias:")
        for w in warnings:
            print(f"        - {w}")


def _imprimir_resumen_validaciones(procesados: list, score_ok: float = SCORE_OK):
    """Imprime un cuadro resumen con el resultado de validacion de cada HU/endpoint."""
    if len(procesados) < 2:
        return
    sep = "═" * 70
    lin = "─" * 70
    print(f"\n{sep}")
    print(f"   📋 Resumen de validaciones ({len(procesados)} features)")
    print(lin)
    print(f"   {'#':<4} {'Feature/Endpoint':<30} {'Sint':^4} {'Cohe':^4} {'Tecn':^4} {'Func':^6} {'Score':^6} {'':^3}")
    print(lin)
    for idx, (hu, feature_path, vr) in enumerate(procesados, 1):
        ref = hu.get('ref', '')
        subject = hu.get('subject', hu.get('path', str(feature_path)))
        if str(ref).isdigit():
            nombre = f"HU{ref} - {subject}"[:30]
        else:
            nombre = subject[:30]
        validaciones = vr.get('validaciones') or {}
        sint = '✅' if (validaciones.get('sintactica') or {}).get('valid') else '❌'
        cohe = '✅' if (validaciones.get('cohesion') or {}).get('valid') else '❌'
        tecn = '✅' if (validaciones.get('tecnica') or {}).get('valid') else '❌'
        semantica = vr.get('semantic_validation') or {}
        cov = semantica.get('coverage_score', 0) or 0
        sem = semantica.get('semantic_score', 0) or 0
        func = f"{(cov + sem) / 2:.0%}"
        overall = vr.get('overall_score', 0) or 0
        estado = '✅' if overall >= score_ok else '⚠️ '
        print(f"   {idx:<4} {nombre:<30} {sint:^4} {cohe:^4} {tecn:^4} {func:^6} {overall:>5.0%}  {estado}")
    print(lin)
    scores = [vr.get('overall_score', 0) or 0 for _, _, vr in procesados]
    media = sum(scores) / len(scores) if scores else 0
    n_ok = sum(1 for s in scores if s >= score_ok)
    n_nok = len(scores) - n_ok
    print(f"   Score medio: {media:.0%}   ✅ {n_ok} OK   {'❌ ' + str(n_nok) + ' bajo umbral' if n_nok else ''}")
    print(f"{sep}\n")


def _regenerar_casos_faltantes(generator, validator, hu, feature_result, feature_path, validation_result):
    """Ofrece al usuario regenerar el feature para cubrir los casos que indica el validador.

    Devuelve la tupla (feature_result, feature_path, validation_result) actualizada.
    """
    from agents.agent_validator import AgentValidator
    if (validation_result.get("overall_score", 0) or 0) >= SCORE_OK:
        sugerencias = (validation_result.get("semantic_validation") or {}).get("improvement_suggestions") or []
        if sugerencias:
            resp = input("   ❓ ¿Quieres aplicar las sugerencias de mejora y regenerar? (s/n): ").strip().lower()
            if resp in ("s", "si", "sí", "y", "yes"):
                feedback = generator.faltantes_de_validacion(validation_result)
                if feedback:
                    print("   🔄 Regenerando feature con sugerencias de mejora...")
                    feature_result, feature_path = generator.regenerar_con_feedback(hu, feedback)
                    print(f"   ✅ Feature regenerado en: {feature_path}")
                    generated = AgentValidator.load_generated(feature_path, "tests/steps/steps.py")
                    generated["used_endpoints"] = feature_result.get("endpoints_swagger", [])
                    validation_result = validator.validate_complete(generated, hu)
                    _mostrar_score_validacion(validation_result, mostrar_detalle=False)
        return feature_result, feature_path, validation_result
    for _ in range(3):  # como mucho 3 iteraciones para evitar bucles infinitos
        feedback = generator.faltantes_de_validacion(validation_result)
        if not feedback:
            break

        respuesta = input("   ❓ ¿Quieres generar los casos que faltan que indica el validador? (s/n): ").strip().lower()
        if respuesta not in ("s", "si", "sí", "y", "yes"):
            print("   ⏭️  No se generan los casos faltantes.")
            break

        print("   🔄 Regenerando feature con los casos faltantes...")
        feature_result, feature_path = generator.regenerar_con_feedback(hu, feedback)
        print(f"   ✅ Feature regenerado en: {feature_path}")

        generated = AgentValidator.load_generated(feature_path, "tests/steps/steps.py")
        generated["used_endpoints"] = feature_result.get("endpoints_swagger", [])
        validation_result = validator.validate_complete(generated, hu)
        _mostrar_score_validacion(validation_result)

    return feature_result, feature_path, validation_result


def _validar_y_registrar_generados(generator, validator, generados, max_iteraciones=3, score_ok=SCORE_OK, data=None, mostrar_detalle=True, validar_semantica=True, metrics_file=None):
    """Valida (y regenera automaticamente lo que falte) los features generados.

    Repite la validacion/regeneracion tantas veces como haga falta hasta alcanzar un
    score global >= score_ok (0.95 por defecto), o hasta que no haya nada accionable
    que regenerar, o hasta agotar max_iteraciones. Registra metricas SIN ejecutar y, al
    final, pregunta al usuario si desea ejecutar las pruebas.
    """
    from clients.swagger import SwaggerReader
    from agents.agent_validator import AgentValidator
    import reporter
    contract_endpoints = SwaggerReader.normalizar_endpoints(data) if data else None

    procesados = []
    scores_tests = []
    for hu, feature_result, feature_path in generados:
        _t_inicio = time.perf_counter()
        generated = AgentValidator.load_generated(feature_path, "tests/steps/steps.py")
        _raw_eps = feature_result.get("endpoints_swagger", [])
        if contract_endpoints:
            _enriched = []
            for ep in _raw_eps:
                _match = next(
                    (c for c in contract_endpoints
                     if c.get("method", "").upper() == ep.get("method", "").upper()
                     and c.get("path") == ep.get("path")),
                    None,
                )
                _enriched.append(_match if _match else ep)
            generated["used_endpoints"] = _enriched if _enriched else contract_endpoints[:1]
        else:
            generated["used_endpoints"] = _raw_eps
        if contract_endpoints is not None:
            generated["contract_endpoints"] = contract_endpoints
        validation_result = validator.validate_complete(generated, hu, validar_semantica=validar_semantica)
        _mostrar_score_validacion(validation_result, mostrar_detalle=mostrar_detalle)

        # Si score OK pero hay sugerencias, ofrecer aplicarlas (hasta 3 iteraciones)
        for _iter_sug in range(3):
            if (validation_result.get("overall_score", 0) or 0) < score_ok:
                break
            sugerencias = (validation_result.get("semantic_validation") or {}).get("improvement_suggestions") or []
            if not sugerencias:
                break
            resp = input(f"   ❓ ¿Quieres aplicar las sugerencias de mejora y regenerar? (s/n) [{_iter_sug + 1}/3]: ").strip().lower()
            if resp not in ("s", "si", "sí", "y", "yes"):
                break
            feedback = generator.faltantes_de_validacion(validation_result)
            if not feedback:
                break
            print("   🔄 Regenerando feature con sugerencias de mejora...")
            feature_result, feature_path = generator.regenerar_con_feedback(hu, feedback, endpoints=contract_endpoints)
            print(f"   ✅ Feature regenerado en: {feature_path}")
            generated = AgentValidator.load_generated(feature_path, "tests/steps/steps.py")
            generated["used_endpoints"] = feature_result.get("endpoints_swagger", [])
            if contract_endpoints is not None:
                generated["contract_endpoints"] = contract_endpoints
            validation_result = validator.validate_complete(generated, hu, validar_semantica=validar_semantica)
            _mostrar_score_validacion(validation_result, mostrar_detalle=mostrar_detalle)

        # Regenerar si el score no llega al umbral, preguntando al usuario cada vez
        for _ in range(max_iteraciones):
            score = validation_result.get("overall_score", 0) or 0
            if score >= score_ok:
                break
            feedback = generator.faltantes_de_validacion(validation_result)
            if not feedback:
                break
            respuesta = input(f"   ❓ Score {score:.0%} < {score_ok:.0%}. ¿Regenerar '{hu.get('subject', '')}'? (s/n): ").strip().lower()
            if respuesta not in ("s", "si", "sí", "y", "yes"):
                print("   ⏭️  No se regenera.")
                break
            print("   🔄 Regenerando feature con los casos faltantes...")
            feature_result, feature_path = generator.regenerar_con_feedback(hu, feedback, endpoints=contract_endpoints)
            print(f"   ✅ Feature regenerado en: {feature_path}")
            generated = AgentValidator.load_generated(feature_path, "tests/steps/steps.py")
            generated["used_endpoints"] = feature_result.get("endpoints_swagger", [])
            if contract_endpoints is not None:
                generated["contract_endpoints"] = contract_endpoints
            validation_result = validator.validate_complete(generated, hu, validar_semantica=validar_semantica)
            _mostrar_score_validacion(validation_result, mostrar_detalle=mostrar_detalle)

        # Registrar metricas/evidencias SIN ejecutar todavia
        reporter.registrar_resultado(hu, validation_result, feature_path, ejecutar=False, metrics_file=metrics_file)
        _t_val = time.perf_counter() - _t_inicio
        print(f"   ⏱️  Tiempo validación + refinamiento: {_fmt_tiempo(_t_val)}")
        procesados.append((hu, feature_path, validation_result))
        scores_tests.append(validation_result.get("overall_score", 0) or 0)

    if not procesados:
        return scores_tests, 0.0

    _imprimir_resumen_validaciones(procesados, score_ok=score_ok)

    # Preguntar al usuario si desea ejecutar las pruebas generadas
    respuesta = input("\n   ❓ ¿Deseas ejecutar las pruebas generadas ahora? (s/n): ").strip().lower()
    if respuesta not in ("s", "si", "sí", "y", "yes"):
        print("   ⏭️  No se ejecutan las pruebas. Quedan generadas y validadas.")
        return scores_tests, 0.0

    _t_exec_inicio = time.perf_counter()
    total_passed = total_failed = total_skipped = 0
    for hu, feature_path, _vr in procesados:
        print(f"   🧪 Ejecutando pruebas de '{hu.get('subject', '')}'...")
        resultado = reporter.registrar_resultado(hu, _vr, feature_path, ejecutar=True, metrics_file=metrics_file)
        resumen = resultado.get("ejecucion", {})
        p = resumen.get("passed", 0)
        f = resumen.get("failed", 0) + resumen.get("errors", 0)
        s = resumen.get("skipped", 0)
        total_passed += p
        total_failed += f
        total_skipped += s
        print(f"      {p} passed, {f} failed, {s} skipped")
    total = total_passed + total_failed + total_skipped
    print(f"\n   📊 Total: {total} | ✅ {total_passed} passed | ❌ {total_failed} failed | ⏭️  {total_skipped} skipped")
    _t_ejecucion = time.perf_counter() - _t_exec_inicio
    print(f"   ⏱️  Tiempo total ejecución: {_fmt_tiempo(_t_ejecucion)}")

    return scores_tests, _t_ejecucion


def mostrar_menu():
    """Muestra el menú principal interactivo."""

    print("FRAMEWORK BDD - TESTING")
    print("="*60)
    print("\n¿Qué deseas importar?\n")
    print("1. Una Historia de Usuario específica")
    print("2. Todas las HUs del sprint activo")
    print("3. Esquema OpenAPI (payments.yaml)")
    print("0. Salir")
    print("\n" + "-"*60)
    return input("Selecciona una opción (0-3):").strip()


def opcion_hu_especifica():
    """Importa una HU específica."""
    import reporter
    reporter.HU_METRICS_FILE.unlink(missing_ok=True)
    print("Ingresa el número de la HU")
    try:
        hu_number = int(input("   HU número: ").strip())
        if hu_number < 1:
            print("ERROR. El número debe ser positivo")
            return None
        
        logger.info(f"Importando HU #{hu_number}...")
        return procesar_hu(hu_number)
    except ValueError:
        print("Debes ingresar un número válido")
        return None


def opcion_sprint_activo():
    """Importa todas las HUs del sprint activo."""
    import reporter
    reporter.HU_METRICS_FILE.unlink(missing_ok=True)
    from clients.ingest import extract_info_allHU
    logger.info("Obteniendo todas las HUs del sprint activo...")
    _t_total = time.perf_counter()
    try:
        hus = extract_info_allHU()
        if not hus:
            print("❌ No se encontraron HUs disponibles")
            return None
        
        print(f"\n✅ Se encontraron {len(hus)} HU(s)")
        resultados = []
        for i, hu in enumerate(hus, 1):
            print(f"\n[{i}/{len(hus)}] Procesando HU #{hu.get('ref', 'UNKNOWN')}...")
            resultado = procesar_hu_completo(hu)
            if resultado:
                resultados.append(resultado)
        
        _imprimir_resumen_tiempos({"TOTAL": time.perf_counter() - _t_total})
        return resultados
    except Exception as e:
        logger.error(f"Error al obtener sprint: {str(e)}")
        return None


def opcion_yaml():
    """Carga datos desde archivo YAML OpenAPI (payments.yaml)."""
    from agents.agent_generator import AgentGenerator
    from agents.agent_validator import AgentValidator
    import reporter
    reporter.OPENAPI_METRICS_FILE.unlink(missing_ok=True)
    yaml_file = Path("payments.yaml")
    _t_total = time.perf_counter()
    
    if not yaml_file.exists():
        print(f"❌ Archivo no encontrado: {yaml_file}")
        print(f"   El archivo debe estar en la raíz del proyecto")
        return None
    
    try:
        logger.info(f"Cargando OpenAPI schema desde {yaml_file}...")
        with open(yaml_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        if not data:
            print("❌ YAML vacío o inválido")
            return None
        
        # Mostrar información del API
        title = data.get('info', {}).get('title', 'API')
        version = data.get('info', {}).get('version', 'unknown')
        paths = data.get('paths', {})
        
        print(f"\n✅ OpenAPI Schema cargado:")
        print(f"   Título: {title}")
        print(f"   Versión: {version}")
        print(f"   Endpoints: {len(paths)}")
      
        # Validar el esquema OpenAPI (endpoints, codigos de respuesta, semantica)
        logger.info("Validando esquema OpenAPI...")
        validator = AgentValidator(yaml_data=data)
        validation_result = validator.validate_openapi(data)
        if validation_result.get("overall_valid"):
            print(f"   ✅ Validación del esquema exitosa")
        else:
            print(f"   ⚠️  Validación del esquema con warnings/errores")
        _mostrar_score_validacion(validation_result)
        _t_ingesta = time.perf_counter() - _t_total

        # Generar escenarios SOLO para los endpoints que aun no tienen feature
        _t_gen_ini = time.perf_counter()
        generator = AgentGenerator()
        generados = generator.generar_escenarios_no_cubiertos(data)
        _t_generacion = time.perf_counter() - _t_gen_ini

        _t_val_ini = time.perf_counter()
        _res = _validar_y_registrar_generados(generator, validator, generados, data=data, mostrar_detalle=False, validar_semantica=True, metrics_file=reporter.OPENAPI_METRICS_FILE)
        _t_ejecucion_yaml = _res[1] if isinstance(_res, tuple) else 0.0
        _t_validacion_ejecucion = time.perf_counter() - _t_val_ini - _t_ejecucion_yaml

        # Registrar metricas (Excel) y evidencias basicas del esquema OpenAPI
        reporte = reporter.registrar_openapi(data, validation_result, str(yaml_file))
        if reporte:
            print(f"\n📊 Métricas: {reporte.get('metricas_excel')}")
        
        _tiempos_yaml = {"Ingesta": _t_ingesta, "Generacion": _t_generacion, "Validacion": _t_validacion_ejecucion}
        if _t_ejecucion_yaml > 0:
            _tiempos_yaml["Ejecucion"] = _t_ejecucion_yaml
        _tiempos_yaml["TOTAL"] = time.perf_counter() - _t_total
        _imprimir_resumen_tiempos(_tiempos_yaml)
        return {
            "openapi_data": data,
            "title": title,
            "version": version,
            "endpoints_count": len(paths)
        }
    except Exception as e:
        logger.error(f"Error al cargar YAML: {str(e)}")
        return None


def procesar_hu(hu_number: int):
    """Procesa una HU específica (ingestar → generar → validar)."""
    from clients.ingest import extract_info
    from agents.agent_generator import AgentGenerator
    from agents.agent_validator import AgentValidator
    import reporter
    try:
        # 1. Ingestar
        _t_inicio = time.perf_counter()
        logger.info(f"1️⃣  Extrayendo HU #{hu_number}...")
        hu_data = extract_info(hu_number)
        if not hu_data:
            print(f"❌ No se encontró HU #{hu_number}")
            return None
        
        hu = hu_data[0]
        print(f"   ✅ HU extraída: {hu.get('subject', 'Sin título')}")
        _t_ingesta = time.perf_counter() - _t_inicio

        # Comprobar si la HU ya existe y preguntar si se sobrescribe
        hu_id = hu.get("ref", hu_number)
        features_dir = Path("tests/features")
        existentes = list(features_dir.rglob(f"hu{hu_id}_*.feature")) if features_dir.exists() else []
        if existentes and not _preguntar_sobrescribir(hu_id, existentes[0]):
            print(f"⏭️  No se sobrescribe la HU #{hu_id}")
            logger.info(f"El usuario decidio no sobrescribir la HU #{hu_id}: {existentes[0]}")
            return {
                "hu_id": hu_id,
                "status": "already_exists",
                "feature_file": str(existentes[0]),
            }
        
        # 2. Generar
        _t_gen_ini = time.perf_counter()
        logger.info(f"2️⃣  Generando feature para HU #{hu_number}...")
        generator = AgentGenerator()
        feature_result = generator.generate_feature(hu)
        print(f"   ✅ Feature generado")
        
        # 3. Guardar
        logger.info(f"3️⃣  Guardando archivos...")
        feature_path = generator.save_feature(hu, feature_result)
        print(f"   ✅ Archivos guardados en: {feature_path}")
        _t_generacion = time.perf_counter() - _t_gen_ini

        # 4. Validar
        _t_val_ini = time.perf_counter()
        logger.info(f"4️⃣  Validando...")
        validator = AgentValidator()
        generated = AgentValidator.load_generated(feature_path, "tests/steps/steps.py")
        generated["used_endpoints"] = feature_result.get("endpoints_swagger", [])
        validation_result = validator.validate_complete(generated, hu)
        
        if validation_result.get("overall_valid"):
            print(f"   ✅ Validación exitosa")
        else:
            print(f"   ⚠️  Validación con warnings/errores")
        _mostrar_score_validacion(validation_result)

        # 4b. Ofrecer generar los casos faltantes que indica el validador
        feature_result, feature_path, validation_result = _regenerar_casos_faltantes(
            generator, validator, hu, feature_result, feature_path, validation_result)
        _t_validacion = time.perf_counter() - _t_val_ini

        # 5. Preguntar si se quiere ejecutar antes de registrar
        resp_exec = input("\n   ❓ ¿Deseas ejecutar las pruebas generadas ahora? (s/n): ").strip().lower()
        ejecutar = resp_exec in ("s", "si", "sí", "y", "yes")
        if not ejecutar:
            print("   ⏭️  No se ejecutan las pruebas. Quedan generadas y validadas.")

        # 6. Registrar metricas (Excel) y evidencias basicas
        _t_exec_ini = time.perf_counter()
        reporte = reporter.registrar_resultado(hu, validation_result, feature_path, ejecutar=ejecutar)
        _t_ejecucion = time.perf_counter() - _t_exec_ini
        if reporte:
            print(f"   📊 Métricas: {reporte.get('metricas_excel')}")
            if reporte.get("evidencias"):
                print(f"   📁 Evidencias: {reporte.get('evidencias')}")
            ejec = reporte.get("ejecucion") or {}
            if ejec:
                fallidos = ejec.get("failed", 0) + ejec.get("errors", 0)
                print(f"   🧪 Ejecución: {ejec.get('passed', 0)} passed, "
                      f"{fallidos} failed (incluye {ejec.get('errors', 0)} errors)")
        _t_total_val = time.perf_counter() - _t_inicio
        _tiempos = {"Ingesta": _t_ingesta, "Generacion": _t_generacion, "Validacion": _t_validacion}
        if ejecutar:
            _tiempos["Ejecucion"] = _t_ejecucion
        _tiempos["TOTAL"] = _t_total_val
        _imprimir_resumen_tiempos(_tiempos)
        
        return {
            "hu_id": hu.get("ref"),
            "status": "success" if validation_result.get("overall_valid") else "warning",
            "feature_file": feature_path,
            "validation_score": validation_result.get("overall_score", 0),
            "ejecucion": (reporte or {}).get("ejecucion") or {},
        }
    except Exception as e:
        logger.error(f"Error al procesar HU: {str(e)}")
        return None


def procesar_hu_completo(hu: dict):
    """Procesa una HU completa desde un diccionario."""
    from agents.agent_generator import AgentGenerator
    from agents.agent_validator import AgentValidator
    import reporter
    try:
        hu_ref = hu.get('ref', 'UNKNOWN')
        logger.info(f"Procesando HU #{hu_ref}...")
        
        # Comprobar si la HU ya existe y preguntar si se sobrescribe
        features_dir = Path("tests/features")
        existentes = list(features_dir.rglob(f"hu{hu_ref}_*.feature")) if features_dir.exists() else []
        if existentes and not _preguntar_sobrescribir(hu_ref, existentes[0]):
            logger.info(f"El usuario decidio no sobrescribir la HU #{hu_ref}: {existentes[0]}")
            return {
                "hu_id": hu_ref,
                "status": "already_exists",
                "feature_file": str(existentes[0]),
            }
        
        # Generar
        _t_inicio = time.perf_counter()
        generator = AgentGenerator()
        feature_result = generator.generate_feature(hu)
        feature_path = generator.save_feature(hu, feature_result)
        _t_generacion = time.perf_counter() - _t_inicio

        # Validar
        _t_val_ini = time.perf_counter()
        validator = AgentValidator()
        generated = AgentValidator.load_generated(feature_path, "tests/steps/steps.py")
        generated["used_endpoints"] = feature_result.get("endpoints_swagger", [])
        validation_result = validator.validate_complete(generated, hu)
        _mostrar_score_validacion(validation_result)

        # Ofrecer generar los casos faltantes que indica el validador
        feature_result, feature_path, validation_result = _regenerar_casos_faltantes(
            generator, validator, hu, feature_result, feature_path, validation_result)
        _t_validacion = time.perf_counter() - _t_val_ini

        # Preguntar si se quiere ejecutar antes de registrar
        resp_exec = input("\n   ❓ ¿Deseas ejecutar las pruebas generadas ahora? (s/n): ").strip().lower()
        ejecutar = resp_exec in ("s", "si", "sí", "y", "yes")
        if not ejecutar:
            print("   ⏭️  No se ejecutan las pruebas. Quedan generadas y validadas.")

        # Registrar metricas (Excel) y evidencias basicas
        _t_exec_ini = time.perf_counter()
        reporte = reporter.registrar_resultado(hu, validation_result, feature_path, ejecutar=ejecutar)
        _t_ejecucion = time.perf_counter() - _t_exec_ini
        if reporte:
            print(f"   📊 Métricas: {reporte.get('metricas_excel')}")
            if reporte.get("evidencias"):
                print(f"   📁 Evidencias: {reporte.get('evidencias')}")
        _tiempos = {"Generacion": _t_generacion, "Validacion": _t_validacion}
        if ejecutar:
            _tiempos["Ejecucion"] = _t_ejecucion
        _tiempos["TOTAL"] = time.perf_counter() - _t_inicio
        _imprimir_resumen_tiempos(_tiempos)

        ejec = (reporte or {}).get("ejecucion") or {}
        return {
            "hu_id": hu_ref,
            "status": "success" if validation_result.get("overall_valid") else "warning",
            "feature_file": feature_path,
            "validation_score": validation_result.get("overall_score", 0),
            "ejecucion": ejec,
        }
    except Exception as e:
        logger.error(f"Error procesando HU: {str(e)}")
        return None


def mostrar_resultados(resultados):
    """Muestra los resultados del procesamiento."""
    if not resultados:
        return
    
    # Convertir a lista si es un solo resultado
    if isinstance(resultados, dict):
        resultados = [resultados]
    
    print("\n" + "="*60)
    print("📊 RESULTADOS DEL PROCESAMIENTO")
    print("="*60)
    
    total_passed = sum((r.get("ejecucion") or {}).get("passed", 0) for r in resultados)
    total_failed = sum(((r.get("ejecucion") or {}).get("failed", 0) + (r.get("ejecucion") or {}).get("errors", 0)) for r in resultados)
    total_hus = len(resultados)

    print(f"\n📈 HUs procesadas : {total_hus}")
    print(f"🧪 Escenarios     : ✅ {total_passed} passed  ❌ {total_failed} failed")

    print("\n📋 Detalles:")
    for r in resultados:
        hu_id = r.get("hu_id", "UNKNOWN")
        status = "✅" if r.get("status") == "success" else "⚠️"
        score = r.get("validation_score", 0)
        ejec = r.get("ejecucion") or {}
        if ejec.get("total", 0):
            passed = ejec.get("passed", 0)
            failed = ejec.get("failed", 0) + ejec.get("errors", 0)
            skipped = ejec.get("skipped", 0)
            ejec_str = f" | 🧪 {passed} passed, {failed} failed, {skipped} skipped"
        else:
            ejec_str = " | 🧪 sin ejecutar"
        print(f"   {status} HU-{hu_id}: {score:.0%} de cobertura{ejec_str}")
    
    print("\n💾 Archivos generados en: output/")
    print("   - features/hu*.feature")
    print("   - steps.py")
    print("="*60 + "\n")


def main():
    """Flujo principal interactivo."""
    while True:
        opcion = mostrar_menu()
        
        if opcion == "0":
            print("\n👋 ¡Hasta luego!")
            break
        elif opcion == "1":
            resultados = opcion_hu_especifica()
            if resultados:
                mostrar_resultados(resultados)
        elif opcion == "2":
            resultados = opcion_sprint_activo()
            if resultados:
                mostrar_resultados(resultados)
        elif opcion == "3":
            # OpenAPI schema - solo muestra información
            resultado = opcion_yaml()
            if resultado:
                print(f"\n✅ Esquema OpenAPI cargado correctamente")
        else:
            print("❌ Opción no válida. Intenta de nuevo.")


if __name__ == "__main__":

    main()



