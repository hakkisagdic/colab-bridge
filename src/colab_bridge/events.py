"""
Events: short messages a process on the Colab runtime leaves for this machine. They come here without a cell of their
own.

The runtime cannot reach this machine, and the bridge never runs a cell just to look (a cell counts as use, and keeps
an idle runtime from timing out). So a process on the runtime appends a line to the runtime's outbox,
/tmp/colab-bridge/outbox.jsonl: a JSON object such as {"level": "alert", "text": "the learner died", "source":
"watch"}, or plain text. Every cell a colab-bridge client sends (run, fetch, jobs) begins with a line of code that
prints what this machine has not had of the outbox yet. The client takes those lines out of the cell's output and keeps
them in the bridge's folder, in events.jsonl, one JSON object a line: {"at", "host", "level", "text", "source", "t"}.
So a message arrives with the next cell through the bridge, whoever sends that cell.

Where this machine got to in each runtime's outbox (by the runtime's host name) is kept beside it, in
events-cursor.json, and only a client that has kept the lines moves it. A cell whose output never came back (its client
stopped waiting) leaves the lines for the next cell, and two cells that read the same lines keep them once. The outbox
is append-only; a runtime lives a day at most.
"""

import contextlib
import fcntl
import json
import os
import time

OUTBOX = "/tmp/colab-bridge/outbox.jsonl"
EVENTS_FILE = "events.jsonl"
CURSOR_FILE = "events-cursor.json"
LOCK_FILE = "events.lock"
MARKER = "@@colab-bridge-event@@ "
MAX_LINES = 200  # outbox lines one cell brings; the rest come with the next cell
MAX_BYTES = 256 << 10
MAX_LINE = 4000  # characters of an outbox line a cell brings
MAX_TEXT = 2000  # characters of a message kept
KEEP_HOSTS = 8  # runtimes whose place in the outbox is remembered
KEEP_BYTES = 2 << 20  # past this, events.jsonl keeps its newer half

# Runs at the start of a cell, in a namespace of its own (nothing is left in the notebook's): prints the outbox's lines
# from where this machine got to on this runtime, a marker line each, and never stops the cell.
DRAIN = r'''
import json, os, socket
host = None
try:
    host = socket.gethostname()
    at = offsets.get(host, 0)
    size = os.path.getsize(outbox) if os.path.exists(outbox) else 0
    restart = at > size  # the outbox was cut short: it is read again from its start
    if restart:
        at = 0
    if size > at:
        with open(outbox, "rb") as f:
            f.seek(at)
            data = f.read(max_bytes)
        lines = data.split(b"\n")[:-1]
        cut = not lines and len(data) == max_bytes  # one line longer than a cell brings: taken in parts
        for raw in ([data] if cut else lines)[:max_lines]:
            at += len(raw) + (0 if cut else 1)
            print(marker + json.dumps({"host": host, "end": at, "restart": restart,
                                       "line": raw.decode("utf-8", "replace")[:max_line]}), flush=True)
except Exception as e:
    print(marker + json.dumps({"host": host, "error": f"{type(e).__name__}: {e}"[:300]}), flush=True)
'''


def drain_line(offsets: dict) -> str:
    """The cell's first line of code: DRAIN, given where this machine got to in each runtime's outbox."""
    names = {"offsets": dict(offsets), "outbox": OUTBOX, "marker": MARKER, "max_lines": MAX_LINES,
             "max_bytes": MAX_BYTES, "max_line": MAX_LINE}
    return f"exec({DRAIN!r}, {names!r})\n"


def with_drain(code: str, offsets: dict) -> str:
    """The code with the drain first, after the title when it has one (a first line that is a comment): the cell keeps
    its title, and untitled code stays untitled."""
    first, _, rest = code.partition("\n")
    if first.lstrip().startswith("#"):
        return f"{first}\n{drain_line(offsets)}{rest}"
    return drain_line(offsets) + code


