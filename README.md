# Testing Payment - Guía de Configuración

## Requisitos

- Python 3.10+
- Pip (gestor de paquetes)
- Credenciales de:
  - **Claude API** (Anthropic)
  - **Taiga** (Project Management)
  - **OpenAPI/Swagger** (Para validar endpoints)

## Instalación

### 1. Clonar/Descargar el proyecto

```bash
cd C:\Users\USUARIO\PycharmProjects\testing_payment
```

### 2. Crear ambiente virtual (recomendado)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

Si no existe `requirements.txt`, instala manualmente:

```bash
pip install anthropic requests python-dotenv behave
```

### 4. Configurar variables de entorno

**Opción A: Usando archivo .env (RECOMENDADO)**

```bash
# Copiar el archivo de ejemplo
cp .env.example .env

# Editar .env con tus credenciales
```

Contenido de `.env`:
```dotenv
CLAUDE_API_KEY=sk-ant-....tu-clave...
CLAUDE_MODEL=claude-sonnet-5-5
LLM_MAX_GENERATION_TOKENS=16000
LLM_MAX_VALIDATION_TOKENS=9000
TAIGA_USER=tu-email@taiga.io
TAIGA_PASS=tu-contraseña
```

**Opción B: Variables de entorno del sistema**

En PowerShell:
```powershell
$env:CLAUDE_API_KEY="sk-ant-...tu-clave..."
$env:CLAUDE_MODEL="claude-sonnet-5-5"
$env:TAIGA_USER="tu-email@taiga.io"
$env:TAIGA_PASS="tu-contraseña"
```

### 5. Instalar python-dotenv para cargar .env

```bash
pip install python-dotenv
```

Agregar al inicio de `main.py`:
```python
from dotenv import load_dotenv
load_dotenv()
```

## Usar los agentes

### Agent Ingest - Extraer HUs

```powershell
# Obtener HU#1
python agents/ingest.py

# Obtener HU#3
python agents/ingest.py 3
```

### Agent Generator - Generar Features

```powershell
# Generar feature para HU#1
python agents/agent_generator.py

# Generar para HU#5
python agents/agent_generator.py 5
```

### Agent Validator - Validar Features

```powershell
# Validar HU#1 (ingestar → generar → validar)
python agents/agent_validator.py

# Validar HU#2
python agents/agent_validator.py 2
```

## Estructura del proyecto

```
testing_payment/
├── agents/
│   ├── ingest.py            # Extrae HUs de Taiga
│   ├── agent_generator.py   # Genera features Gherkin
│   └── agent_validator.py   # Valida features generados
├── clients/
│   ├── claude.py            # Cliente Claude API
│   ├── swagger.py           # Lector de OpenAPI/Swagger
│   └── taigo_connect.py     # Cliente Taiga API
├── output/
│   ├── features/            # Features .feature generados
│   └── steps.py             # Steps de Behave generados
├── main.py                   # Punto de entrada principal
├── .env.example             # Plantilla de variables de entorno
└── requirements.txt         # Dependencias del proyecto
```

## Evaluación de eficiencia (TFM)

Ejecuta `python main.py` y selecciona una HU, el sprint activo o el esquema OpenAPI. Se utiliza exclusivamente Claude Sonnet para ambos agentes, con `claude-sonnet-5-5` por defecto (la versión configurada en el proyecto original). También se admite el identificador `claude-sonnet-5` si esa es la versión de la evaluación. Mantén el mismo identificador en todos los experimentos.

Las métricas se guardan en los mismos Excel que el informe de calidad: `output/metricas_HU.xlsx` (HU individual y sprint) y `output/metricas_OpenAPI.xlsx` (YAML). Se añaden dos hojas:

- **Eficiencia**: una fila por cada caso de uso ejecutado, con fecha, modelo, duración total, espera de decisiones humanas, duración del proceso sin dicha espera, tiempo de las peticiones, número de peticiones, tokens y coste estimado en USD.
- **Peticiones**: una fila por cada intento de llamada HTTP al SDK de Claude, incluyendo generación, validación del contrato, validación semántica, refinamiento y reintentos. Se identifica tanto el agente como la HU o endpoint, si procede.

El tiempo se mide con `time.perf_counter()`. La duración total incluye ingesta, generación, validación, refinamiento, ejecución de Behave (si se acepta) e informes normales, pero no la propia escritura de este nuevo Excel. Las respuestas incompletas y los reintentos también se contabilizan. No se confunde el límite de tokens del prompt con los tokens efectivos devueltos por `response.usage`.

Tarifas de referencia de la API directa de Anthropic (consultadas el 17/11/2026; USD por millón de tokens):

| Modelo | Entrada sin caché | Salida | Escritura caché 5 min | Escritura caché 1 h | Lectura caché |
| --- | ---: | ---: | ---: | ---: | ---: |
| Claude Sonnet 5.5 | 2 | 10 | 2.50 | 4 | 0.10 |
| Claude Sonnet 5 | 2 | 10 | 2.50 | 4 | 0.20 |

Fuente: [Anthropic: Pricing](https://platform.claude.com/docs/en/about-claude/pricing). El coste se obtiene multiplicando cada categoría de tokens por su tarifa y dividiendo entre 1.000.000. Es una estimación del coste de inferencia, no una factura (no incluye impuestos ni descuentos particulares). Si una petición no devuelve `usage`, aparece señalada y los totales se interpretan como parciales. El histórico no contiene claves ni contenidos de los prompts.

Para repetir la evaluación limpia únicamente los artefactos de test que corresponda, conservando el histórico de eficiencia. Cierra el Excel antes de ejecutar si lo tienes abierto. Las hojas de calidad (`Metricas validacion`, `Dashboard`, `Evaluacion`) se reinician para la nueva ejecución, como antes; las hojas `Eficiencia` y `Peticiones` conservan el histórico.

Las pruebas unitarias de medición no necesitan credenciales: `python -m unittest discover -s unit_tests -v`.

## Seguridad

⚠️ **IMPORTANTE:**
- ❌ NUNCA commits credenciales a Git
- ✅ Usa siempre variables de entorno o `.env` local
- ✅ Agrega `.env` a `.gitignore`
- ✅ Rota tus API Keys regularmente

## Solución de problemas

### Error: "CLAUDE_API_KEY no está configurada"

**Solución:**
1. Verifica que `.env` está en la carpeta raíz
2. Verifica que tienes `CLAUDE_API_KEY=...` en `.env`
3. Asegúrate de haber llamado `load_dotenv()`

### Error: "Error de autenticación"

**Solución:**
1. Verifica que tu API key es válida en https://console.anthropic.com/
2. Verifica que la API key no está expirada
3. Comprueba que tienes saldo en tu cuenta

### Error: "No se pudo conectar a Taiga"

**Solución:**
1. Verifica que TAIGA_URL es correcto
2. Verifica credenciales de Taiga (email y contraseña)
3. Comprueba que tienes acceso al proyecto "eduardogr92-pagos"

## Contacto

Para más ayuda, consulta la documentación oficial:
- [Anthropic Claude Docs](https://docs.anthropic.com/)
- [Taiga API Docs](https://taiga.io/support/)
- [Behave BDD](https://behave.readthedocs.io/)

