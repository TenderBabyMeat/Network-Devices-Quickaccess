"""Network scan: find what answers on a private address range and suggest how to add it.

Only private, CGNAT (e.g. Tailscale) and link-local ranges up to /22 (1024 addresses) are allowed,
so a typo can't turn into scanning someone else's network."""
import asyncio
import ipaddress
import socket
import time

from . import db, status

PORTS = {
    22: "SSH", 80: "HTTP", 443: "HTTPS", 445: "SMB", 631: "IPP printing", 1883: "MQTT",
    3389: "RDP", 5000: "Synology", 5001: "Synology HTTPS", 8006: "Proxmox VE", 8007: "Proxmox Backup",
    8080: "HTTP alt", 8096: "Jellyfin/Emby", 8123: "Home Assistant", 8443: "HTTPS alt",
    9000: "HTTP 9000", 9100: "Printer", 9443: "HTTPS 9443", 32400: "Plex",
}
PROBE_TIMEOUT = 1.0
CONCURRENCY = 256
MAX_HOSTS = 1024
CGNAT = ipaddress.ip_network("100.64.0.0/10")

job: dict | None = None
_task: asyncio.Task | None = None


def validate(cidr: str) -> ipaddress.IPv4Network:
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        raise ValueError("Enter a range like 192.168.1.0/24")
    if net.version != 4:
        raise ValueError("Only IPv4 ranges can be scanned")
    if not (net.is_private or net.subnet_of(CGNAT) or net.is_link_local) or net.is_loopback:
        raise ValueError("Only private home-network ranges (10.x, 172.16-31.x, 192.168.x, 100.64-127.x) can be scanned")
    if net.num_addresses > MAX_HOSTS:
        raise ValueError(f"That range is too big ({net.num_addresses} addresses). Use /22 or smaller, e.g. /24")
    return net


def suggest(ip: str, ports: list[int], name: str) -> dict:
    """Guess a device type, model hint and web address from what's open."""
    p = set(ports)
    dtype, model, web = "", "", ""
    if 8006 in p:
        dtype, model, web = "server", "Proxmox VE", "https://{ip}:8006"
    elif 8007 in p:
        dtype, model, web = "server", "Proxmox Backup Server", "https://{ip}:8007"
    elif 8123 in p:
        dtype, model, web = "iot", "Home Assistant", "http://{ip}:8123"
    elif 5001 in p or 5000 in p:
        dtype, model, web = "nas", "Synology DSM", "https://{ip}:5001" if 5001 in p else "http://{ip}:5000"
    elif 32400 in p:
        dtype, model, web = "media", "Plex", "http://{ip}:32400/web"
    elif 8096 in p:
        dtype, model, web = "media", "Jellyfin / Emby", "http://{ip}:8096"
    elif 9100 in p or 631 in p:
        dtype, model = "printer", "Printer"
    elif 3389 in p:
        dtype, model = "pc", "Windows (RDP)"
    elif 1883 in p:
        dtype = "iot"
    if not web:
        for port, url in ((443, "https://{ip}"), (80, "http://{ip}"), (8443, "https://{ip}:8443"),
                          (8080, "http://{ip}:8080"), (9443, "https://{ip}:9443"), (9000, "http://{ip}:9000")):
            if port in p:
                web = url
                break
    if not dtype and 22 in p:
        dtype = "server"
    short = name.split(".")[0] if name and not name.replace(".", "").isdigit() else ""
    return {"hostname": short or f"host-{ip.split('.')[-1]}", "dtype": dtype, "model": model,
            "web_url": web, "ssh_enabled": 22 in p}


async def _rdns(ip: str) -> str:
    loop = asyncio.get_running_loop()
    try:
        host, _ = await asyncio.wait_for(loop.getnameinfo((ip, 0), socket.NI_NAMEREQD), 1.5)
        return host
    except Exception:
        return ""


async def _tcp(ip: str, port: int, sem) -> bool:
    async with sem:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), PROBE_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return False
        w.close()
        return True


async def _ping(ip: str, sem):
    async with sem:
        try:
            return await status.ping(ip)
        except Exception:
            return None


async def _host(ip: str, sem, known: dict):
    probes = [_tcp(ip, p, sem) for p in PORTS]
    res = await asyncio.gather(_ping(ip, sem), *probes)
    ms, open_ports = res[0], [p for p, ok in zip(PORTS, res[1:]) if ok]
    if job:
        job["done"] += 1
    if ms is None and not open_ports:
        return
    name = await _rdns(ip)
    job["results"].append({"ip": ip, "name": name, "ping_ms": ms, "ports": open_ports,
                           "known": known.get(ip), "suggest": suggest(ip, open_ports, name)})


async def _run(net):
    known = {}
    for d in db.all_devices():
        for addr in (d["ip"], d.get("ssh_host") or ""):
            if addr:
                known[addr.strip()] = d["hostname"]
    sem = asyncio.Semaphore(CONCURRENCY)
    hosts = [str(h) for h in (net.hosts() if net.num_addresses > 2 else net)]
    job["total"] = len(hosts)
    try:
        await asyncio.gather(*(_host(h, sem, known) for h in hosts))
        job["state"] = "done"
    except asyncio.CancelledError:
        job["state"] = "cancelled"
    except Exception as e:
        job["state"] = "error"
        job["error"] = str(e)[:200]
    job["results"].sort(key=lambda r: tuple(int(x) for x in r["ip"].split(".")))
    job["finished"] = time.time()


def start(cidr: str) -> dict:
    global job, _task
    if job and job["state"] == "running":
        raise ValueError("A scan is already running")
    net = validate(cidr)
    job = {"cidr": str(net), "state": "running", "done": 0, "total": 0, "results": [],
           "started": time.time(), "finished": None}
    _task = asyncio.create_task(_run(net))
    return job


def cancel():
    if _task and not _task.done():
        _task.cancel()


def suggest_range() -> str:
    """The /24 that most saved devices live in, as a starting point."""
    counts: dict[str, int] = {}
    for d in db.all_devices():
        try:
            ip = ipaddress.ip_address(d["ip"].strip())
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local:
            n = str(ipaddress.ip_network(f"{ip}/24", strict=False))
            counts[n] = counts.get(n, 0) + 1
    return max(counts, key=counts.get) if counts else "192.168.1.0/24"
