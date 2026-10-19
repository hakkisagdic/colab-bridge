"""
A shared record of the Colab runtimes this machine drives, so several projects can use one runtime without stepping on
each other, whether they share one bridge or each has its own tab and bridge on it.

Tabs opened from bridge links share one runtime, so its GPU, files and life are shared too. Every bridge and client on
this machine reads and writes one JSON file (~/.cache/colab-bridge/registry.json, or COLAB_BRIDGE_REGISTRY) under a
lock:

  claims   {"project", "host", "gpu", "bridge", "vram_gb", "note", "since", "until"}: who uses which runtime, with how much
           GPU memory, until when. Advisory: nothing stops a cell from using more, but a claim that does not fit next to
           the others is refused, and `who` shows them all.
  bridges  port -> {"host", "last_run", "last_project", "cells"}: the runtime each bridge's tab ran its last cell on, and
           the ids of the cells its notebook held then.
  hosts    host -> {"gpu", "last_run", "last_project", "last_bridge", "reminded"}: every runtime seen until it is released
           or forgotten. When a bridge's cell runs on another runtime than its last, the notebook's cells tell why: the
           same notebook means it changed runtime (Runtime > Change runtime type, or Colab gave it a new one), and Colab
           released the old one, which is forgotten there and then; a new notebook (the link opened in another tab) may
           have left the old runtime running in the first tab, which is noticed once, then. A runtime no bridge's tab is
           on is not reminded about, and is forgotten a day after its last cell, by when Colab has ended it.
"""

import contextlib
import fcntl
import json
import os
import time

REGISTRY_ENV = "COLAB_BRIDGE_REGISTRY"
KEEP_ENDED_CLAIMS_S = 6 * 3600  # ended claims still count as the runtime's last use when reminding
RUNTIME_LIFE_S = 24 * 3600  # Colab ends every runtime within a day (Pro+'s longest): one left that long is gone
KEEP_CELL_IDS = 500  # the notebook's cell ids kept per bridge, to tell its notebook from another


class ClaimError(RuntimeError):
    pass


def registry_path() -> str:
    return os.path.expanduser(os.environ.get(REGISTRY_ENV) or "~/.cache/colab-bridge/registry.json")


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    for key, empty in (("claims", []), ("bridges", {}), ("hosts", {})):
        if not isinstance(data.get(key), type(empty)):
            data[key] = empty
    return data