def split(output: str) -> tuple:
    """(the output without the drain's lines, the drained lines as dicts)."""
    kept, drained = [], []
    for line in output.splitlines(keepends=True):
        if line.startswith(MARKER):
            try:
                drained.append(json.loads(line[len(MARKER):]))
                continue
            except ValueError:
                pass
        kept.append(line)
    return "".join(kept), drained


@contextlib.contextmanager
def _locked(state_dir: str):
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, LOCK_FILE), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _cursor(state_dir: str) -> dict:
    """{"offsets": {host: where this machine got to}, "errors": {host: the last failure said}}."""
    try:
        with open(os.path.join(state_dir, CURSOR_FILE), encoding="utf-8") as f:
            data = json.load(f)
        offsets = {str(h): int(o) for h, o in data["offsets"].items()}
        errors = {str(h): str(e) for h, e in data.get("errors", {}).items()}
        return {"offsets": offsets, "errors": errors}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return {"offsets": {}, "errors": {}}


def offsets(state_dir: str) -> dict:
    """host -> where this machine got to in that runtime's outbox."""
    return _cursor(state_dir)["offsets"]


def parse(line: str) -> dict:
    """An outbox line as {"level", "text", "source", "t"}: a JSON object's fields ("text", or "message"), or plain
    text, which is information."""
    line = line.strip()
    try:
        data = json.loads(line)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"level": "info", "text": line[:MAX_TEXT], "source": None, "t": None}
    text = data.get("text", data.get("message"))
    t = data.get("t")
    return {"level": str(data.get("level") or "info")[:20],
            "text": (line if text is None else str(text))[:MAX_TEXT],
            "source": str(data["source"])[:80] if data.get("source") else None,
            "t": float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None}


def _newest(mapping: dict, keep: int) -> dict:
    return dict(list(mapping.items())[-keep:])


def record(state_dir: str, drained: list, now: float = None) -> list:
    """Keeps the drained lines this machine has not had yet in <state_dir>/events.jsonl, and moves the cursor past
    them; returns the new events. A drain that failed is one event (level "error") until it says something else."""
    if not drained:
        return []
    now = time.time() if now is None else now
    new = []
    with _locked(state_dir):
        cursor = _cursor(state_dir)
        places, errors = cursor["offsets"], cursor["errors"]
        restarted = set()
        for d in drained:
            host = str(d.get("host"))
            if "error" in d:
                text = f"colab-bridge could not read the runtime's outbox ({d['error']})"
                if errors.get(host) != text:
                    errors.pop(host, None)
                    errors[host] = text
                    new.append({"at": now, "host": d.get("host"), "level": "error", "text": text,
                                "source": "colab-bridge", "t": None})
                continue
            try:
                end = int(d["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if d.get("restart") and host not in restarted:
                places[host] = 0
                restarted.add(host)
            if end <= places.get(host, 0):
                continue  # kept already, from another cell that read the same lines
            places.pop(host, None)
            places[host] = end  # the newest last
            event = parse(str(d.get("line", "")))
            if event["text"]:
                new.append({"at": now, "host": host, **event})
        path = os.path.join(state_dir, EVENTS_FILE)
        if new:
            with open(path, "a", encoding="utf-8") as f:
                f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in new)
            if os.path.getsize(path) > KEEP_BYTES:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
                temporary = f"{path}.{os.getpid()}.tmp"
                with open(temporary, "w", encoding="utf-8") as f:
                    f.writelines(lines[len(lines) // 2:])
                os.replace(temporary, path)
        temporary = os.path.join(state_dir, f"{CURSOR_FILE}.{os.getpid()}.tmp")
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump({"offsets": _newest(places, KEEP_HOSTS), "errors": _newest(errors, KEEP_HOSTS)}, f)
        os.replace(temporary, os.path.join(state_dir, CURSOR_FILE))
    return new


def read(state_dir: str, since: float = None) -> list:
    """The events kept in <state_dir>/events.jsonl, the oldest first; since: those that came after that time."""
    try:
        with open(os.path.join(state_dir, EVENTS_FILE), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and (since is None or (event.get("at") or 0) >= since):
            out.append(event)
    return out
