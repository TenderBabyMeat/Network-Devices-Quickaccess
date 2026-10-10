"""Automatic daily JSON backups in <data>/backups, newest 14 kept."""
import asyncio
import re
import time
from pathlib import Path

from . import config, db

KEEP = 14
NAME = re.compile(r"^netdevices-\d{4}-\d{2}-\d{2}(-manual-\d{6})?\.json$")


def folder() -> Path:
    p = config.DATA_DIR / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def make(manual: bool = False) -> str:
    stamp = time.strftime("%Y-%m-%d")
    name = f"netdevices-{stamp}-manual-{time.strftime('%H%M%S')}.json" if manual else f"netdevices-{stamp}.json"
    path = folder() / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(db.export_json(), encoding="utf-8")
    tmp.replace(path)
    prune()
    return name


def prune():
    files = sorted((p for p in folder().iterdir() if NAME.match(p.name)), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[KEEP:]:
        p.unlink(missing_ok=True)


def listing() -> list[dict]:
    return [{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
            for p in sorted(folder().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True) if NAME.match(p.name)]


def path_of(name: str) -> Path | None:
    if not NAME.match(name):
        return None
    p = folder() / name
    return p if p.is_file() else None


async def run_forever():
    await asyncio.sleep(60)          # let the app settle after a restart
    while True:
        try:
            if not (folder() / f"netdevices-{time.strftime('%Y-%m-%d')}.json").exists():
                name = make()
                print("backup written:", name, flush=True)
        except Exception as e:
            print("backup failed:", e, flush=True)
        await asyncio.sleep(3600)
