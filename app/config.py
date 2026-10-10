"""Runtime settings, all from environment variables (set via .env / docker-compose)."""
import os
from pathlib import Path

VERSION = "1.1.1"


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
CLIENT_DIR = BASE_DIR / "client"

DATA_DIR = Path(os.environ.get("ND_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "netdevices.db"
SSH_KEY_DIR = Path(os.environ.get("ND_SSH_KEY_DIR", "/ssh"))

STATUS_INTERVAL = _int("ND_STATUS_INTERVAL", 30, 5, 3600)      # seconds between sweeps
STATUS_TIMEOUT = _int("ND_STATUS_TIMEOUT", 2, 1, 30)           # seconds per check
STATUS_CONCURRENCY = _int("ND_STATUS_CONCURRENCY", 64, 1, 512)

SESSION_DAYS = _int("ND_SESSION_DAYS", 30, 1, 365)
COOKIE_SECURE = _bool("ND_COOKIE_SECURE", False)               # set when served over HTTPS
SSH_CONNECT_TIMEOUT = _int("ND_SSH_TIMEOUT", 10, 2, 120)
