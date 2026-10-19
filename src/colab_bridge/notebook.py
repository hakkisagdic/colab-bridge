"""
Running code as a notebook cell through the Colab tab's tools. The bridge uses it for its `run` request and the client
for bridges older than that request, so both name cells, check the runtime and read the output the same way.

Every cell starts with a short preamble that prints which runtime ran it (host name and GPU) on a marker line, which is
taken out of the output again. With an expected host, the preamble stops the cell before its own code when the tab now
runs on another runtime: Colab moves an idle tab to a new runtime without saying so.
"""

import json
import re
import time

from colab_bridge import registry

RUNTIME_MARKER = "@@colab-bridge-runtime@@ "
UNTITLED = "# colab-bridge cell"
MOVED = "colab-bridge: this tab runs on another runtime than expected; nothing was run"


def output_text(outputs) -> str:
    """What a cell printed: stream text, errors as "Name: value", and plain-text display data, without ANSI colours."""
    parts = []
    for output in outputs or []:
        if "text" in output:
            text = output["text"]
            parts.append("".join(text) if isinstance(text, list) else str(text))
        elif output.get("output_type") == "error":
            parts.append(f"{output.get('ename')}: {output.get('evalue')}")
        elif "data" in output:
            plain = output["data"].get("text/plain", "")
            parts.append("".join(plain) if isinstance(plain, list) else str(plain))
    return re.sub(r"\x1b\[[0-9;]*m", "", "".join(parts))


def title_and_body(code: str, project: str = None) -> tuple:
    """The cell's title line and the code after it. A first line that is a comment is the title; other code gets a
    shared one. The project's name goes at the end of the title, so projects sharing a notebook keep their own cells."""
    first, _, rest = code.partition("\n")
    if first.lstrip().startswith("#"):
        title, body = first.rstrip(), rest
    else:
        title, body = UNTITLED, code
    if project:
        title = f"{title} [{project}]"
    return title, body


def runtime_preamble(expected_host: str = None) -> str:
    """Code that prints the runtime on a marker line and, with an expected host, stops the cell on any other host. The
    host and GPU are looked up once per kernel."""
    guard = (f"if _colab_bridge_runtime['host'] != {expected_host!r}:\n    raise SystemExit({MOVED!r})\n"
             if expected_host else "")
    return (
        "import json as _colab_bridge_json\n"
        "if '_colab_bridge_runtime' not in globals():\n"
        "    import socket as _colab_bridge_socket, subprocess as _colab_bridge_subprocess\n"
        "    try:\n"
        "        _colab_bridge_gpu = _colab_bridge_subprocess.run(\n"
        "            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], capture_output=True, text=True,\n"
        "            timeout=30).stdout.strip().splitlines()[0] or None\n"
        "    except Exception:\n"
        "        _colab_bridge_gpu = None\n"
        "    _colab_bridge_runtime = {'host': _colab_bridge_socket.gethostname(), 'gpu': _colab_bridge_gpu}\n"
        f"print({RUNTIME_MARKER!r} + _colab_bridge_json.dumps(_colab_bridge_runtime), flush=True)\n"
        + guard)


def cell_source(code: str, project: str = None, expected_host: str = None) -> str:
    title, body = title_and_body(code, project)
    return f"{title}\n{runtime_preamble(expected_host)}{body}"


def split_runtime(output: str) -> tuple:
    """(the output without the runtime line, {"host", "gpu"} or None when the cell printed none)."""
    runtime, kept = None, []
    for line in output.splitlines(keepends=True):
        if runtime is None and line.startswith(RUNTIME_MARKER):
            try:
                runtime = json.loads(line[len(RUNTIME_MARKER):])
                continue
            except ValueError:
                pass
        kept.append(line)
    return "".join(kept), runtime


def run_steps(code: str, project: str = None, expected_host: str = None, cell_ids: list = None):
    """The tool calls that run code as a cell, as a generator: it yields (tool, arguments), is sent each tool's result
    and returns what the cell printed. A cell with the same title is updated and run again instead of adding one.
    cell_ids, when given, gets the ids of the notebook's cells (the one run among them), which tell one notebook from
    another (registry.note_run)."""
    source = cell_source(code, project, expected_host)
    title = source.partition("\n")[0]
    cells = (yield "get_cells", {"cellIndexStart": 0, "cellIndexEnd": 1000, "includeOutputs": False}).get("cells", [])
    existing = next((c for c in cells if "".join(c.get("source") or []).splitlines()[:1] == [title]), None)
    if existing:
        cell_id = existing["id"]
        yield "update_cell", {"cellId": cell_id, "content": source}
    else:
        cell_id = (yield "add_code_cell", {"cellIndex": len(cells), "language": "python", "code": source})["newCellId"]
    if cell_ids is not None:
        cell_ids.extend([c["id"] for c in cells if c.get("id")] + ([] if existing else [cell_id]))
    result = yield "run_code_cell", {"cellId": cell_id}
    return output_text(result.get("outputs") if isinstance(result, dict) else [])


def run_flow(code: str, project: str, bridge, expect_host: str = None):
    """run_steps for a bridge: the cell must run on expect_host, or else on the runtime the bridge's claims are on, and
    the runtime it ran on is recorded in the registry. Returns {"output", "runtime", "moved", "elsewhere"}: moved is the
    GPU runtime Colab moved the tab away from (see registry.note_run), elsewhere why the cell's code was not run."""
    expected = expect_host
    if expected is None:
        with registry.locked(write=False) as data:
            expected = registry.expected_host(data, bridge, time.time())
    cell_ids = []
    output, runtime = split_runtime((yield from run_steps(code, project, expected, cell_ids)))
    moved = elsewhere = None
    if runtime:
        with registry.locked() as data:
            left = registry.note_run(data, bridge, project, runtime, time.time(), cell_ids)
        if left:
            moved = (f"The link of bridge {bridge} now runs in another notebook, on runtime {runtime['host']} "
                     f"({runtime.get('gpu') or 'no GPU'}); its earlier runtime {left['host']} ({left['gpu']}) may still "
                     "be running in the first tab and using compute units: if so, end it under Runtime > Manage "
                     f"sessions. colab-bridge forgets {left['host']} by itself a day after its last cell.")
        if expected and runtime["host"] != expected:
            elsewhere = (f"Not run: the tab of bridge {bridge} now runs on {runtime['host']} "
                         f"({runtime.get('gpu') or 'no GPU'}), but the cell was meant for {expected}. Colab moves a tab "
                         "to a new runtime when it sits idle. Pick the runtime again in the tab (Runtime > Change "
                         f"runtime type); if {expected} is gone, run: colab-bridge forget {expected}.")
    return {"output": output, "runtime": runtime, "moved": moved, "elsewhere": elsewhere}


def drive(steps, call):
    """Runs a run_steps generator with call(tool, arguments) -> result."""
    try:
        request = next(steps)
        while True:
            request = steps.send(call(*request))
    except StopIteration as done:
        return done.value


async def drive_async(steps, call):
    """drive() for an async call."""
    try:
        request = next(steps)
        while True:
            request = steps.send(await call(*request))
    except StopIteration as done:
        return done.value
