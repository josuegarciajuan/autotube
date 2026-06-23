#!/usr/bin/env python3
"""Complete OAuth2 authentication with a manually-provided authorization code.

Usage:
    python3 scripts/complete_auth.py canal2 '4/0AanRRr...'
"""

import sys
import json
import pickle
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow
from config.settings import TOKENS_DIR

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def complete_auth(account_name: str, code: str) -> bool:
    """Complete OAuth flow with the authorization code the user copied from URL."""
    state_path = TOKENS_DIR / f"{account_name}_state.json"
    token_path = TOKENS_DIR / f"{account_name}.pickle"

    if not state_path.exists():
        logger.error("❌ No state file found for '%s'. Run authenticate() first.", account_name)
        logger.info("   Ej: python3 -c \"from pipeline.youtube_uploader import YouTubeUploader; YouTubeUploader(account_name='%s').authenticate()\"", account_name)
        return False

    state_data = json.loads(state_path.read_text())
    client_secret_path = state_data["client_secret_path"]
    scopes = state_data["scopes"]
    code_verifier = state_data.get("code_verifier")

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes)
    flow.redirect_uri = "http://localhost"

    # Set code_verifier for PKCE (required for installed app OAuth)
    if code_verifier:
        flow.code_verifier = code_verifier

    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        logger.error("❌ Code exchange failed: %s", exc)
        logger.info("   Asegúrate de haber copiado el código completo (puede tener / y -)")
        logger.info("   El código es lo que va entre 'code=' y '&scope=' en la URL de redirección")
        return False

    credentials = flow.credentials
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)

    with open(token_path, "wb") as f:
        pickle.dump(credentials, f)

    state_path.unlink()  # clean up

    logger.info("✅ Autenticación completada para '%s'", account_name)
    logger.info("   Token guardado en: %s", token_path)

    # Quick verification
    from googleapiclient.discovery import build
    youtube = build("youtube", "v3", credentials=credentials)
    resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if items:
        channel_title = items[0]["snippet"]["title"]
        logger.info("   Canal conectado: %s", channel_title)
    else:
        logger.warning("   ⚠️ No se encontró canal asociado a esta cuenta.")

    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python3 scripts/complete_auth.py <account_name> <code>")
        print('Ej:   python3 scripts/complete_auth.py canal2 "4/0AanRRr..."')
        sys.exit(1)

    account_name = sys.argv[1]
    code = sys.argv[2]

    ok = complete_auth(account_name, code)
    sys.exit(0 if ok else 1)
