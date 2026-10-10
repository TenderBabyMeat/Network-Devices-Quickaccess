"""Browser terminal <-> SSH bridge over a WebSocket.

Protocol (browser -> server, JSON text frames):
  {"type":"start","cols":N,"rows":N}      first message
  {"type":"input","data":"..."}           keystrokes
  {"type":"resize","cols":N,"rows":N}
  {"type":"answer","value":"..."}         reply to a prompt
Server -> browser: binary frames are terminal output; JSON text frames are
  {"type":"status","msg"}  {"type":"prompt","kind":"username|password|hostkey",...}
  {"type":"connected"}     {"type":"closed","reason"}

Security notes:
  * Host keys are trust-on-first-use, stored per host:port. The key is fetched and
    verified BEFORE any credentials are sent; a changed key needs explicit approval.
  * Passwords are only held in memory for the connection attempt, never stored.
  * Private keys come from the read-only /ssh mount.
"""
import asyncio
import json
import os

from starlette.websockets import WebSocket, WebSocketDisconnect

# The container runs as a bare UID with no /etc/passwd entry. asyncssh looks up the
# *local* username while building its options (even for the host-key probe, before the
# real login name is used) and fails if it can't find one. Give it a harmless default.
for _var in ("USER", "LOGNAME"):
    os.environ.setdefault(_var, "netdevices")
if not os.environ.get("HOME") or not os.access(os.environ["HOME"], os.W_OK):
    os.environ["HOME"] = "/tmp"

from . import config, db
from .status import ssh_target

try:
    import asyncssh
except ImportError:          # lets the rest of the app run (and be tested) without it
    asyncssh = None

MAX_PW_TRIES = 3


class Abort(Exception):
    pass


def _legacy_algs() -> dict:
    """Everything asyncssh supports (not just its modern defaults) for older NAS boxes, routers and embedded gear."""
    opts = {}
    getters = {
        "kex_algs": ("asyncssh.kex", "get_kex_algs"),
        "encryption_algs": ("asyncssh.encryption", "get_encryption_algs"),
        "mac_algs": ("asyncssh.mac", "get_mac_algs"),
        "server_host_key_algs": ("asyncssh.public_key", "get_public_key_algs"),
    }
    import importlib
    for opt, (mod, fn) in getters.items():
        try:
            algs = getattr(importlib.import_module(mod), fn)()
            algs = [a.decode() if isinstance(a, bytes) else a for a in algs]
            algs = [a for a in algs if a != "none"]
            if algs:
                opts[opt] = algs
        except Exception:
            pass
    return opts


def load_client_keys():
    keys, notes = [], []
    if asyncssh is None or not config.SSH_KEY_DIR.is_dir():
        return keys, notes
    for p in sorted(config.SSH_KEY_DIR.iterdir()):
        if not p.is_file() or p.suffix in (".pub", ".txt", ".md") or p.name.startswith(".") or p.name in ("known_hosts", "config", "authorized_keys"):
            continue
        try:
            keys.append(asyncssh.read_private_key(str(p)))
        except Exception as e:
            notes.append(f"{p.name}: {'passphrase-protected (not supported)' if 'passphrase' in str(e).lower() else 'not a usable private key'}")
    return keys, notes


def key_names() -> list[str]:
    try:
        return [p.name for p in sorted(config.SSH_KEY_DIR.iterdir())
                if p.is_file() and p.suffix != ".pub" and not p.name.startswith(".")]
    except OSError:
        return []


