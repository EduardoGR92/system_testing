
from behave import given, when, then
import requests
import json
import os
import glob


# ===== NUEVOS STEPS para GET_payments_healthcheck =====
@given("la API de payments esta disponible")
def step_given_api_payments_disponible(context):
    context.base_url = os.getenv('BASE_URL', 'http://localhost:8000')
    assert context.base_url, 'No se ha definido la URL base de la API'

@when("se envia una peticion '{method}' al endpoint '{endpoint}'")
def step_when_enviar_peticion(context, method, endpoint):
    base_url = os.getenv('BASE_URL', 'http://localhost:8000')
    url = f'{base_url}{endpoint}'
    if '{card_id}' in url and hasattr(context, 'card_id'):
        url = url.replace('{card_id}', str(context.card_id))
    if '{payment_id}' in url and hasattr(context, 'payment_id'):
        url = url.replace('{payment_id}', str(context.payment_id))
    http_method = method.strip().upper()
    body = getattr(context, 'request_body', None)
    if http_method in ('POST', 'PUT', 'PATCH') and body is not None:
        context.response = requests.request(http_method, url, json=body, timeout=30)
    else:
        context.response = requests.request(http_method, url, timeout=30)

@then("el codigo de respuesta es '{expected_status}'")
def step_then_codigo_respuesta(context, expected_status):
    actual = context.response.status_code
    assert actual == int(expected_status), f'Se esperaba status {expected_status} pero se obtuvo {actual}: {context.response.text}'

@then("el campo '{field}' de la respuesta es '{expected}'")
def step_then_campo_respuesta(context, field, expected):
    response_data = context.response.json()
    assert field in response_data, f'El campo {field} no esta en la respuesta: {response_data}'
    actual = response_data[field]
    is_number = isinstance(actual, (int, float)) and not isinstance(actual, bool)
    if is_number:
        try:
            assert float(actual) == float(expected), f'Campo {field}: se esperaba {expected} pero se obtuvo {actual}'
            return
        except ValueError:
            pass
    assert str(actual).strip().lower() == str(expected).strip().lower(), f'Campo {field}: se esperaba {expected} pero se obtuvo {actual}'

# ===== NUEVOS STEPS para POST_payments =====
@given("el body de creacion de pago '{template}'")
def step_given_body_creacion_pago(context, template):
    templates_root = os.path.join(os.path.dirname(__file__), '..', 'templates')
    coincidencias = glob.glob(os.path.join(templates_root, '**', template + '.json'), recursive=True)
    assert coincidencias, f'No se encontro el template {template}.json'
    with open(coincidencias[0], encoding='utf-8') as f:
        context.request_body = json.load(f)

@then("el mensaje de error de la respuesta es '{expected_error}'")
def step_then_mensaje_error(context, expected_error):
    response_data = context.response.json()
    actual = None
    if isinstance(response_data, dict):
        actual = response_data.get('error')
        if actual is None:
            detail = response_data.get('detail')
            if isinstance(detail, dict):
                actual = detail.get('error')
            else:
                actual = detail
    assert actual is not None, f'No se encontro mensaje de error en la respuesta: {response_data}'
    assert str(actual).strip().lower() == str(expected_error).strip().lower(), f'Error esperado: {expected_error} pero se obtuvo: {actual}'

# ===== NUEVOS STEPS para GET_payments_card_card_id =====
@given("existe un pago creado con el body '{template}'")
def step_given_existe_pago_creado(context, template):
    templates_root = os.path.join(os.path.dirname(__file__), '..', 'templates')
    coincidencias = glob.glob(os.path.join(templates_root, '**', template + '.json'), recursive=True)
    assert coincidencias, f'No se encontro el template {template}.json'
    with open(coincidencias[0], encoding='utf-8') as f:
        body = json.load(f)
    base_url = os.getenv('BASE_URL', 'http://localhost:8000')
    r = requests.post(f'{base_url}/payments', json=body, timeout=30)
    assert r.status_code == 200, f'Error creando pago: {r.status_code} {r.text}'
    context.request_body = body
    context.card_id = r.json().get('card_id', body.get('card_id'))
    r2 = requests.get(f'{base_url}/payments/card/{context.card_id}', timeout=30)
    assert r2.status_code == 200, f'Error consultando pagos por card_id: {r2.status_code} {r2.text}'
    lista = r2.json()
    assert isinstance(lista, list) and len(lista) > 0, 'La lista de pagos esta vacia'
    context.payment_id = lista[-1]['payment_id']

