"""Lock de navegador por CUENTA, válido entre procesos (fcntl.flock).

El lock de ``YouTubeBrowser`` (``self._lock``) es un ``threading.Lock``: solo
serializa dentro de un mismo proceso. El backfill de marcado IA y el worker de
generación son **procesos distintos** y podrían abrir dos sesiones sobre el mismo
perfil de Chromium → corrupción del perfil. Este lock es de fichero (Linux) y
cubre ambos.

Uso:
    from pipeline.browser_lock import browser_account_lock
    with browser_account_lock(account):
        ...operación de navegador para esa cuenta...
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("autotube.browser_lock")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TOKENS_DIR = Path(os.getenv("YT_BROWSER_TOKENS_DIR") or (_PROJECT_ROOT / "tokens"))


def _lock_path(account: str) -> Path:
    safe = "".join(c for c in (account or "default") if c.isalnum() or c in "-_")
    return _TOKENS_DIR / f".browser_{safe}.lock"


@contextmanager
def browser_account_lock(account: str, timeout: float = 180.0, poll: float = 1.0):
    """Adquiere el lock exclusivo de la cuenta (cross-process).

    Args:
        account: cuenta Google del canal (clave del perfil de navegador).
        timeout: segundos máximos de espera antes de rendirse.
        poll: intervalo de reintento.

    Raises:
        TimeoutError: si no se consigue el lock en ``timeout`` segundos.
    """
    try:
        import fcntl
    except ImportError:  # plataformas sin fcntl: no-op (no romper)
        yield
        return

    path = _lock_path(account)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser_account_lock: no se pudo abrir %s (%s); sin lock", path, exc)
        yield
        return

    acquired = False
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"browser_account_lock timeout para cuenta '{account}'"
                    )
                time.sleep(poll)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        fh.close()
