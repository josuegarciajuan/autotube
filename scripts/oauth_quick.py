#!/usr/bin/env python3
"""OAuth quick flow — generador de refresh token para Autotube.
Ejecuta ESTO en tu máquina local (oficina), NO en el server.

Uso:
    python3 oauth_quick.py

Esto:
1. Abre tu navegador → autorizas con la cuenta Google del canal
2. Google redirige a localhost → código capturado automáticamente
3. Imprime el refresh token → copias y pegas en el chat
"""

import json
import pickle
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

# ═══ CONFIGURACIÓN ═══════════════════════════════════════════
# Pegas aquí el client_secret JSON que te pasé
CLIENT_SECRET_JSON = {
    "installed": {
        "client_id": "415608242228-rmd6g2kgcl2s3086rjo90ijdv4sevege.apps.googleusercontent.com",
        "project_id": "youtube-uploads-automation",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "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",
        "redirect_uris": ["http://localhost"]
    }
}

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

# ═══ EJECUCIÓN ══════════════════════════════════════════════

print("\n" + "=" * 60)
print("  🔐 AUTOTUBE OAUTH — FLOW RÁPIDO")
print("=" * 60)
print()
print("1. Se va a abrir tu navegador...")
print("2. Elige la cuenta: tracatrack@gmail.com")
print("3. Si pregunta 'App no verificada' → Avanzado → Continuar")
print("4. Autoriza los permisos")
print("5. Google redirigirá a localhost → se captura automáticamente")
print()

# Guardar secret temporal
secret_path = Path("/tmp/autotube_client_secret.json")
secret_path.write_text(json.dumps(CLIENT_SECRET_JSON))

try:
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    flow.redirect_uri = "http://localhost"

    print("🟢 Abriendo navegador...")

    # run_local_server() abre navegador y captura el código automáticamente
    credentials = flow.run_local_server(
        port=8080,
        open_browser=True,
        authorization_prompt_message="",
        success_message="✅ Autorizado. Ya puedes cerrar esta ventana.",
    )

    # Extraer refresh token
    refresh_token = credentials.refresh_token
    token_info = {
        "token": credentials.token,
        "refresh_token": refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes),
    }

    print()
    print("=" * 60)
    print("  ✅ AUTORIZACIÓN COMPLETA")
    print("=" * 60)
    print()

    if refresh_token:
        print("📋 REFRESH TOKEN (copia esto y pásalo al chat):")
        print()
        print(refresh_token)
        print()
        print("─" * 60)
        print("⚠️  Copia SOLO el refresh token de arriba y pégalo en el chat")
        print("─" * 60)
    else:
        print("⚠️  No se obtuvo refresh token.")
        print("   Seguramente ya autorizaste antes esta app.")
        print()
        print("📋 TOKEN COMPLETO (copia todo el bloque de abajo):")
        print()
        print(json.dumps(token_info, indent=2))
        print()

    print("✅ Script completado.")

except Exception as e:
    print()
    print(f"❌ ERROR: {e}")
    print()
    print("Posibles causas:")
    print("  1. Falta instalar: pip install google-auth-oauthlib")
    print("  2. El puerto 8080 está ocupado")
    print("  3. No se abrió el navegador — prueba:")
    print("     python3 -c \"import webbrowser; webbrowser.open('http://localhost:8080')\"")
    print()
    print("Si falla, dime exactamente el error y lo arreglamos.")

finally:
    secret_path.unlink(missing_ok=True)
