"""Single-user login: one password, server-side sessions, login throttling,
and a one-time setup code so nobody else on the LAN can claim a fresh install."""
import base64
import hashlib
import hmac
import secrets
import time

from . import config, db

COOKIE = "nd_session"
_failures: dict[str, list[float]] = {}
setup_code: str | None = None


def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def _verify(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        return hmac.compare_digest(_hash(password, base64.b64decode(salt)).split("$")[2], h)
    except Exception:
        return False


def is_configured() -> bool:
    return db.get_meta("password") is not None


def init_setup_code():
    """Called at startup. Prints a code to the container log that first-run setup must quote."""
    global setup_code
    if is_configured():
        setup_code = None
        return
    setup_code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
    print(f"\n*** NetDevices first-run setup code: {setup_code} ***\n", flush=True)


def check_password_rules(pw: str):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if len(pw) > 256:
        return "Password is too long."
    return None


def set_password(pw: str):
    global setup_code
    db.set_meta("password", _hash(pw))
    setup_code = None
    with db._lock:
        db._conn.execute("DELETE FROM sessions")   # log out everywhere


def throttled(ip: str) -> float:
    now = time.time()
    f = [t for t in _failures.get(ip, []) if now - t < 900]
    _failures[ip] = f
    if len(f) >= 5:
        wait = min(300, 2 ** (len(f) - 4))
        remaining = f[-1] + wait - now
        return max(0.0, remaining)
    return 0.0


def login(ip: str, pw: str) -> str | None:
    stored = db.get_meta("password")
    if stored and _verify(pw, stored):
        _failures.pop(ip, None)
        return new_session()
    _failures.setdefault(ip, []).append(time.time())
    return None


def verify_current(pw: str) -> bool:
    stored = db.get_meta("password")
    return bool(stored and _verify(pw, stored))


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db._lock:
        db._conn.execute("INSERT INTO sessions(token_hash,created,expires) VALUES(?,?,?)",
                         (hashlib.sha256(token.encode()).hexdigest(), now, now + config.SESSION_DAYS * 86400))
        db._conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    return token


def valid(token: str | None) -> bool:
    if not token:
        return False
    r = db.q("SELECT expires FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    return bool(r) and r[0]["expires"] > time.time()


def end_session(token: str | None):
    if token:
        with db._lock:
            db._conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))


def cookie_args() -> dict:
    return dict(key=COOKIE, httponly=True, samesite="strict", secure=config.COOKIE_SECURE,
                max_age=config.SESSION_DAYS * 86400, path="/")
