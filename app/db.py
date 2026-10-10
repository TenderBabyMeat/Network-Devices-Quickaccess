"""SQLite storage. The front end edits the whole location/device list and saves it back
with the revision it started from, so two open tabs can't silently overwrite each other.

(Locations are stored in the `sites` table / `sites` key for compatibility with older versions.)"""
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sites (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, pos INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL, hostname TEXT NOT NULL, ip TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', web_url TEXT NOT NULL DEFAULT '',
  ssh_host TEXT NOT NULL DEFAULT '', ssh_user TEXT NOT NULL DEFAULT '', ssh_port INTEGER NOT NULL DEFAULT 22,
  putty_session TEXT NOT NULL DEFAULT '', legacy_ssh INTEGER NOT NULL DEFAULT 0,
  chk_ping INTEGER NOT NULL DEFAULT 1, chk_ssh INTEGER NOT NULL DEFAULT 1, chk_web INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS host_keys (
  target TEXT PRIMARY KEY, key TEXT NOT NULL, fingerprint TEXT NOT NULL, added REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, ts REAL NOT NULL, state TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_dev_ts ON events(device_id, ts);
"""

# Columns added after 1.0.x: (name, definition)
MIGRATIONS = [
    ("dtype", "TEXT NOT NULL DEFAULT ''"),
    ("tags", "TEXT NOT NULL DEFAULT ''"),
    ("links", "TEXT NOT NULL DEFAULT '[]'"),
    ("ssh_enabled", "INTEGER NOT NULL DEFAULT 0"),
]

DEVICE_FIELDS = ("hostname", "ip", "model", "notes", "web_url", "ssh_host", "ssh_user",
                 "ssh_port", "putty_session", "legacy_ssh", "chk_ping", "chk_ssh", "chk_web",
                 "dtype", "tags", "links", "ssh_enabled")
BOOL_FIELDS = ("legacy_ssh", "chk_ping", "chk_ssh", "chk_web", "ssh_enabled")

DEVICE_TYPES = {
    "server": "Server", "vm": "VM", "container": "Container / LXC", "pc": "Desktop / laptop",
    "pi": "Raspberry Pi / SBC", "nas": "NAS / storage", "router": "Router / firewall", "switch": "Switch",
    "ap": "Wi-Fi access point", "printer": "Printer", "iot": "Smart home / IoT", "media": "Media / TV",
    "phone": "Phone / tablet", "other": "Other",
}


class ValidationError(ValueError):
    pass


def init():
    global _conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)
    have = {r["name"] for r in _conn.execute("PRAGMA table_info(devices)")}
    added = []
    for name, ddl in MIGRATIONS:
        if name not in have:
            _conn.execute(f"ALTER TABLE devices ADD COLUMN {name} {ddl}")
            added.append(name)
    if "ssh_enabled" in added:
        # 1.0.x showed SSH buttons for everything. Keep them where SSH was clearly set up.
        _conn.execute("UPDATE devices SET ssh_enabled=1 WHERE ssh_user<>'' OR putty_session<>'' "
                      "OR ssh_host<>'' OR ssh_port<>22")
        if _conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]:
            print(f"database upgraded from 1.0.x: added {', '.join(added)}", flush=True)
    if get_meta("rev") is None:
        set_meta("rev", "0")


@contextmanager
def tx():
    with _lock:
        _conn.execute("BEGIN IMMEDIATE")
        try:
            yield _conn
            _conn.execute("COMMIT")
        except BaseException:
            _conn.execute("ROLLBACK")
            raise


def q(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchall()


def ex(sql, args=()):
    with _lock:
        return _conn.execute(sql, args)


def get_meta(key):
    r = q("SELECT value FROM meta WHERE key=?", (key,))
    return r[0]["value"] if r else None


def set_meta(key, value):
    with _lock:
        _conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))


# ------------------------------------------------------------------ helpers
def _json_list(s):
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def device_row_to_dict(d) -> dict:
    dev = {"id": d["id"]}
    for f in DEVICE_FIELDS:
        v = d[f]
        if f in BOOL_FIELDS:
            v = bool(v)
        elif f == "links":
            v = _json_list(v)
        elif f == "tags":
            v = [t for t in (v or "").split(",") if t]
        dev[f] = v
    return dev


# ------------------------------------------------------------------ location/device document
def load_all() -> dict:
    sites = [{"id": s["id"], "name": s["name"], "devices": []} for s in q("SELECT * FROM sites ORDER BY pos")]
    by_id = {s["id"]: s for s in sites}
    for d in q("SELECT * FROM devices ORDER BY pos"):
        s = by_id.get(d["site_id"])
        if s is not None:
            s["devices"].append(device_row_to_dict(d))
    return {"rev": int(get_meta("rev") or 0), "sites": sites}


def all_devices() -> list[dict]:
    out = []
    for r in q("SELECT * FROM devices"):
        d = dict(r)
        d["links"] = _json_list(d.get("links"))
        out.append(d)
    return out


def get_device(dev_id: str):
    r = q("SELECT * FROM devices WHERE id=?", (dev_id,))
    return dict(r[0]) if r else None


_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TAG = re.compile(r"^[\w .+&/-]{1,24}$")


def _s(v, name, maxlen, required=False):
    v = "" if v is None else str(v).strip()
    if required and not v:
        raise ValidationError(f"{name} is required")
    if len(v) > maxlen:
        raise ValidationError(f"{name} is too long")
    return v


def _url(v, what, hostname):
    """Only http(s) links (or scheme-less ones, which become https). Blocks javascript: and friends."""
    v = _s(v, what, 2000)
    if not v:
        return ""
    if "://" in v:
        scheme = v.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            raise ValidationError(f"{hostname}: {what} must start with http:// or https://")
    elif re.match(r"^[a-z][a-z0-9+.-]*:", v, re.I) and not re.match(r"^[\w.-]+:\d", v):
        raise ValidationError(f"{hostname}: {what} must be a web address")
    try:
        urlsplit(v.replace("{ip}", "x").replace("{IP}", "x"))
    except ValueError:
        raise ValidationError(f"{hostname}: {what} isn't a valid address")
    return v


def _new_id():
    return secrets.token_hex(6)


def normalize(doc: dict) -> list[dict]:
    """Validate an incoming document. Also accepts exports from older versions and the old standalone page."""
    if not isinstance(doc, dict) or not isinstance(doc.get("sites"), list):
        raise ValidationError("expected an object with a 'sites' list")
    seen = set()
    out = []
    for s in doc["sites"]:
        if not isinstance(s, dict):
            raise ValidationError("bad location entry")
        sid = str(s.get("id") or "")
        if not _ID.match(sid) or sid in seen:
            sid = _new_id()
        seen.add(sid)
        site = {"id": sid, "name": _s(s.get("name"), "Location name", 200) or "Unnamed location", "devices": []}
        for d in s.get("devices") or []:
            if not isinstance(d, dict):
                raise ValidationError("bad device entry")
            did = str(d.get("id") or "")
            if not _ID.match(did) or did in seen:
                did = _new_id()
            seen.add(did)
            old_ssh = d.get("ssh") if isinstance(d.get("ssh"), dict) else {}   # old standalone-page format
            hostname = _s(d.get("hostname"), "Name", 200, True)
            port = d.get("ssh_port", old_ssh.get("port", 22))
            try:
                port = int(port or 22)
            except (TypeError, ValueError):
                raise ValidationError(f"{hostname}: SSH port must be a number")
            if not 1 <= port <= 65535:
                raise ValidationError(f"{hostname}: SSH port must be 1-65535")
            putty = d.get("putty_session")
            if putty is None:
                putty = old_ssh.get("session", "") if old_ssh.get("mode") == "session" else ""
            ssh_host = _s(d.get("ssh_host", old_ssh.get("host")), "SSH host", 255)
            ssh_user = _s(d.get("ssh_user", old_ssh.get("user")), "SSH user", 100)
            putty = _s(putty, "PuTTY session", 200)
            if "ssh_enabled" in d:
                ssh_enabled = bool(d["ssh_enabled"])
            else:   # older exports: same rule as the database upgrade
                ssh_enabled = bool(ssh_user or putty or ssh_host or port != 22 or old_ssh)

            tags = d.get("tags") or []
            if isinstance(tags, str):
                tags = tags.split(",")
            clean_tags = []
            for t in tags:
                t = str(t).strip()
                if not t:
                    continue
                if not _TAG.match(t):
                    raise ValidationError(f"{hostname}: tag '{t[:30]}' - use letters, numbers, spaces and . + & / - (max 24)")
                if t.lower() not in [x.lower() for x in clean_tags]:
                    clean_tags.append(t)
            if len(clean_tags) > 12:
                raise ValidationError(f"{hostname}: at most 12 tags")

            links = []
            for l in d.get("links") or []:
                if not isinstance(l, dict):
                    continue
                url = _url(l.get("url"), "Link address", hostname)
                if not url:
                    continue
                label = _s(l.get("label"), "Link label", 30) or "Link"
                links.append({"label": label, "url": url})
            if len(links) > 8:
                raise ValidationError(f"{hostname}: at most 8 extra links")

            dtype = str(d.get("dtype") or "")
            if dtype and dtype not in DEVICE_TYPES:
                dtype = "other"

            dev = {
                "id": did,
                "hostname": hostname,
                "ip": _s(d.get("ip"), "IP address", 255, True),
                "model": _s(d.get("model"), "Model / OS", 200),
                "notes": _s(d.get("notes"), "Notes", 2000),
                "web_url": _url(d.get("web_url", d.get("webUrl")), "Web UI address", hostname),
                "ssh_host": ssh_host,
                "ssh_user": ssh_user,
                "ssh_port": port,
                "putty_session": putty,
                "legacy_ssh": bool(d.get("legacy_ssh", False)),
                "chk_ping": bool(d.get("chk_ping", True)),
                "chk_ssh": bool(d.get("chk_ssh", True)),
                "chk_web": bool(d.get("chk_web", True)),
                "dtype": dtype,
                "tags": clean_tags,
                "links": links,
                "ssh_enabled": ssh_enabled,
            }
            if dev["ssh_host"] and not re.match(r"^[A-Za-z0-9.\-:\[\]%]+$", dev["ssh_host"]):
                raise ValidationError(f"{hostname}: SSH host has invalid characters")
            if dev["ssh_user"] and not re.match(r"^[A-Za-z0-9._\-\\@]+$", dev["ssh_user"]):
                raise ValidationError(f"{hostname}: SSH user has invalid characters")
            if re.search(r'["\\]', dev["putty_session"]):
                raise ValidationError(f"{hostname}: PuTTY session name can't contain quotes or backslashes")
            site["devices"].append(dev)
        out.append(site)
    return out


def _db_value(f, v):
    if isinstance(v, bool):
        return int(v)
    if f == "tags":
        return ",".join(v)
    if f == "links":
        return json.dumps(v, separators=(",", ":"))
    return v


def save_all(sites: list[dict], expected_rev: int | None) -> int:
    with tx() as c:
        rev = int(c.execute("SELECT value FROM meta WHERE key='rev'").fetchone()[0])
        if expected_rev is not None and expected_rev != rev:
            raise RevisionConflict(rev)
        c.execute("DELETE FROM devices")
        c.execute("DELETE FROM sites")
        for i, s in enumerate(sites):
            c.execute("INSERT INTO sites(id,name,pos) VALUES(?,?,?)", (s["id"], s["name"], i))
            for j, d in enumerate(s["devices"]):
                c.execute(f"INSERT INTO devices(id,site_id,pos,{','.join(DEVICE_FIELDS)}) "
                          f"VALUES(?,?,?,{','.join('?' * len(DEVICE_FIELDS))})",
                          (d["id"], s["id"], j, *[_db_value(f, d[f]) for f in DEVICE_FIELDS]))
        rev += 1
        c.execute("UPDATE meta SET value=? WHERE key='rev'", (str(rev),))
        return rev


class RevisionConflict(Exception):
    def __init__(self, rev):
        super().__init__(f"data changed (now revision {rev})")
        self.rev = rev


# ------------------------------------------------------------------ host keys
def get_host_key(target: str):
    r = q("SELECT * FROM host_keys WHERE target=?", (target,))
    return dict(r[0]) if r else None


def set_host_key(target: str, key: str, fingerprint: str):
    with _lock:
        _conn.execute("INSERT INTO host_keys(target,key,fingerprint,added) VALUES(?,?,?,?) "
                      "ON CONFLICT(target) DO UPDATE SET key=excluded.key, fingerprint=excluded.fingerprint, added=excluded.added",
                      (target, key, fingerprint, time.time()))


def forget_host_key(target: str) -> bool:
    with _lock:
        return _conn.execute("DELETE FROM host_keys WHERE target=?", (target,)).rowcount > 0


# ------------------------------------------------------------------ status history
def add_events(rows: list[tuple[str, float, str]]):
    if rows:
        with _lock:
            _conn.executemany("INSERT INTO events(device_id,ts,state) VALUES(?,?,?)", rows)


def events_since(since: float) -> dict[str, list[tuple[float, str]]]:
    """Per device: the last event before `since` (the state at that moment) plus everything after."""
    out: dict[str, list[tuple[float, str]]] = {}
    for r in q("SELECT e.device_id, e.ts, e.state FROM events e JOIN "
               "(SELECT device_id, MAX(ts) AS ts FROM events WHERE ts < ? GROUP BY device_id) b "
               "ON e.device_id=b.device_id AND e.ts=b.ts", (since,)):
        out.setdefault(r["device_id"], []).append((r["ts"], r["state"]))
    for r in q("SELECT device_id, ts, state FROM events WHERE ts >= ? ORDER BY ts", (since,)):
        out.setdefault(r["device_id"], []).append((r["ts"], r["state"]))
    return out


def last_states() -> dict[str, str]:
    return {r["device_id"]: r["state"] for r in q(
        "SELECT e.device_id, e.state FROM events e JOIN (SELECT device_id, MAX(id) AS id FROM events GROUP BY device_id) m "
        "ON e.id=m.id")}


def prune_events(keep_days: int = 30):
    cutoff = time.time() - keep_days * 86400
    with _lock:
        # keep the newest pre-cutoff event per device so the state at the cutoff is still known
        _conn.execute("DELETE FROM events WHERE ts < ? AND id NOT IN "
                      "(SELECT MAX(id) FROM events WHERE ts < ? GROUP BY device_id)", (cutoff, cutoff))
        _conn.execute("DELETE FROM events WHERE device_id NOT IN (SELECT id FROM devices)")


def export_json() -> str:
    d = load_all()
    return json.dumps({"app": "netdevices", "version": 3, "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "title": get_meta("title") or "", "sites": d["sites"]}, indent=2)