@contextlib.contextmanager
def locked(path: str = None, write: bool = True):
    """The registry's data, locked against every other bridge and client; written back when the block ends."""
    path = path or registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = _load(path)
            yield data
            if write:
                temporary = f"{path}.{os.getpid()}.tmp"
                with open(temporary, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=1)
                os.replace(temporary, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def snapshot(path: str = None) -> dict:
    with locked(path, write=False) as data:
        return data


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def minutes(seconds: float) -> str:
    seconds = max(seconds, 0)
    return f"{seconds / 60:.0f} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"


def active(claims: list, now: float) -> list:
    return [c for c in claims if c["until"] > now]


def prune(data: dict, now: float):
    data["claims"] = [c for c in data["claims"] if c["until"] > now - KEEP_ENDED_CLAIMS_S]


def describe(c: dict, now: float) -> str:
    memory = f"{c['vram_gb']:g} GB" if c.get("vram_gb") else "no GPU memory"
    note = f" ({c['note']})" if c.get("note") else ""
    return f"{c['project']} holds {memory} until {clock(c['until'])}{note}"


def claim(data: dict, project: str, host: str, bridge, now: float, seconds: float, vram_gb: float = None,
          note: str = "", gpu: str = None, total_gb: float = None, free_gb: float = None) -> dict:
    """Adds or renews project's claim on host. GPU memory must fit next to the other projects' claims and, for a new
    claim, in what the GPU has free right now; a renewal's own job already uses part of that. Raises ClaimError."""
    prune(data, now)
    here = [c for c in active(data["claims"], now) if c["host"] == host]
    mine = [c for c in here if c["project"] == project]
    others = [c for c in here if c["project"] != project]
    if vram_gb:
        if not total_gb:
            raise ClaimError(f"{host} has no GPU, so there is no GPU memory to claim. If the tab should be on a GPU "
                             "runtime, pick the GPU again in the tab (Runtime > Change runtime type).")
        room = total_gb - sum(c.get("vram_gb") or 0 for c in others)
        if not mine and free_gb is not None:
            room = min(room, free_gb)
        if vram_gb > room + 0.05:
            holders = "; ".join(describe(c, now) for c in others) or "no other project holds a claim"
            free = f", {free_gb:.1f} GB free right now" if free_gb is not None else ""
            raise ClaimError(f"{vram_gb:g} GB does not fit on {host}: its GPU has {total_gb:.1f} GB{free}, and "
                             f"{holders}. Wait for them, claim less, or agree with them.")
    data["claims"] = [c for c in data["claims"] if not (c["host"] == host and c["project"] == project)]
    entry = {"project": project, "host": host, "gpu": gpu, "bridge": str(bridge), "vram_gb": vram_gb or None,
             "note": note, "since": mine[0]["since"] if mine else now, "until": now + seconds}
    data["claims"].append(entry)
    return entry


def release(data: dict, project: str, host: str = None) -> list:
    """Ends project's claims (on host, or everywhere); returns them."""
    ended = [c for c in data["claims"] if c["project"] == project and (host is None or c["host"] == host)]
    data["claims"] = [c for c in data["claims"] if c not in ended]
    return ended


def expected_host(data: dict, bridge, now: float):
    """The runtime this bridge's claims are on (the latest claim's), or None: a cell sent through the bridge must run
    there."""
    mine = sorted((c for c in active(data["claims"], now) if c["bridge"] == str(bridge)), key=lambda c: c["since"])
    return mine[-1]["host"] if mine else None


def note_run(data: dict, bridge, project: str, runtime: dict, now: float, cells: list = None):
    """Records that a cell ran on runtime through bridge, in a notebook holding the cells with these ids (None when not
    known). When the earlier runtime was another one, the notebook tells what happened to it: if the bridge's notebook is
    the same (a cell id in common), the notebook changed runtime and Colab released the old one, which is forgotten. Else
    returns the earlier runtime when it has a GPU, which may still be running in another tab and using compute units;
    else None."""
    host = runtime["host"]
    record = data["bridges"].setdefault(str(bridge), {})
    earlier = record.get("host")
    moved = None
    if earlier and earlier != host:
        if cells and set(cells) & set(record.get("cells") or []):
            forget(data, earlier)
        elif data["hosts"].get(earlier, {}).get("gpu"):
            moved = {"host": earlier, "gpu": data["hosts"][earlier]["gpu"]}
    record.update(host=host, last_run=now, last_project=project)
    if cells is not None:
        record["cells"] = list(cells)[-KEEP_CELL_IDS:]
    seen = data["hosts"].setdefault(host, {})
    seen.update(gpu=runtime.get("gpu"), last_run=now, last_project=project, last_bridge=str(bridge))
    return moved


def idle_reminders(data: dict, now: float, idle_s: float) -> list:
    """Reminders for runtimes a bridge's tab is on that ran no cell for idle_s and that no project claims, at most one
    per runtime every idle_s (the registry records when); returns the messages. A runtime no bridge's tab is on any more
    is not reminded about (it was noticed when the tab moved, see note_run) and is forgotten a day after its last cell."""
    prune(data, now)
    messages = []
    for host, seen in list(data["hosts"].items()):
        if any(c["host"] == host for c in active(data["claims"], now)):
            continue
        if not any(b.get("host") == host for b in data["bridges"].values()):
            if now - (seen.get("last_run") or 0) >= RUNTIME_LIFE_S:
                forget(data, host)
            continue
        last = max([seen.get("last_run") or 0] + [c["until"] for c in data["claims"] if c["host"] == host])
        if now - last < idle_s or now - (seen.get("reminded") or 0) < idle_s:
            continue
        seen["reminded"] = now
        machine = f"{host} ({seen.get('gpu') or 'no GPU'})"
        port = min(p for p, b in data["bridges"].items() if b.get("host") == host)
        how = (f"If nothing runs there any more, release it: colab-bridge --port {port} release-runtime. If a "
               "background job still runs there, claim it (colab-bridge claim --for 2h NOTE). If it is already "
               f"gone: colab-bridge forget {host}.")
        messages.append(f"Colab runtime {machine} has run no cell for {minutes(now - last)} and nobody claims it. {how}")
    return messages


def forget(data: dict, host: str) -> bool:
    """Drops a runtime that is gone: its record, the claims on it and the bridges that pointed to it."""
    known = host in data["hosts"] or any(b.get("host") == host for b in data["bridges"].values())
    data["hosts"].pop(host, None)
    data["claims"] = [c for c in data["claims"] if c["host"] != host]
    for record in data["bridges"].values():
        if record.get("host") == host:
            record["host"] = None
    return known
