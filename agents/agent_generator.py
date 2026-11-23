# Agent para generar features Gherkin a partir de Historias de Usuario
from pathlib import Path
import logging
import json
import re

from clients.claude import Claude
from clients.efficiency import contexto_llm
from clients.swagger import SwaggerReader

URL_SWAGGER = 'http://127.0.0.1:8000/openapi.json'

logger = logging.getLogger(__name__)


class AgentGenerator:
    """
    Agente que genera features Gherkin y steps automáticamente a partir de Historias de Usuario.
    """

    def __init__(self, model=None, strict_mode=False):
        """
        Inicializa el agente con el modelo Claude y el lector Swagger.
        
        Raises:
            Exception: Si hay error al conectar con Swagger o Claude
        """
        try:
            logger.info("Inicializando AgentGenerator...")
            self.model = model if model is not None else Claude(role="Generador")
            self.strict_mode = strict_mode
            self.swagger = SwaggerReader(URL_SWAGGER)
            logger.info("AgentGenerator inicializado correctamente")
        except Exception as e:
            logger.error(f"Error al inicializar AgentGenerator: {str(e)}")
            raise

    @staticmethod
    def _cargar_templates_existentes(hu_ref) -> str:
        """Carga los templates JSON existentes para la HU indicada."""
        import json as _json
        from pathlib import Path as _Path
        carpeta = _Path(f"tests/templates/HU{hu_ref}")
        if not carpeta.exists():
            return ""
        lineas = []
        for f in sorted(carpeta.glob("*.json")):
            try:
                body = _json.loads(f.read_text(encoding="utf-8"))
                lineas.append(f"{f.stem}.json: {_json.dumps(body, ensure_ascii=False)}")
            except Exception:
                pass
        return "\n".join(lineas) if lineas else ""

    def buildPrompt(self, hu: dict, feedback: dict = None, endpoints: list = None) -> str:
        """
        Construye el prompt para Claude basado en la HU y endpoints del swagger.
        
        Args:
            hu: Diccionario con información de la Historia de Usuario
            feedback: Opcional. Casos faltantes detectados por el validador a cubrir.
            endpoints: Opcional. Endpoints ya normalizados (flujo YAML). Si es None,
                se obtienen del swagger en vivo (flujo HU).
            
        Returns:
            str: Prompt formateado para enviar a Claude
        """
        try:
            if endpoints is not None:
                # Flujo YAML: usar el contrato ya cargado/normalizado, sin llamar a la URL
                swagger_endpoints = endpoints
            else:
                try:
                    swagger_endpoints = self.swagger.get_endpoints_normalizados()
                except Exception as e:
                    logger.warning(f"No se pudieron obtener endpoints del swagger: {e}")
                    swagger_endpoints = []
            hu_title = hu.get("subject", "Feature sin titulo")
            pasos_existentes = self._leer_steps_existentes()
            bloque_feedback = self._construir_bloque_feedback(feedback)
            templates_existentes = self._cargar_templates_existentes(hu.get("ref", ""))
            
            prompt = f"""Como QA experto en APIs y BDD, quiero que interpretes las HU que recibas por entrada y:
1. Analices su entrada
2. Extraiga sus características principales
3. Conviertas los criterios de aceptacion en escenarios gherkin e implementes los steps.

Las reglas son las siguientes:
- El feature generado debe contener los n escenarios outlines correspondientes a los n criterios de aceptacion con la estructura Given, When, Then.
- Todas las HU deben tener una estructura parecida Given When Then con steps los mas parecidos posibles
- Cada escenario outline debe tener un titulo descriptivo y claro
- No puede haber escenarios sin Given When Then
- Debe haber solo un feature por archivo
- Puedes basarte en el swagger o escenarios ya existentes en el proyecto.
- Tienes que entender bien la logica de negocio de la HU
- El campo "scenarios" NO debe contener ninguna linea que empiece por "Feature:". El titulo del Feature se anade automaticamente a partir del campo "feature". En "scenarios" solo deben ir las lineas Scenario/Scenario Outline con sus Given/When/Then y Examples.
- Si ya existe un escenario similar, puedes reutilizarlo pero debes adaptarlo a la nueva HU
- Se generaran un json correspondiente a la HU que sea el body principal del escenario outline 
- En el feature generado el nombre de la template no puede ser una ruta, sino un nombre descriptivo que indique a que HU corresponde
- OBLIGATORIO — UNICIDAD DE NOMBRES DE TEMPLATES: los templates se buscan de forma recursiva en TODA la carpeta tests/templates, por lo que si dos features usan el mismo nombre de template, el step cargara el primero que encuentre (orden alfabetico de carpetas) y NO el que corresponde al endpoint actual. Para evitar colisiones: (1) NUNCA uses nombres genericos como 'payment_create_success' como template de creacion en un feature que NO es POST /payments — ese nombre ya existe en la carpeta de POST_payments y el step lo encontrara primero; (2) cuando un feature de un endpoint secundario (cancel, authorize, GET por id...) necesita crear un pago como precondicion, el template de creacion DEBE llamarse con el sufijo del endpoint: 'payment_create_for_cancel', 'payment_create_for_authorize', 'payment_create_for_get_by_card_id', etc.; (3) los templates de accion propia (cancel, authorize) pueden usar nombres propios: 'payment_cancel_success', 'payment_authorize_success'; (4) en general, el nombre del template debe ser unico en todo el proyecto — si no estas seguro, añade el sufijo del endpoint o de la HU al nombre.
- Los features, templates y steps no pueden contener acentos ni caracteres especiales, para evitar problemas de encoding, en ese caso se pondra la vocal que corresponda. Si alguna vocal lleva acento se pondra sin el. 
- Los features generados deben empezar por Feature
- IMPORTANTE Y OBLIGATORIO: esta TERMINANTEMENTE PROHIBIDO redefinir un step (decorador @given/@when/@then) cuyo texto ya aparezca en la lista 'STEPS YA EXISTENTES'. Si necesitas un step que ya existe, REUTILIZALO escribiendo en tus escenarios EXACTAMENTE el mismo texto del decorador (mismas palabras, mismas comillas, mismos parametros) y NO lo incluyas en 'steps_functions' ni en 'new_steps_added'. En esas listas SOLO puede ir codigo de steps cuyo texto NO este ya en la lista de existentes. Si todos los steps que necesitas ya existen, devuelve 'steps_functions' y 'new_steps_added' como listas vacias.
- IMPORTANTE Y OBLIGATORIO: solo se ESCRIBEN en el archivo los steps de 'new_steps_added'. Por lo que en 'new_steps_added' cada step (texto del decorador @given/@when/@then) debe aparecer UNA SOLA VEZ. Esta TERMINANTEMENTE PROHIBIDO repetir el mismo step (mismo verbo y mismo texto, aunque cambien comillas o espacios) dentro de 'new_steps_added'. Antes de devolver el JSON, revisa 'new_steps_added' y elimina cualquier funcion cuyo decorador ya este definido en otra posicion de la misma lista o en 'STEPS YA EXISTENTES'.
- ROLES A TENER EN CUENTA DE CADA LISTA: 'steps_functions' es la lista COMPLETA de referencia con TODOS los steps que usan tus escenarios (incluidos los reutilizados ya existentes); NO se escribe en el archivo. 'new_steps_added' contiene UNICAMENTE las funciones de steps NUEVAS que aun NO existen en 'STEPS YA EXISTENTES'; esta es la unica lista que se escribe. Un step reutilizado va solo en 'steps_functions' y NUNCA en 'new_steps_added'. Si no hace falta crear ningun step nuevo, devuelve 'new_steps_added' como lista vacia [].
- Los steps de mandar peticion deben ser reutilizable para varios endpoints, no quiero un step por cada endpoint diferente, tanto verbo como endpoint debe ser dinamico, no un valor estatico en el step.
- OBLIGATORIO — IMPLEMENTACION DEL STEP WHEN DE PETICION HTTP: si el step "se envia una peticion '{{method}}' al endpoint '{{endpoint}}'" NO existe en 'STEPS YA EXISTENTES' y debes crearlo, su implementacion DEBE incluir SIEMPRE la sustitucion de path params antes de construir la URL. El codigo obligatorio es exactamente este patron: `url = f'{{base_url}}{{endpoint}}'` seguido de `if '{{card_id}}' in url and hasattr(context, 'card_id'): url = url.replace('{{card_id}}', str(context.card_id))` y `if '{{payment_id}}' in url and hasattr(context, 'payment_id'): url = url.replace('{{payment_id}}', str(context.payment_id))`. Sin esta sustitucion, los placeholders literales como /payments/{{payment_id}}/cancel se enviaran URL-encoded (%7Bpayment_id%7D) y la API devolvera 404.
- Los steps de validacion de datos o campos deben ser reutilizables, no quiero un step por validacion de cada campo, tanto campo como valor debe ser dinamico en funcion del escenario.
- Los json pueden ser reutilizados entre escenarios, pero deben ser adaptados a cada HU
- Si la HU ya existe de antes y no ha cambiado, debes preguntar si puedes sobreescribir la ya existente
- Por cada feature que generes, si tienes dudas puedes preguntarme.
- Debes generar la implementacion de cada step, en python valido y corregir las importaciones si es necesario
- Cada step debe estar COMPLETAMENTE implementado con codigo Python valido y ejecutable. Esta PROHIBIDO usar 'pass', o 'TODO' como cuerpo de un step.
- La indentacion del cuerpo de cada funcion debe ir incluida dentro de cada string de la lista (4 espacios). No dependas de que el sistema la anada.
- Los decoradores deben usar comillas simples para envolver los parametros tipo '{{param}}', coincidiendo EXACTAMENTE con las comillas simples que usa el feature (ej: @given("el body de creacion de pago '{{template}}'")).
- ESTRICTO Y OBLIGATORIO: en los DECORADORES (@given/@when/@then) los parametros se capturan SIEMPRE con llaves '{{nombre}}' (sintaxis parse de behave), NUNCA con angulos '<nombre>'. Los angulos '<nombre>' SOLO se usan en las lineas del Scenario Outline del feature y en sus Examples. Si pones '<nombre>' en un decorador, behave NO capturara el parametro y el step fallara con error. Ejemplo correcto: feature -> "When se envia una peticion '<method>' al endpoint '<endpoint>'"; decorador -> @when("se envia una peticion '{{method}}' al endpoint '{{endpoint}}'").
- La implementacion real debe: cargar el body buscando <template>.json de forma recursiva dentro de tests/templates (los templates se guardan en subcarpetas por HU), ejecutar la peticion HTTP real al endpoint del swagger y comprobar con asserts el status code y los campos de la respuesta.
- OBLIGATORIO: el campo "body" de cada template DEBE usar EXACTAMENTE los mismos nombres de campo que aparecen en "request_template.fields" del endpoint swagger correspondiente. Esta TERMINANTEMENTE PROHIBIDO abreviar, renombrar, añadir o quitar sufijos a esos nombres. Ejemplos de errores prohibidos: si el contrato define "country_id" no puedes usar "country"; si define "merchant_id" no puedes usar "merchant"; si define "currency" (SIN _id) NO puedes usar "currency_id" — el campo se llama "currency", punto. ANTES de escribir cada campo del template, busca su nombre exacto en "request_template.fields" y copialo literalmente sin modificarlo.
- OBLIGATORIO — CAMPOS DE RESPUESTA: "responses[codigo].response_fields" es el universo COMPLETO y EXHAUSTIVO de campos que devuelve ese endpoint. NO es una lista parcial ni incompleta. Si un campo NO aparece en response_fields, NO EXISTE en la respuesta, aunque tu conocimiento general diga lo contrario. CASO ESPECIAL — RESPUESTAS DE TIPO LISTA: si "responses[codigo].is_list=true", el endpoint devuelve una LISTA de objetos donde cada elemento tiene los campos de response_fields. En este caso: (1) el Then SOLO puede validar campos que aparezcan en response_fields del item (NO campos del request body como card_id si no estan en response_fields), (2) usa el step "la respuesta es una lista con al menos un elemento" seguido de "el primer elemento de la lista contiene el campo 'X' con valor 'Y'". CRITICO — IMPLEMENTACION DEL STEP DE LISTA: el step "la respuesta es una lista con al menos un elemento" DEBE implementarse buscando el elemento que coincida con context.payment_id si existe (ese es el pago recien creado); si no existe context.payment_id, usar el ultimo elemento (response_data[-1]). NUNCA usar response_data[0] porque la lista puede contener pagos previos de otras ejecuciones y el pago recien creado es siempre el ultimo. El paso de validacion de campo ("el primer elemento de la lista contiene...") debe usar context.current_item, que es el elemento guardado por el step anterior. Implementacion obligatoria del step de lista: `response_data = context.response.json(); assert isinstance(response_data, list) and len(response_data) > 0; pid = str(getattr(context, 'payment_id', None)); match = next((e for e in response_data if str(e.get('payment_id','')) == pid), None) if pid != 'None' else None; context.current_item = match if match else response_data[-1]`. El step de campo usa: `item = context.current_item; assert field in item`. NUNCA valides campos de lista que no esten en response_fields aunque esten en el request body. Ejemplo critico: si response_fields de POST /payments es {{status, amount, currency, card_id}}, entonces NO hay payment_id, NO hay id, NO hay transaction_id — el contrato manda sobre tu conocimiento previo. Los steps Then SOLO pueden comprobar campos presentes en response_fields o en el example. Si response_fields y example estan ausentes, el Then UNICAMENTE valida el status code.
- OBLIGATORIO — ESTRUCTURA DE ERRORES Y MENSAJES: la API devuelve los errores con la estructura {{"detail": {{"error": "...", "details": [...]}}}}. Para el step de validacion de error: busca primero en response_data['error'], si no existe busca en response_data['detail']['error'] (si detail es dict) o en response_data['detail'] (si es string). La comparacion SIEMPRE es case-insensitive. CRITICO — MENSAJES EN LA COLUMNA expected_error: PROCESO OBLIGATORIO: (1) localiza el codigo de error (ej: 400) en 'responses' del endpoint, (2) ese codigo puede tener 'example' (unico) o 'examples' (lista de varios); lee TODOS los ejemplos disponibles, (3) para cada escenario negativo, elige el ejemplo cuyo mensaje de error corresponde al tipo de error que estas probando: si el escenario envia un campo faltante usa el ejemplo de campo faltante; si envia un amount invalido (cero o negativo) usa el ejemplo de amount invalido; si el escenario corresponde a un recurso no encontrado usa el ejemplo de not found, etc., (4) extrae el valor de detail.error del ejemplo elegido y usalo como expected_error. ESTA TERMINANTEMENTE PROHIBIDO: (a) usar siempre el primer ejemplo ignorando los demas, (b) inventar mensajes que no aparezcan en ningun example del contrato, (c) usar el mismo expected_error para todos los escenarios de un mismo codigo HTTP si el contrato tiene multiples ejemplos para ese codigo.
- OBLIGATORIO — STEPS NO AMBIGUOS Y SIN DUPLICADOS: (1) NUNCA crees dos steps cuyo texto del decorador sea identico o equivalente. Behave considera duplicado cualquier step cuyo patron de texto coincida, INDEPENDIENTEMENTE del tipo de comillas del decorador Python o del nombre de la funcion Python. Ejemplos de duplicados prohibidos: `@when('se envia una peticion ...')` y `@when("se envia una peticion ...")` son EL MISMO step; `@given('el body de creacion de pago ...')` y `@given("el body de creacion de pago ...")` son EL MISMO step; `@then('el campo ... de la respuesta es ...')` y `@then("el campo ... de la respuesta es ...")` son EL MISMO step. Si ambos existen, behave lanzara AmbiguousStep y NINGUNA prueba funcionara. (2) PROCESO OBLIGATORIO ANTES DE AÑADIR UN STEP: compara el texto del decorador que quieres añadir con CADA step en 'STEPS YA EXISTENTES', ignorando si usan comillas simples o dobles. Si el texto coincide, ese step YA EXISTE — NO lo incluyas en new_steps_added aunque quieras cambiar el nombre de la funcion o mejorar la implementacion. (3) Si TODOS los steps que necesita esta HU ya existen en 'STEPS YA EXISTENTES', el campo new_steps_added del JSON de respuesta debe ser una lista VACIA []. (4) Usa UN UNICO decorador por concepto: si necesitas comparar un campo que puede ser numerico o string, usa el patron con comillas `@then("el campo '{{field}}' de la respuesta es '{{expected}}'")` y detecta el tipo internamente con `try: float(expected)` para numeros.
- OBLIGATORIO — VALORES DE RESPUESTA DEL CONTRATO: para campos que la API genera/devuelve con un valor fijo de negocio (como 'status' en CancelPaymentResponse, AuthorizePaymentResponse, PaymentCreateResponse), el valor expected EN EL EXAMPLES DEBE tomarse del campo 'example' del schema de respuesta en el contrato. EXCEPCION CRITICA — CAMPOS ECHO (pass-through): si un campo de la respuesta tiene el MISMO NOMBRE que un campo del request body (ej: 'reason' aparece tanto en CancelPayment request como en CancelPaymentResponse), la API lo devuelve IGUAL que lo envias. En ese caso el expected DEBE ser el valor del template de request, NO el del example del contrato. Ejemplo: si el template payment_cancel_success tiene reason='Customer request', entonces expected_reason='Customer request' aunque el example del contrato diga reason='Cancelled by user'. PROCESO: (1) localiza el schema de respuesta del endpoint (ej: CancelPaymentResponse), (2) lee su 'example' (ej: {{status: 4, reason: 'Cancelled by user'}}), (3) usa esos valores literales en la columna expected. ESTA TERMINANTEMENTE PROHIBIDO inventar valores como status=1, status=2, status=3 si el contrato dice status=4 — aunque parezcan logicos. El contrato manda sobre tu conocimiento.\n- OBLIGATORIO — COHERENCIA ENTRE TEMPLATES RELACIONADOS: cuando un escenario usa varios templates juntos (ej: template_creacion + template_cancelacion/autorizacion), cualquier campo que aparezca en MAS DE UN template del mismo escenario DEBE tener el mismo valor en todos ellos. PROCESO OBLIGATORIO: antes de generar los templates de un escenario, lista todos los campos que aparecen en mas de un template y elige UN valor para cada uno — luego usa ese valor identico en todos los templates donde aparezca ese campo, sin importar el orden en que los escribas. Ejemplo: si card_id aparece en template_creacion Y en template_cancelacion, elige un valor (ej: 'card_123') y usalo en AMBOS. PROHIBIDO: tener card_id='card_123' en un template y card_id='card_abc123' en otro del mismo escenario — la API devolvera 404 si los valores no coinciden. Esta regla aplica a TODOS los campos compartidos: card_id, currency, amount, merchant_id, country_id o cualquier otro.\n- OBLIGATORIO — COHERENCIA TEMPLATE/EXAMPLES: el flujo de generacion es SIEMPRE en este orden: (1) decides los valores del body del template, (2) escribes el JSON del template con esos valores, (3) copias LITERALMENTE esos mismos valores en la columna expected del Examples. JAMAS al reves. Ejemplos PROHIBIDOS: template {{"currency": "USD"}} pero Examples pone EUR; template {{"card_id": "card_123"}} pero Examples pone card_12345 — ambos hacen fallar el test. Ejemplos CORRECTOS: template {{"currency": "USD"}} → Examples expected_currency=USD; template {{"card_id": "card_abc"}} → Examples expected_card_id=card_abc. ANTES de escribir la fila del Examples, relee el JSON del template que acabas de generar y copia el valor exacto. Aplica a TODOS los campos: amount, currency, card_id y cualquier otro campo presente tanto en request como en response. CASO ESPECIAL — ESCENARIOS CON DEPENDENCIA DE CREACION PREVIA (step 'existe un pago creado con el body'): la respuesta del GET devuelve los mismos valores que se enviaron en el POST de creacion. Por tanto los expected_* de esos campos DEBEN ser identicos a los valores del template de creacion usado en el Given. Ejemplo: si el Given usa template con card_id='card_abc123', country_id='ES', merchant_id='merchant_xyz789', entonces expected_card_id=card_abc123, expected_country_id=ES, expected_merchant_id=merchant_xyz789 — PROHIBIDO poner valores distintos como card_det_001 o merchant_det_001.
- OBLIGATORIO — ACCESO A LA RESPUESTA: para leer campos de la respuesta HTTP SIEMPRE usa `response_data = context.response.json()` dentro del step. NUNCA uses `context.response_data` (ese atributo no existe en el contexto behave). El unico atributo disponible es `context.response` (objeto requests.Response).
- OBLIGATORIO — COMPARACION DE CAMPOS NUMERICOS: cuando el contrato define un campo como `number` o `integer` (p.ej. `amount`, `status`), el step DEBE comparar usando tipos numericos: `float(response_data[field]) == float(expected)` para decimales, `int(response_data[field]) == int(expected)` para enteros. NUNCA compares numeros como strings (`str(response_data[field]) == '100.50'`): eso falla porque `str(100.5)` es `'100.5'`, no `'100.50'`. Si el valor llega del feature como string (parametro de Examples), conviertelo a float/int antes de comparar.
- DEPENDENCIAS ENTRE ENDPOINTS: REGLA GENERICA — si el escenario happy path de un endpoint necesita que existan datos en la base de datos para devolver 200 (es decir, el endpoint consulta, modifica u opera sobre un recurso que alguien tiene que haber creado antes), el Given DEL HAPPY PATH DEBE crear ese recurso primero. PROCESO: (1) determina si el endpoint puede devolver 200 sin datos previos (ej: healthcheck, POST de creacion) — si puede, no necesita Given de creacion; (2) si el endpoint necesita datos previos (ej: GET por id, GET por card_id, POST de accion sobre un recurso existente), usa el step "existe un pago creado con el body '{{template_creacion}}'" como Given — ese step hace POST /payments, guarda context.card_id desde la respuesta, luego hace GET /payments/card/{{card_id}} y guarda context.payment_id del ultimo elemento. (3) NUNCA uses "el body de creacion de pago" como Given en un happy path que necesita datos en la BD — ese step SOLO carga el template en memoria sin hacer ninguna llamada HTTP, por lo que la API devolvera 404 o lista vacia. El step When sustituye automaticamente los placeholders literales {{payment_id}} y {{card_id}} en la URL con context.payment_id y context.card_id respectivamente. Si en 'STEPS YA EXISTENTES' ya existe el step "existe un pago creado con el body '{{template_creacion}}'" REUTILIZALO siempre que el happy path necesite datos previos.
- OBLIGATORIO — ESCENARIOS CON ESTADO PREVIO (CONFLICTO/DUPLICADO): algunos escenarios negativos necesitan que la API este en un estado concreto ANTES del When. Por ejemplo, "autorizar un pago ya autorizado" (409) o "cancelar un pago ya cancelado" (409). Para estos escenarios NO basta con cargar un template en memoria — hay que llamar a la API para establecer el estado. PROCESO OBLIGATORIO: (1) detecta si el escenario describe un recurso que ya ha sufrido la operacion antes (palabras clave: 'ya autorizado', 'ya cancelado', 'duplicado', 'ya procesado', 'conflict'); (2) el Given DEBE tener DOS pasos: primero "existe un pago creado con el body '<template_creacion>'" para crear el pago, y luego un segundo Given que ejecute la operacion previa realmente — ese step DEBE llamar a la API Y ADEMAS actualizar context.request_body con el body que acaba de cargar, por ejemplo para pre-autorizar: `base_url = os.getenv('BASE_URL','http://localhost:8000'); r = requests.post(f'{{base_url}}/payments/{{context.payment_id}}/authorize', json=body, timeout=30); assert r.status_code == 200; context.request_body = body`. CRITICO: si el step de pre-condicion NO asigna context.request_body = body, el step When usara el ultimo body que haya en context (tipicamente el body de creacion del pago, que carece de campos como 'card_id' para authorize o 'reason' para cancel) y la API devolvera 400 'Mandatory field not informed' en lugar de 409; (3) el nombre del step de pre-condicion debe ser descriptivo: "el pago ha sido autorizado previamente con el body '<template>'" o "el pago ha sido cancelado previamente con el body '<template>'"; (4) este step va en new_steps_added si no existe en STEPS YA EXISTENTES; (5) el When a continuacion ejecuta la MISMA operacion otra vez para provocar el conflicto. CRITICO: si el step de pre-condicion solo carga el template sin llamar a la API, el When ejecutara la primera operacion (devuelve 200) en lugar de la segunda (devuelve 409) y el test siempre fallara.
- OBLIGATORIO — STATUS CODES SOLO DEL CONTRATO: los codigos HTTP que uses en expected_status DEBEN salir EXCLUSIVAMENTE de 'responses' del endpoint en el contrato que se te pasa. NUNCA uses tu conocimiento general de FastAPI o HTTP para asignar codigos. Proceso obligatorio: (1) lee 'responses' del endpoint, (2) anota los codigos documentados (ej: 200, 400, 404), (3) SOLO esos codigos pueden aparecer como expected_status en los Examples. Si el contrato documenta 400 para campos faltantes, el expected_status DEBE ser 400, no 422 aunque FastAPI por defecto devolveria 422. Si el contrato no documenta 404 para un caso concreto, ese caso NO puede usar expected_status=404. Los errores de negocio que no tienen un codigo 4xx en el contrato DEBEN usar expected_status=200.
- OBLIGATORIO — ESCENARIOS NEGATIVOS SOLO SI ESTAN EN EL CONTRATO: un escenario negativo (que espera 4xx/5xx) SOLO puede generarse si ese codigo de error esta documentado en el contrato para ese endpoint. ESTA TERMINANTEMENTE PROHIBIDO inventar escenarios de error para casos que el contrato no documenta: por ejemplo si el contrato de POST /payments no tiene ningun codigo de error para 'currency invalida', 'card_id vacio' o 'country invalido', NO generes esos escenarios negativos — la API simplemente los acepta y devuelve 200. Limitate a los casos de error documentados: tipicamente campos obligatorios faltantes (400) y path params invalidos (400).
- OBLIGATORIO — CAMPOS DINAMICOS EN RESPUESTA: algunos campos de la respuesta son generados dinamicamente por la API (IDs auto-incrementales, timestamps, tokens) y su valor exacto NO se puede conocer de antemano. Identificalos por estas caracteristicas: (1) son de tipo integer/string y aparecen en response_fields pero NO en el request body (ej: payment_id en AuthorizePaymentResponse o CancelPaymentResponse — el cliente no lo envia, la API lo genera), (2) o su valor depende de datos creados en el Given. Para estos campos ESTA TERMINANTEMENTE PROHIBIDO poner su nombre como placeholder literal (ej: '{{payment_id}}', '{{id}}') ni un valor inventado en la columna expected del Examples — el test fallara siempre. ADEMAS, ESTA ABSOLUTAMENTE PROHIBIDO usar palabras centinela como 'dynamic', 'auto', 'generated', 'N/A', 'any', 'ignore' o cualquier otra palabra que no sea un valor real comparable — el step intentara comparar esa cadena literalmente con el valor de la API y fallara. En su lugar: (a) si en STEPS YA EXISTENTES existe un step especifico que valide ese campo contra el contexto (ej: "el campo 'payment_id' de la respuesta coincide con el pago creado"), USALO; (b) si no existe ese step, OMITE la validacion de ese campo en el Then y valida solo el status code y los demas campos estaticos conocidos. NUNCA incluyas una columna expected para un campo dinamico si no tienes un step que lo maneje correctamente.

Información de la HU:
{json.dumps(hu, indent=2, ensure_ascii=False)}

Los endpoints del swagger disponibles son:
{json.dumps(swagger_endpoints, indent=2, ensure_ascii=False) if isinstance(swagger_endpoints, list) else str(swagger_endpoints)}

TEMPLATES JSON YA EXISTENTES para esta HU (contenido real de los archivos en tests/templates/HU{hu.get('ref','')}):
{templates_existentes if templates_existentes else "(ninguno aun)"}
IMPORTANTE: si regeneras o reutilizas un template que ya existe, usa EXACTAMENTE los mismos valores que tiene. Si creas uno nuevo, asegurate de que los valores del body coincidan con los expected del Examples.

STEPS YA EXISTENTES en tests/steps/steps.py (NO los redefinas; reutiliza su texto EXACTO en los escenarios):
{json.dumps(pasos_existentes, indent=2, ensure_ascii=False) if pasos_existentes else "(ninguno por ahora)"}
{bloque_feedback}
Devuelve el resultado en un formato json estructurado EXACTAMENTE así (sin ningun otro caracter):
{{
    "hu_title": "{hu_title}",
    "feature": "nombre descriptivo del feature",
    "scenarios": [
        "Scenario: nombre descriptivo del escenario",
        "    Given precondicion basada en la HU",
        "    When accion a ejecutar",
        "    Then resultado esperado"
    ],
    "endpoints_swagger": [
        {{"method": "GET", "path": "/endpoint", "summary": "descripción"}}
    ],
    "steps_imports": [
        "from behave import given, when, then",
        "import requests",
        "import json",
        "import os",
        "import glob"
    ],
    "steps_functions": [
        "@given(\"descripcion del given con parametro '{{param}}'\")",
        "def step_given_descriptivo(context, param):",
        "    templates_root = os.path.join(os.path.dirname(__file__), '..', 'templates')",
        "    coincidencias = glob.glob(os.path.join(templates_root, '**', param + '.json'), recursive=True)",
        "    assert coincidencias, f'No se encontro el template {{param}}.json'",
        "    with open(coincidencias[0], encoding='utf-8') as f:",
        "        context.request_body = json.load(f)"
    ],
    "new_steps_added": [
        "@when('descripcion de la accion a ejecutar')",
        "def step_when_descriptivo(context):",
        "    context.response = requests.post(URL_ENDPOINT, json=context.request_body, timeout=30)",
        "",
        "@then('descripcion del resultado esperado {{status_code:d}}')",
        "def step_then_descriptivo(context, status_code):",
        "    assert context.response.status_code == status_code"
    ],
    "templates": [
        {{
            "template_name": "descriptive_name_based_on_hu_purpose",
            "body": {{
                "ATENCION_CRITICA": "ANTES de rellenar este body, consulta 'request_template.fields' del endpoint en el swagger. Copia cada clave LITERALMENTE. PROHIBIDO añadir sufijos (_id, _code, etc.) ni abreviar. Si el contrato dice 'currency', escribe 'currency', no 'currency_id'.",
                "field1_exacto_del_contrato": "value1",
                "field2_exacto_del_contrato": "value2"
            }}
        }}
    ]
}}"""
            return prompt
        except Exception as e:
            logger.error(f"Error al construir prompt: {str(e)}")
            raise

    @staticmethod
    def _construir_bloque_feedback(feedback: dict) -> str:
        """Construye un bloque de prompt con los casos que el validador pide cubrir."""
        if not feedback:
            return ""
        sin_cubrir = feedback.get("criterios_sin_cubrir", []) or []
        faltantes = feedback.get("escenarios_faltantes", []) or []
        sugerencias = feedback.get("improvement_suggestions", []) or []
        errores_estaticos = feedback.get("errores_estaticos", []) or []
        errores_funcionales = feedback.get("errores_funcionales", []) or []
        advertencias = feedback.get("advertencias", []) or []
        if not (sin_cubrir or faltantes or sugerencias or errores_estaticos or errores_funcionales or advertencias):
            return ""
        lineas = [
            "",
            "OBLIGATORIO - REGENERACION SOLICITADA: el usuario ha pedido explicitamente mejorar el feature.",
            "DEBES aplicar TODOS los cambios listados a continuacion, sin excepcion.",
            "Mantén TODOS los escenarios validos previos y NO elimines ninguno correcto.",
        ]
        if errores_estaticos:
            lineas.append("OBLIGATORIO — corrige estos errores de validacion en el feature/steps:")
            lineas.extend(f"  - {e}" for e in errores_estaticos)
        if errores_funcionales:
            lineas.append("OBLIGATORIO — corrige estos errores funcionales respaldados por la HU y el contrato:")
            lineas.extend(f"  - {e}" for e in errores_funcionales)
        if sin_cubrir:
            lineas.append("OBLIGATORIO — anade un escenario para cada criterio sin cubrir:")
            lineas.extend(f"  - {c}" for c in sin_cubrir)
        if faltantes:
            lineas.append("OBLIGATORIO — anade estos escenarios que faltan (positivos/negativos):")
            lineas.extend(f"  - {e}" for e in faltantes)
        if sugerencias:
            lineas.append("OBLIGATORIO — aplica TODAS estas mejoras al feature regenerado (NO son opcionales):")
            lineas.extend(f"  - {s}" for s in sugerencias)
        if advertencias:
            lineas.append("REVISA estas advertencias. Corrige solo problemas verificables con la HU o el contrato; no inventes datos ni cambies un mensaje de error correcto solo porque no aparece en los ejemplos OpenAPI:")
            lineas.extend(f"  - {w}" for w in advertencias)
        lineas.append("")
        return "\n".join(lineas)

    def generate_feature(self, hu: dict, feedback: dict = None, endpoints: list = None) -> dict:
        """
        Genera feature Gherkin y steps a partir de una HU.
        
        Args:
            hu: Diccionario con información de la Historia de Usuario
            feedback: Opcional. Casos faltantes detectados por el validador a cubrir.
            endpoints: Opcional. Endpoints ya normalizados (flujo YAML). Si es None,
                se obtienen del swagger en vivo (flujo HU).
            
        Returns:
            dict: Respuesta estructurada de Claude con feature y steps
            
        Raises:
            Exception: Si hay error en la generación
        """
        try:
            logger.info(f"Generando feature para {hu.get('ref', 'UNKNOWN')}...")
            prompt = self.buildPrompt(hu, feedback, endpoints)
            logger.debug(f"Prompt enviado a Claude (primeros 500 chars): {prompt[:500]!s}")
            
            # Pedimos a Claude una respuesta JSON estructurada
            try:
                with contexto_llm(hu.get('ref', 'UNKNOWN'), "Refinamiento" if feedback else "Generación"):
                    result = self.model.generate_json(prompt)
                # Validar que tenga la estructura esperada
                if self._is_valid_structure(result):
                    logger.info(f"JSON estructurado validado correctamente para {hu.get('ref', 'UNKNOWN')}")
                    return result
                else:
                    logger.warning("JSON no tiene la estructura esperada, intentando fallback")
                    return self._generate_structured_fallback(hu, result)
            except Exception as gen_exc:
                logger.warning(f"generate_json falló: {gen_exc}; intentando fallback a texto crudo")
                # Fallback: si no se obtuvo JSON directo, solicitar texto y tratar de parsear
                with contexto_llm(hu.get('ref', 'UNKNOWN'), "Refinamiento" if feedback else "Generación"):
                    raw = self.model.generate_feature(prompt)
                logger.debug(f"Respuesta cruda de Claude (primeros 1000 chars): {raw[:1000]!s}")
                
                # Intentar extraer JSON embebido
                m = re.search(r"[{].*[}]", raw, re.DOTALL)
                if m:
                    try:
                        result = json.loads(m.group(0))
                        if self._is_valid_structure(result):
                            logger.info("JSON extraído y estructurado correctamente")
                            return result
                        else:
                            logger.warning("JSON extraído pero sin estructura esperada")
                            return self._generate_structured_fallback(hu, result)
                    except Exception as parse_exc:
                        logger.error(f"No se pudo parsear JSON embebido: {parse_exc}")
                        return self._generate_structured_fallback(hu, raw)
                else:
                    logger.error("No se encontró JSON en la respuesta cruda")
                    return self._generate_structured_fallback(hu, raw)

        except Exception as e:
            logger.error(f"Error al generar feature: {str(e)}")
            raise

    def _is_valid_structure(self, data: dict) -> bool:
        """Valida que el dict tenga la estructura esperada."""
        if not isinstance(data, dict):
            return False
        required_keys = {"hu_title", "feature", "scenarios", "templates", "endpoints_swagger"}
        if not required_keys.issubset(data):
            return False
        # Debe traer el codigo de los steps en alguno de los formatos soportados
        return any(key in data for key in ("steps_code", "steps_functions", "new_steps_added"))

    def _leer_steps_existentes(self) -> list:
        """Devuelve la lista de decoradores de step (@given/@when/@then) ya definidos en steps.py.

        Si steps.py aun no existe (primera HU del proyecto), devuelve una lista vacia
        para que el modelo genere todos los steps necesarios desde cero.
        """
        steps_file = Path("tests/steps/steps.py")
        if not steps_file.exists():
            logger.info("steps.py no existe todavia; no hay steps previos que reutilizar")
            return []
        try:
            contenido = steps_file.read_text(encoding="utf-8")
        except OSError:
            return []
        decoradores = []
        for ln in contenido.splitlines():
            decorador = ln.strip()
            if decorador.startswith(("@given", "@when", "@then")) and decorador not in decoradores:
                decoradores.append(decorador)
        return decoradores

    def _build_steps_code(self, feature_data: dict) -> str:
        """Obtiene el codigo de los steps NUEVOS que hay que escribir en el archivo.

        Solo se escriben los steps de 'new_steps_added' (los realmente nuevos).
        'steps_functions' es la lista completa de referencia (incluye reutilizados)
        y NO se escribe para evitar duplicar steps ya existentes. Si el modelo solo
        rellena 'steps_functions', se usa como respaldo.
        """
        steps_code = feature_data.get("steps_code")
        if isinstance(steps_code, str) and steps_code.strip():
            return steps_code.strip("\n")
        # Preferir 'new_steps_added'; usar 'steps_functions' solo como respaldo
        nuevos = feature_data.get("new_steps_added")
        tiene_nuevos = (isinstance(nuevos, list) and nuevos) or (
            isinstance(nuevos, str) and nuevos.strip()
        )
        clave = "new_steps_added" if tiene_nuevos else "steps_functions"
        lines: list = []
        value = feature_data.get(clave)
        if isinstance(value, list) and value:
            lines.extend(str(line) for line in value)
        elif isinstance(value, str) and value.strip():
            lines.append(value)
        return "\n".join(lines)

    @staticmethod
    def _split_step_blocks(code: str) -> list:
        """Divide el codigo en funciones de step; cada bloque empieza por su decorador."""
        blocks: list = []
        current: list = []
        has_def = False
        for line in code.splitlines():
            is_decorator = line.lstrip().startswith("@")
            if is_decorator and has_def:
                blocks.append("\n".join(current).rstrip())
                current = [line]
                has_def = False
            else:
                current.append(line)
                if line.lstrip().startswith("def "):
                    has_def = True
        if current:
            blocks.append("\n".join(current).rstrip())
        return [b for b in blocks if b.strip()]

    @staticmethod
    def _normalizar_decorador(decorador: str) -> str:
        """Normaliza un decorador para comparar steps ignorando comillas y espacios."""
        texto = decorador.strip()
        # Primero eliminar backslashes de comillas escapadas (\' -> ' y \" -> ")
        texto = texto.replace("\\'", "'").replace('\\"', '"')
        # Luego unificar todas las comillas a simples y normalizar espacios
        texto = texto.replace('"', "'")
        return " ".join(texto.split())

    def _filter_new_step_blocks(self, steps_code: str, existing_content: str) -> list:
        """Filtra las funciones de step cuyo decorador ya existe en el archivo o se repite en la generacion."""
        new_blocks: list = []
        # Decoradores ya presentes en el archivo
        vistos = {
            self._normalizar_decorador(ln)
            for ln in existing_content.splitlines()
            if ln.lstrip().startswith("@")
        }
        for block in self._split_step_blocks(steps_code):
            decorators = [
                self._normalizar_decorador(ln)
                for ln in block.splitlines()
                if ln.lstrip().startswith("@")
            ]
            if any(dec and dec in vistos for dec in decorators):
                ref = decorators[0] if decorators else block[:60]
                logger.info(f"Step duplicado, no se escribira: {ref[:60]}")
                continue
            new_blocks.append(block)
            # Registrar los decoradores recien anadidos para evitar duplicados en el mismo lote
            vistos.update(dec for dec in decorators if dec)
        return new_blocks

    def _generate_structured_fallback(self, hu: dict, raw_data=None) -> dict:
        """Genera un dict con estructura completa cuando Claude falla."""
        if self.strict_mode:
            raise ValueError("El LLM no devolvió artefactos válidos: no se admiten artefactos de relleno en modo estricto")
        hu_id = hu.get('ref', 'UNKNOWN')
        subj = hu.get('subject', 'Feature sin titulo')
        desc = hu.get('description', '')
        
        logger.info(f"Generando estructura completa de fallback para {hu_id}")
        
        return {
            'hu_title': subj,
            'feature': subj,
            'scenarios': [
                f"Feature: {subj}",
                "",
                f"Scenario: {subj}",
                "    Given una precondicion basada en la HU",
                "    When se ejecuta la accion",
                "    Then se observa el resultado esperado"
            ],
            'endpoints_swagger': [],
            'steps_imports': [
                "from behave import given, when, then",
                "import requests",
                "import json",
                "import os"
            ],
            'steps_functions': [
                "@given('una precondicion')",
                "def step_precondition(context):",
                "    pass"
            ],
            'new_steps_added': [
                "@when('se ejecuta una accion')",
                "def step_action(context):",
                "    pass",
                "",
                "@then('se observa el resultado')",
                "def step_result(context):",
                "    pass"
            ],
            'templates': [
                {
                    "template_name": f"fallback_hu{hu_id}",
                    "body": {"status": "success", "message": "Fallback template"}
                }
            ]
        }

    def save_feature(self, hu: dict, feature_data: dict) -> str:
        """
        Guarda el feature generado en archivos .feature y .py.
        
        Args:
            hu: Diccionario con información de la HU
            feature_data: Diccionario con feature, scenarios y steps
            
        Returns:
            str: Ruta del archivo guardado
            
        Raises:
            Exception: Si hay error al guardar los archivos
        """
        try:
            hu_id = hu.get("ref", "unknown")
            import unicodedata as _ud
            _raw = hu.get("subject", "").replace(" ", "_").replace("/", "_")
            hu_subject = _ud.normalize("NFD", _raw).encode("ascii", "ignore").decode("ascii")[:30]
            
            # Crear directorio de salida (subcarpeta por HU o por endpoint en flujo YAML)
            _es_hu = str(hu_id).isdigit()
            output_dir = Path("tests/features") / (f"HU{hu_id}" if _es_hu else str(hu_id))
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Guardar feature
            scenarios_list = feature_data.get("scenarios", [])
            feature_content = f"Feature: {feature_data.get('feature', 'Feature')}\n\n"
            if isinstance(scenarios_list, list):
                # Evitar 'Feature:' duplicado: el titulo ya se escribe arriba
                scenarios_filtradas = [s for s in scenarios_list if not str(s).lstrip().startswith("Feature:")]
                feature_content += "\n".join(scenarios_filtradas) + "\n"
            else:
                scenarios_text = "\n".join(
                    ln for ln in str(scenarios_list).splitlines() if not ln.lstrip().startswith("Feature:")
                )
                feature_content += scenarios_text + "\n"
            
            feature_file = output_dir / (f"hu{hu_id}_{hu_subject}.feature" if _es_hu else f"{hu_id}_{hu_subject}.feature")
            with open(feature_file, "w", encoding="utf-8") as f:
                f.write(feature_content)
            logger.info(f"Feature guardado en: {feature_file}")
            
            # ===== GUARDAR STEPS =====
            steps_dir = Path("tests/steps")
            steps_dir.mkdir(parents=True, exist_ok=True)
            steps_file = steps_dir / "steps.py"
            
            # Leer contenido existente del archivo steps.py
            existing_content = ""
            if steps_file.exists():
                with open(steps_file, "r", encoding="utf-8") as f:
                    existing_content = f.read()
            
            # Obtener imports y el bloque de codigo de los steps
            steps_imports = feature_data.get("steps_imports", [])
            steps_code = self._build_steps_code(feature_data)
            new_step_blocks = self._filter_new_step_blocks(steps_code, existing_content)
            
            # Escribir solo los imports que no existan ya en el archivo
            missing_imports = [
                imp for imp in steps_imports
                if str(imp).strip() and str(imp).strip() not in existing_content
            ]

            with open(steps_file, "a", encoding="utf-8") as f:
                if missing_imports:
                    f.write("\n")
                    for imp in missing_imports:
                        f.write(str(imp) + "\n")
                    f.write("\n")
                
                # Escribir solo las funciones de step que no existan ya, preservando indentacion
                if new_step_blocks:
                    _label = f"HU-{hu_id}" if _es_hu else hu_id
                    f.write(f"\n# ===== NUEVOS STEPS para {_label} =====\n")
                    f.write("\n\n".join(new_step_blocks) + "\n")
                else:
                    logger.info("Todos los steps ya existian; no se escribieron nuevos")
            
            logger.info(f"Steps guardados en: {steps_file}")

            # ===== GUARDAR TEMPLATES (subcarpeta por HU o por endpoint en flujo YAML) =====
            template_dir = Path("tests/templates") / (f"HU{hu_id}" if _es_hu else str(hu_id))
            template_dir.mkdir(parents=True, exist_ok=True)
            
            templates_list = feature_data.get("templates", [])
            if templates_list and len(templates_list) > 0:
                for template_item in templates_list:
                    try:
                        if isinstance(template_item, dict):
                            template_name = template_item.get("template_name", f"template_hu{hu_id}")
                            # Normalizar prefijo del template_name:
                            # Flujo HU: reemplazar hu<numeros> por hu{id} correcto
                            # Flujo YAML: eliminar prefijo hu<numeros> si Claude lo añadió
                            if _es_hu:
                                template_name = re.sub(r'^hu\d+_', f'hu{hu_id}_', template_name)
                            else:
                                template_name = re.sub(r'^hu\d+_', '', template_name)
                            template_body = template_item.get("body", {})
                        else:
                            continue
                        
                        template_file = template_dir / f"{template_name}.json"
                        
                        # Verificar si el template ya existe para no duplicar
                        if template_file.exists():
                            logger.info(f"Template ya existe, no se sobrescribirá: {template_name}")
                            continue
                        
                        with open(template_file, "w", encoding="utf-8") as f:
                            f.write(json.dumps(template_body, indent=2, ensure_ascii=False))
                        logger.info(f"Template guardado en: {template_file}")
                    except Exception as t_err:
                        logger.error(f"Error guardando template: {t_err}")
            
            return str(feature_file)
        except Exception as e:
            logger.error(f"Error al guardar feature: {str(e)}")
            raise

    @staticmethod
    def _endpoints_de_schema(data: dict) -> list:
        """Extrae los endpoints (metodo, path, summary, codigos) de un esquema OpenAPI."""
        metodos_validos = {"get", "post", "put", "delete", "patch"}
        endpoints = []
        for path, methods in (data.get("paths", {}) or {}).items():
            if not isinstance(methods, dict):
                continue
            for method, info in methods.items():
                if method.lower() not in metodos_validos:
                    continue
                info = info if isinstance(info, dict) else {}
                endpoints.append({
                    "method": method.upper(),
                    "path": path,
                    "summary": info.get("summary", ""),
                    "responses": [str(c) for c in (info.get("responses", {}) or {}).keys()],
                })
        return endpoints

    @staticmethod
    def _ref_endpoint(ep: dict) -> str:
        """Genera un identificador de feature estable y seguro para un endpoint."""
        crudo = f"{ep['method']}_{ep['path']}"
        return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", crudo)).strip("_")

    def endpoints_sin_feature(self, data: dict) -> list:
        """Devuelve [(ref, endpoint)] de los endpoints del esquema que aun no tienen feature."""
        features_dir = Path("tests/features")
        pendientes = []
        for ep in self._endpoints_de_schema(data):
            ref = self._ref_endpoint(ep)
            # Flujo YAML: ref no numerico -> archivos sin prefijo hu
            patron = f"hu{ref}_*.feature" if str(ref).isdigit() else f"{ref}_*.feature"
            existe = list(features_dir.rglob(patron)) if features_dir.exists() else []
            if not existe:
                pendientes.append((ref, ep))
        return pendientes

    def generar_feature_endpoint(self, ref: str, ep: dict, endpoints: list = None) -> tuple:
        """Sintetiza una HU a partir de un endpoint, genera y guarda su feature.

        Args:
            ref: Identificador del endpoint.
            ep: Endpoint del esquema (metodo, path, summary, codigos).
            endpoints: Opcional. Contrato normalizado del YAML para pasar al prompt
                sin llamar a la URL del swagger.

        Returns:
            tuple: (hu, feature_result, feature_path)
        """
        codigos = list(ep.get("responses", {}).keys()) if isinstance(ep.get("responses"), dict) else list(ep.get("responses", []))
        codigos_error = [c for c in codigos if str(c).startswith(("4", "5"))]
        hu = {
            "ref": ref,
            "subject": ep.get("summary") or f"{ep['method']} {ep['path']}",
            "description": (
                f"Endpoint {ep['method']} {ep['path']}. {ep.get('summary', '')}. "
                f"Genera UNICAMENTE los escenarios basicos: "
                f"(1) UN escenario de exito (happy path) con el codigo {next((c for c in codigos if str(c).startswith('2')), '200')}. "
                + (
                    f"(2) UN escenario por cada codigo de error documentado: {', '.join(str(c) for c in codigos_error)}. "
                    if codigos_error else ""
                )
                + f"NO generes variantes adicionales ni escenarios para casos no documentados en el contrato."
            ),
        }
        feature_result = self.generate_feature(hu, endpoints=endpoints)
        feature_path = self.save_feature(hu, feature_result)
        return hu, feature_result, feature_path

    def generar_escenarios_no_cubiertos(self, data: dict) -> list:
        """Genera features para los endpoints del esquema que aun no tienen feature.

        Solo genera escenarios para endpoints no cubiertos (los ya generados se omiten).
        Usa el contrato del YAML ya cargado (normalizado) como contexto, sin llamar a la URL.

        Returns:
            list: [(hu, feature_result, feature_path)] de los features generados.
        """
        pendientes = self.endpoints_sin_feature(data)
        if not pendientes:
            print("   ✅ Todos los endpoints del esquema ya tienen escenarios generados.")
            return []

        # Contrato normalizado (con request_template) que se pasara al prompt
        endpoints_normalizados = SwaggerReader.normalizar_endpoints(data)

        print(f"   ℹ️  Generando escenarios para los endpoints sin feature ({len(pendientes)}):")
        for _, ep in pendientes:
            print(f"        - {ep['method']} {ep['path']}")

        generados = []
        for ref, ep in pendientes:
            print(f"   🔄 Generando escenarios para {ep['method']} {ep['path']}...")
            try:
                hu, feature_result, feature_path = self.generar_feature_endpoint(
                    ref, ep, endpoints=endpoints_normalizados
                )
                print(f"   ✅ Feature generado en: {feature_path}")
                generados.append((hu, feature_result, feature_path))
            except Exception as exc:
                logger.error(f"Error generando escenarios para {ep['method']} {ep['path']}: {exc}")
                print(f"   ❌ Error generando escenarios para {ep['method']} {ep['path']}")
        return generados

    @staticmethod
    def faltantes_de_validacion(validation_result: dict) -> dict:
        """Extrae del resultado de validacion los casos faltantes a regenerar.

        Devuelve feedback accionable (criterios sin cubrir, escenarios faltantes,
        sugerencias y errores). Las advertencias solo se incluyen como contexto
        adicional si hay algo que corregir; por si solas no activan el refinamiento.
        """
        semantica = validation_result.get("semantic_validation", {}) or {}
        sin_cubrir = semantica.get("criterios_sin_cubrir", []) or []
        faltantes = semantica.get("escenarios_faltantes", []) or []
        sugerencias = semantica.get("improvement_suggestions", []) or []
        errores_funcionales = semantica.get("errors", []) or []
        advertencias = list(semantica.get("warnings", []) or [])

        # Incluir errores de las capas estaticas (sint, cohesion, tecnica)
        validaciones = validation_result.get("validaciones") or {}
        errores_estaticos = []
        for capa in ("sintactica", "cohesion", "tecnica"):
            capa_obj = validaciones.get(capa) or {}
            if not capa_obj.get("valid"):
                errores_estaticos.extend(capa_obj.get("errors") or [])
            advertencias.extend(capa_obj.get("warnings") or [])

        if not (sin_cubrir or faltantes or sugerencias or errores_estaticos or errores_funcionales):
            return {}
        return {
            "criterios_sin_cubrir": sin_cubrir,
            "escenarios_faltantes": faltantes,
            "improvement_suggestions": sugerencias,
            "errores_estaticos": errores_estaticos,
            "errores_funcionales": errores_funcionales,
            "advertencias": advertencias,
        }

    def regenerar_con_feedback(self, hu: dict, feedback: dict, endpoints: list = None) -> tuple:
        """Regenera y guarda el feature de una HU cubriendo los casos del feedback.

        Args:
            endpoints: Opcional. Contrato normalizado del YAML para no llamar al live swagger.

        Returns:
            tuple: (feature_result, feature_path)
        """
        feature_result = self.generate_feature(hu, feedback=feedback, endpoints=endpoints)
        feature_path = self.save_feature(hu, feature_result)
        return feature_result, feature_path

    def process_hu(self, hu: dict) -> dict:
        """
        Procesa una HU completa: genera y guarda el feature.
        
        Args:
            hu: Diccionario con información de la Historia de Usuario
            
        Returns:
            dict: Resultado con rutas de archivos guardados
            
        Raises:
            Exception: Si hay error en el procesamiento
        """
        try:
            logger.info(f"Procesando HU #{hu.get('ref', 'UNKNOWN')}...")
            feature_data = self.generate_feature(hu)
            feature_path = self.save_feature(hu, feature_data)
            
            result = {
                "hu_id": hu.get("ref"),
                "hu_subject": hu.get("subject"),
                "feature_file": feature_path,
                "status": "success"
            }
            logger.info(f"HU #{hu.get('ref', 'UNKNOWN')} procesada correctamente")
            return result
        except Exception as e:
            logger.error(f"Error al procesar HU: {str(e)}")
            return {
                "hu_id": hu.get("ref"),
                "status": "error",
                "error": str(e)
            }


if __name__ == '__main__':
    from clients.ingest import extract_info
    
    try:
        # Ejemplo: procesar HU#1
        hu_number = 1
        hu_data = extract_info(hu_number)
        
        if hu_data:
            generator = AgentGenerator()
            result = generator.process_hu(hu_data[0])
            print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.error(f"Error en ejecución: {str(e)}")