@then("la respuesta es una lista con al menos un elemento")
def step_then_respuesta_lista(context):
    response_data = context.response.json()
    assert isinstance(response_data, list) and len(response_data) > 0, 'La respuesta no es una lista con elementos'
    pid = str(getattr(context, 'payment_id', None))
    match = next((e for e in response_data if str(e.get('payment_id', '')) == pid), None) if pid != 'None' else None
    context.current_item = match if match else response_data[-1]

@then("el primer elemento de la lista contiene el campo '{field}' con valor '{expected}'")
def step_then_elemento_lista_campo(context, field, expected):
    item = context.current_item
    assert field in item, f'El campo {field} no esta en el elemento de la lista: {item}'
    actual = item[field]
    try:
        assert float(actual) == float(expected), f'Campo {field}: esperado {expected} pero se obtuvo {actual}'
    except ValueError:
        assert str(actual).lower() == str(expected).lower(), f'Campo {field}: esperado {expected} pero se obtuvo {actual}'

# ===== NUEVOS STEPS para POST_payments_payment_id_cancel =====
@given("el body de cancelacion de pago '{template}'")
def step_given_body_cancelacion_pago(context, template):
    templates_root = os.path.join(os.path.dirname(__file__), '..', 'templates')
    coincidencias = glob.glob(os.path.join(templates_root, '**', template + '.json'), recursive=True)
    assert coincidencias, f'No se encontro el template {template}.json'
    with open(coincidencias[0], encoding='utf-8') as f:
        context.request_body = json.load(f)

@given("el pago ha sido cancelado previamente con el body '{template}'")
def step_given_pago_cancelado_previamente(context, template):
    templates_root = os.path.join(os.path.dirname(__file__), '..', 'templates')
    coincidencias = glob.glob(os.path.join(templates_root, '**', template + '.json'), recursive=True)
    assert coincidencias, f'No se encontro el template {template}.json'
    with open(coincidencias[0], encoding='utf-8') as f:
        body = json.load(f)
    base_url = os.getenv('BASE_URL', 'http://localhost:8000')
    r = requests.post(f'{base_url}/payments/{context.payment_id}/cancel', json=body, timeout=30)
    assert r.status_code == 200, f'Error en la cancelacion previa: {r.status_code} {r.text}'
    context.request_body = body

# ===== NUEVOS STEPS para POST_payments_payment_id_authorize =====
@given("el body de autorizacion de pago '{template}'")
def step_given_body_autorizacion(context, template):
    templates_root = os.path.join(os.path.dirname(__file__), '..', 'templates')
    coincidencias = glob.glob(os.path.join(templates_root, '**', template + '.json'), recursive=True)
    assert coincidencias, f'No se encontro el template {template}.json'
    with open(coincidencias[0], encoding='utf-8') as f:
        context.request_body = json.load(f)

@given("el pago ha sido autorizado previamente con el body '{template}'")
def step_given_pago_autorizado_previamente(context, template):
    templates_root = os.path.join(os.path.dirname(__file__), '..', 'templates')
    coincidencias = glob.glob(os.path.join(templates_root, '**', template + '.json'), recursive=True)
    assert coincidencias, f'No se encontro el template {template}.json'
    with open(coincidencias[0], encoding='utf-8') as f:
        body = json.load(f)
    base_url = os.getenv('BASE_URL', 'http://localhost:8000')
    r = requests.post(f'{base_url}/payments/{context.payment_id}/authorize', json=body, timeout=30)
    assert r.status_code == 200, f'Error en la pre-autorizacion: {r.status_code} {r.text}'
    context.request_body = body
