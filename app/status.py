"""Background reachability checks: ICMP ping plus TCP connects to the SSH and web ports,
TLS certificate expiry for https web UIs, and state-change history for uptime figures.

Ping uses unprivileged ICMP "ping sockets" (docker-compose sets net.ipv4.ping_group_range
so the non-root container user may use them). If they're unavailable, ping is reported as
"n/a" and the TCP checks still run."""
import asyncio
import os
import socket
import ssl
import struct
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import config, db

results: dict[str, dict] = {}       # device id -> status
_wake = asyncio.Event()
ping_available: bool | None = None
last_sweep: float | None = None
_cert_cache: dict[str, tuple[float, dict]] = {}   # "host:port" -> (checked, info)
CERT_RECHECK = 3600
CERT_WARN_DAYS = 30
_last_state: dict[str, str] | None = None
_summary_cache: tuple[float, dict] = (0.0, {})


_logged: set[str] = set()


def _log_once(msg: str):
    """Unexpected check errors go to the container log once each, so they're findable."""
    if msg not in _logged and len(_logged) < 100:
        _logged.add(msg)
        print("status:", msg, flush=True)


def web_url(dev: dict) -> str:
    url = (dev.get("web_url") or "").strip().replace("{ip}", dev["ip"]).replace("{IP}", dev["ip"])
    if url and "://" not in url:
        url = "https://" + url
    return url


def web_target(dev: dict) -> tuple[str, int, bool] | None:
    """(host, port, is_https) of the configured web UI, or None when there isn't one."""
    url = web_url(dev)
    if not url:
        return None
    try:
        u = urlsplit(url)
        https = u.scheme == "https"
        return (u.hostname or dev["ip"]), (u.port or (443 if https else 80)), https
    except ValueError:
        return None


def ssh_target(dev: dict) -> tuple[str, int]:
    host = (dev.get("ssh_host") or dev["ip"]).strip().strip("[]")
    return host, int(dev.get("ssh_port") or 22)


async def _resolve(host: str):
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), config.STATUS_TIMEOUT)
    for fam in (socket.AF_INET, socket.AF_INET6):          # prefer IPv4
        for i in infos:
            if i[0] == fam:
                return fam, i[4][0]
    raise OSError("no address")


def _checksum(b: bytes) -> int:
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f"!{len(b) // 2}H", b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


async def ping(host: str) -> float | None:
    """Return round-trip ms, None if no reply. Raises PermissionError if ping sockets are blocked.

    Uses only add_reader()/plain sendto(), which every event loop supports. (uvloop, which uvicorn
    uses in the container, doesn't implement loop.sock_sendto().)"""
    fam, addr = await _resolve(host)
    proto, req, rep = ((socket.IPPROTO_ICMP, 8, 0) if fam == socket.AF_INET else (socket.IPPROTO_ICMPV6, 128, 129))
    sock = socket.socket(fam, socket.SOCK_DGRAM, proto)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    fd = sock.fileno()
    seq = int.from_bytes(os.urandom(2), "big")
    payload = b"netdevices" + os.urandom(6)
    hdr = struct.pack("!BBHHH", req, 0, 0, 0, seq)
    pkt = struct.pack("!BBHHH", req, 0, _checksum(hdr + payload), 0, seq) + payload
    got = loop.create_future()
    start = 0.0

    def readable():
        while not got.done():
            try:
                data = sock.recv(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:          # e.g. ICMP error queued on the socket
                got.set_exception(e)
                return
            if len(data) >= 8 and data[0] == rep and struct.unpack("!H", data[6:8])[0] == seq:
                got.set_result(round((time.perf_counter() - start) * 1000, 1))

    loop.add_reader(fd, readable)
    try:
        start = time.perf_counter()
        sock.sendto(pkt, (addr, 0))
        try:
            return await asyncio.wait_for(got, config.STATUS_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        except OSError:
            return None
    finally:
        loop.remove_reader(fd)
        sock.close()


async def tcp(host: str, port: int) -> float | None:
    start = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), port), config.STATUS_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return None
    w.close()
    try:
        await asyncio.wait_for(w.wait_closed(), 1)
    except Exception:
        pass
    return round((time.perf_counter() - start) * 1000, 1)


def _cert_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE           # homelab certs are often self-signed; we only want the dates
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")  # old NAS/router firmware
    except (ValueError, ssl.SSLError):
        pass
    return ctx


