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
CLAUDE_MODEL=claude-3-5-sonnet-20241022
TAIGA_USER=tu-email@taiga.io
TAIGA_PASS=tu-contraseña
```

**Opción B: Variables de entorno del sistema**

En PowerShell:
```powershell
$env:CLAUDE_API_KEY="sk-ant-...tu-clave..."
$env:CLAUDE_MODEL="claude-3-5-sonnet-20241022"
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