class Session:
    def __init__(self, ws: WebSocket, dev: dict):
        self.ws = ws
        self.dev = dev
        self.host, self.port = ssh_target(dev)
        self.target = f"{self.host}:{self.port}"
        self.cols, self.rows = 80, 24

    async def send(self, **msg):
        await self.ws.send_text(json.dumps(msg))

    async def status(self, msg):
        await self.send(type="status", msg=msg)

    async def ask(self, kind, **extra) -> str:
        await self.send(type="prompt", kind=kind, **extra)
        while True:
            m = await self._recv_json()
            if m.get("type") == "answer":
                return str(m.get("value", ""))
            if m.get("type") == "resize":
                self._size(m)

    async def _recv_json(self) -> dict:
        raw = await self.ws.receive_text()
        try:
            m = json.loads(raw)
            return m if isinstance(m, dict) else {}
        except ValueError:
            return {}

    def _size(self, m):
        try:
            self.cols = max(10, min(1000, int(m.get("cols", self.cols))))
            self.rows = max(4, min(500, int(m.get("rows", self.rows))))
        except (TypeError, ValueError):
            pass

    # -------------------------------------------------------------- steps
    async def verify_host_key(self, algs):
        await self.status(f"Connecting to {self.target} ...")
        hk_opts = {k: v for k, v in algs.items() if k in ("kex_algs", "server_host_key_algs")}
        try:
            key = await asyncio.wait_for(asyncssh.get_server_host_key(self.host, self.port, **hk_opts),
                                         config.SSH_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            raise Abort(f"Timed out connecting to {self.target}.")
        except OSError as e:
            raise Abort(f"Couldn't reach SSH on {self.target}: {e.strerror or e}.")
        except ValueError as e:
            raise Abort(f"SSH option problem: {e}")
        except asyncssh.Error as e:
            hint = "" if self.dev.get("legacy_ssh") else " If this is older gear, tick 'Allow legacy SSH algorithms' in the device editor."
            raise Abort(f"SSH negotiation with {self.target} failed: {e}.{hint}")
        if key is None:
            raise Abort(f"{self.target} didn't present a host key.")
        pub = key.export_public_key().decode().strip()
        fp = key.get_fingerprint("sha256")
        known = db.get_host_key(self.target)
        if known and known["key"] == pub:
            return key
        if known:
            ans = await self.ask("hostkey", changed=True, target=self.target, fingerprint=fp, old=known["fingerprint"],
                                 algorithm=key.get_algorithm())
        else:
            ans = await self.ask("hostkey", changed=False, target=self.target, fingerprint=fp,
                                 algorithm=key.get_algorithm())
        if ans != "yes":
            raise Abort("Host key not accepted. Nothing was sent to the device.")
        db.set_host_key(self.target, pub, fp)
        return key

    async def connect(self, key, algs):
        user = (self.dev.get("ssh_user") or "").strip()
        if not user:
            user = (await self.ask("username", target=self.target)).strip()
            if not user:
                raise Abort("No username given.")
        base = dict(host=self.host, port=self.port, username=user, known_hosts=([key], [], []),
                    agent_path=None, **algs)
        keys, notes = load_client_keys()
        for n in notes:
            await self.status(f"Skipping key {n}")
        if keys:
            await self.status(f"Trying SSH key{'s' if len(keys) > 1 else ''} for {user}@{self.target} ...")
            try:
                return await self._open(base, client_keys=keys, password=None, preferred_auth="publickey")
            except asyncssh.PermissionDenied:
                await self.status("Key not accepted, falling back to password.")
        for attempt in range(MAX_PW_TRIES):
            pw = await self.ask("password", user=user, target=self.target, retry=attempt > 0)
            if pw == "":
                raise Abort("Cancelled.")
            await self.status("Authenticating ...")
            try:
                return await self._open(base, client_keys=None, password=pw,
                                        preferred_auth="keyboard-interactive,password")
            except asyncssh.PermissionDenied:
                continue
            finally:
                pw = None
        raise Abort("Authentication failed.")

    async def _open(self, base, **auth):
        try:
            return await asyncio.wait_for(asyncssh.connect(**base, **auth), config.SSH_CONNECT_TIMEOUT + 20)
        except asyncio.TimeoutError:
            raise Abort("Timed out during login.")
        except asyncssh.PermissionDenied:
            raise
        except asyncssh.HostKeyNotVerifiable:
            raise Abort("Host key changed between check and login - refusing to continue.")
        except (OSError, asyncssh.Error, ValueError) as e:
            raise Abort(f"SSH error: {e}")

    async def run(self):
        start = await self._recv_json()
        if start.get("type") == "start":
            self._size(start)
        if asyncssh is None:
            raise Abort("Web SSH isn't available: the asyncssh package is not installed.")
        algs = _legacy_algs() if self.dev.get("legacy_ssh") else {}
        key = await self.verify_host_key(algs)
        conn = await self.connect(key, algs)
        async with conn:
            proc = await conn.create_process(term_type="xterm-256color", term_size=(self.cols, self.rows),
                                             encoding=None, stderr=asyncssh.STDOUT)
            await self.send(type="connected", target=self.target)

            async def downstream():
                while True:
                    data = await proc.stdout.read(65536)
                    if not data:
                        return "Session ended."
                    await self.ws.send_bytes(data)

            async def upstream():
                while True:
                    m = await self._recv_json()
                    t = m.get("type")
                    if t == "input" and isinstance(m.get("data"), str):
                        proc.stdin.write(m["data"].encode())
                    elif t == "resize":
                        self._size(m)
                        proc.change_terminal_size(self.cols, self.rows)

            tasks = [asyncio.create_task(downstream()), asyncio.create_task(upstream())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            reason = "Session ended."
            for t in done:
                if t.exception() is None:
                    reason = t.result()
                elif isinstance(t.exception(), WebSocketDisconnect):
                    reason = None
            proc.close()
            return reason


async def handle(ws: WebSocket, dev: dict):
    s = Session(ws, dev)
    try:
        reason = await s.run()
        if reason:
            await s.send(type="closed", reason=reason)
    except Abort as e:
        try:
            await s.send(type="closed", reason=str(e))
        except Exception:
            pass
    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await s.send(type="closed", reason=f"Unexpected error: {e}")
        except Exception:
            pass
    try:
        await ws.close()
    except Exception:
        pass
