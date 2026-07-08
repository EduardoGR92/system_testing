#!/usr/bin/env python
"""
Script para configurar las variables de entorno de manera interactiva.
"""

import os
from pathlib import Path

def setup_env():
    """Configura el archivo .env de forma interactiva."""

    env_file = Path(".env")

    print("=" * 60)
    print("CONFIGURACIÓN DE VARIABLES DE ENTORNO")
    print("=" * 60)

    # CLAUDE API KEY
    print("\n1️⃣  CLAUDE API KEY")
    print("   - Ve a: https://console.anthropic.com/account/keys")
    print("   - Copia tu clave (comienza con 'sk-ant-')")
    print("   - Pégala aquí:")

    while True:
        claude_key = input("   CLAUDE_API_KEY: ").strip()
        if claude_key.startswith("sk-ant-") and len(claude_key) > 10:
            print("   ✅ Clave válida")
            break
        else:
            print("   ❌ Clave inválida. Debe empezar con 'sk-ant-'")


    claude_model = input("   CLAUDE_MODEL [claude-sonnet-4-5]: ").strip()
    if not claude_model:
        claude_model = "claude-sonnet-4-5"

    # TAIGA (opcional)
    print("\n3️⃣  TAIGA (OPCIONAL - dejar en blanco si no tienes)")
    taiga_user = input("   TAIGA_USER (email) [dejar en blanco]: ").strip()
    taiga_pass = input("   TAIGA_PASS (contraseña) [dejar en blanco]: ").strip()

    # Guardar en .env
    print("\n💾 Guardando configuración en .env...")

    env_content = f"""# Variables de entorno para testing_payment
# Generado por setup.py

# Claude API (REQUERIDO)
CLAUDE_API_KEY={claude_key}
CLAUDE_MODEL={claude_model}

# Taiga (Opcional)
TAIGA_URL=https://api.taiga.io/api/v1
TAIGA_USER={taiga_user if taiga_user else ""}
TAIGA_PASS={taiga_pass if taiga_pass else ""}

# Logging
LOG_LEVEL=INFO
"""

    with open(env_file, "w", encoding="utf-8") as f:
        f.write(env_content)

    print(f"✅ Archivo .env creado correctamente: {env_file.absolute()}")


if __name__ == "__main__":
    try:
        setup_env()
    except KeyboardInterrupt:
        print("\n❌ Configuración cancelada")
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")