async def cert_info(host: str, port: int) -> dict:
    """Days until the TLS certificate expires. Cached for an hour per host:port."""
    key = f"{host}:{port}"
    hit = _cert_cache.get(key)
    if hit and time.time() - hit[0] < CERT_RECHECK:
        return hit[1]
    info: dict
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), port, ssl=_cert_ctx()),
                                      config.STATUS_TIMEOUT + 2)
        der = w.get_extra_info("ssl_object").getpeercert(binary_form=True)
        w.close()
        from cryptography import x509
        cert = x509.load_der_x509_certificate(der)
        exp = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=timezone.utc)
        days = int((exp - datetime.now(timezone.utc)).total_seconds() // 86400)
        cn = ""
        try:
            cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
        except Exception:
            pass
        info = {"days": days, "expires": exp.strftime("%Y-%m-%d"), "cn": str(cn)[:80],
                "self_signed": cert.issuer == cert.subject}
    except Exception as e:
        info = {"error": (str(e) or type(e).__name__)[:80]}
    _cert_cache[key] = (time.time(), info)
    return info


async def check(dev: dict) -> dict:
    global ping_available
    checks = {}
    jobs = {}
    if dev["chk_ping"]:
        jobs["ping"] = ping(dev["ip"])
    if dev["chk_ssh"] and dev.get("ssh_enabled", 1):
        h, p = ssh_target(dev)
        jobs["ssh"] = tcp(h, p)
    web = web_target(dev) if dev["chk_web"] else None
    if web:
        jobs["web"] = tcp(web[0], web[1])
    if jobs:
        out = await asyncio.gather(*jobs.values(), return_exceptions=True)
        for name, r in zip(jobs, out):
            if isinstance(r, PermissionError):
                ping_available = False
                checks[name] = {"ok": None, "note": "ping not permitted in container"}
            elif isinstance(r, BaseException):
                note = "lookup failed" if isinstance(r, (socket.gaierror, asyncio.TimeoutError)) else (str(r) or type(r).__name__)[:80]
                checks[name] = {"ok": False, "note": note}
                _log_once(f"{name} check error ({type(r).__name__}): {r}")
            else:
                if name == "ping":
                    ping_available = True
                checks[name] = {"ok": r is not None, "ms": r}
    cert = None
    if web and web[2] and checks.get("web", {}).get("ok"):
        cert = await cert_info(web[0], web[1])
    counted = [c["ok"] for c in checks.values() if c["ok"] is not None]
    if not counted:
        state = "off" if not jobs else "unknown"
    elif all(counted):
        state = "up"
    elif any(counted):
        state = "partial"
    else:
        state = "down"
    r = {"state": state, "checks": checks, "checked": time.time()}
    if cert:
        r["cert"] = cert
    return r


async def sweep():
    global last_sweep, _last_state
    devices = db.all_devices()
    sem = asyncio.Semaphore(config.STATUS_CONCURRENCY)
    if _last_state is None:
        _last_state = db.last_states()

    async def one(d):
        async with sem:
            try:
                r = await check(d)
            except Exception as e:     # never let one device kill the sweep
                r = {"state": "unknown", "checks": {}, "checked": time.time(), "error": str(e)[:120]}
            prev = results.get(d["id"])
            r["since"] = prev["since"] if prev and prev["state"] == r["state"] else r["checked"]
            results[d["id"]] = r

    await asyncio.gather(*(one(d) for d in devices))
    ids = {d["id"] for d in devices}
    for k in list(results):
        if k not in ids:
            del results[k]
    now = time.time()
    changes = []
    for i in ids:
        st = results[i]["state"]
        if st != "unknown" and _last_state.get(i) != st:
            changes.append((i, results[i]["checked"], st))
            _last_state[i] = st
    db.add_events(changes)
    last_sweep = now
    db.set_meta("last_sweep", str(now))


def mark_gap():
    """Called at startup: if the app was stopped, record that the state during the gap is unknown."""
    global _last_state
    try:
        prev = float(db.get_meta("last_sweep") or 0)
    except ValueError:
        prev = 0
    if prev and time.time() - prev > config.STATUS_INTERVAL * 3:
        ids = [d["id"] for d in db.all_devices()]
        db.add_events([(i, prev + config.STATUS_INTERVAL, "gap") for i in ids])
    _last_state = {i: "gap" for i in db.last_states()}


# ------------------------------------------------------------------ history summary
HISTORY_BUCKETS = 48           # 24 h strip in 30-minute buckets
_RANK = {"down": 3, "partial": 2, "up": 1}


def _segments(events, start, end):
    """[(t0, t1, state)] clipped to [start, end]. State before the first event is unknown."""
    segs = []
    for n, (ts, st) in enumerate(events):
        t1 = events[n + 1][0] if n + 1 < len(events) else end
        a, b = max(ts, start), min(t1, end)
        if b > a:
            segs.append((a, b, st))
    return segs


def summaries(now: float | None = None) -> dict:
    """Per device: uptime % over 24 h and 7 days, a 24 h strip, last time it went down."""
    global _summary_cache
    now = now or time.time()
    if now - _summary_cache[0] < 30:
        return _summary_cache[1]
    week = now - 7 * 86400
    day = now - 86400
    out = {}
    for dev_id, evs in db.events_since(week).items():
        segs7 = _segments(evs, week, now)

        def uptime(segs):
            up = sum(b - a for a, b, s in segs if s in ("up", "partial"))
            known = sum(b - a for a, b, s in segs if s in ("up", "partial", "down"))
            return round(100 * up / known, 2) if known >= 60 else None

        segs24 = _segments(evs, day, now)
        strip = []
        width = 86400 / HISTORY_BUCKETS
        for k in range(HISTORY_BUCKETS):
            b0 = day + k * width
            b1 = b0 + width
            worst = ""
            for a, b, s in segs24:
                if a < b1 and b > b0 and _RANK.get(s, 0) > _RANK.get(worst, 0):
                    worst = s
            strip.append(worst[:1] or "-")          # u / p / d / -
        last_down = None
        for ts, st in reversed(evs):
            if st == "down":
                last_down = ts
                break
        out[dev_id] = {"up24": uptime(segs24), "up7d": uptime(segs7), "strip": "".join(strip), "last_down": last_down}
    _summary_cache = (now, out)
    return out


def trigger():
    _wake.set()


async def run_forever():
    last_prune = 0.0
    while True:
        if time.time() - last_prune > 86400:
            try:
                db.prune_events()
            except Exception as e:
                print("history prune failed:", e, flush=True)
            last_prune = time.time()
        try:
            await sweep()
        except Exception as e:
            print("status sweep failed:", e, flush=True)
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), config.STATUS_INTERVAL)
        except asyncio.TimeoutError:
            pass
