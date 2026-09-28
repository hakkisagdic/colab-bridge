"""
Client for a running colab-bridge. Standard library only, so any script can drive a Colab notebook through it.

The bridge listens on 127.0.0.1 and takes one JSON line per connection:
  {"op": "status"}                                  -> {"ok": true, "result": {"connected": bool, "error": str|null}}
  {"op": "list"}                                    -> the notebook tools the Colab tab offers
  {"op": "call", "name": TOOL, "args": {...}}       -> the tool's MCP result
  {"op": "run", "code": CODE, "project": NAME}      -> {"output": str, "runtime": {"host", "gpu"}}: the code as a cell,
                                                       one request at a time (colab_bridge.notebook.run_flow)
Errors come back as {"ok": false, "error": "..."}.
"""

import base64
import hashlib
import json
import os
import socket
import sys

from colab_bridge import notebook
from colab_bridge.notebook import output_text  # noqa: F401 (part of this module's interface)

DEFAULT_PORT = 8765
FETCH_PART_BYTES = 4 << 20  # base64 makes a part about 5.6 MB of cell output


class BridgeError(RuntimeError):
    pass


def default_port() -> int:
    return int(os.environ.get("COLAB_BRIDGE_PORT") or DEFAULT_PORT)


def request(req: dict, port: int = None, timeout: float = 900) -> object:
    """Sends one request to the bridge on 127.0.0.1:port and returns its result."""
    port = port or default_port()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall((json.dumps(req) + "\n").encode())
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buffer += chunk
    except OSError as e:
        raise BridgeError(f"No colab-bridge answers on 127.0.0.1:{port} ({e}); start one with "
                          "`colab-bridge start`.") from e
    try:
        out = json.loads(buffer)
    except ValueError as e:
        raise BridgeError(f"The bridge on port {port} sent an unreadable answer.") from e
    if not out.get("ok"):
        raise BridgeError(f"Bridge error: {out.get('error')}")
    return out["result"]


def status(port: int = None) -> dict:
    return request({"op": "status"}, port, timeout=30)


def tool_value(name: str, result: dict):
    """A notebook tool's answer from its MCP result: the structured content, or the text content read as JSON."""
    if result.get("isError"):
        raise BridgeError(f"{name} failed: " + json.dumps(result.get("content"), ensure_ascii=False)[:2000])
    return result.get("structuredContent") or json.loads("".join(c.get("text", "") for c in result["content"]))


def call_tool(name: str, args: dict, port: int = None):
    """Calls one of the Colab tab's notebook tools (get_cells, add_code_cell, update_cell, run_code_cell, ...)."""
    return tool_value(name, request({"op": "call", "name": name, "args": args}, port))


def run(code: str, port: int = None, project: str = None, expect_host: str = None, timeout: float = 900) -> dict:
    """Runs code as a notebook cell and returns {"output": what it printed, "runtime": {"host", "gpu"}}. The bridge runs
    one cell at a time, titles it with the project's name, and refuses to run it on another runtime than expect_host
    (by default the one this bridge's claims are on). A cell whose first line is the same (a title comment such as
    `# my probe`) is updated and run again instead of adding another."""
    port = port or default_port()
    req = {"op": "run", "code": code, "project": project}
    if expect_host:
        req["expect_host"] = expect_host
    try:
        return request(req, port, timeout)
    except BridgeError as e:
        if "unknown op run" not in str(e):
            raise
    # A bridge from before the run request: the same steps from here, without its one-at-a-time lock.
    result = notebook.drive(notebook.run_flow(code, project, port, expect_host),
                            lambda name, args: call_tool(name, args, port))
    if result["moved"]:
        print(f"colab-bridge: {result['moved']}", file=sys.stderr)
    if result["elsewhere"]:
        raise BridgeError(result["elsewhere"])
    return {"output": result["output"], "runtime": result["runtime"]}


def run_cell(code: str, port: int = None, project: str = None, expect_host: str = None, timeout: float = 900) -> str:
    """What code printed when run as a notebook cell (see run)."""
    return run(code, port, project, expect_host, timeout)["output"]


def with_env(code: str, env: dict, filename: str = "cell.py") -> str:
    """The code wrapped to run with these environment variables set for this cell only; they are restored afterwards,
    so a later cell does not inherit them. The first line stays the cell's title."""
    title = code.splitlines()[0] if code else "# colab-bridge"
    return (f"{title}\n"
            "import os as _colab_bridge_os\n"
            f"_colab_bridge_saved = {{name: _colab_bridge_os.environ.get(name) for name in {list(env)!r}}}\n"
            f"_colab_bridge_os.environ.update({dict(env)!r})\n"
            "try:\n"
            f"    exec(compile({code!r}, {filename!r}, 'exec'))\n"
            "finally:\n"
            "    for _colab_bridge_name, _colab_bridge_value in _colab_bridge_saved.items():\n"
            "        if _colab_bridge_value is None:\n"
            "            _colab_bridge_os.environ.pop(_colab_bridge_name, None)\n"
            "        else:\n"
            "            _colab_bridge_os.environ[_colab_bridge_name] = _colab_bridge_value\n")


FETCH_CELL = """# colab-bridge: read part of a runtime file
import base64, hashlib, os
with open({path!r}, "rb") as f:
    f.seek({offset})
    data = f.read({size})
print(os.path.getsize({path!r}), hashlib.sha256(data).hexdigest(), base64.b64encode(data).decode())
"""


def fetch(remote: str, local: str, port: int = None, progress=None, project: str = None) -> int:
    """Copies a file from the Colab runtime to local in parts read by notebook cells, each checked with SHA-256; the
    local file appears only when every part arrived. Returns its size. progress(done, total) is called per part."""
    part = local + ".part"
    offset, total = 0, None
    try:
        with open(part, "wb") as f:
            while total is None or offset < total:
                output = run_cell(FETCH_CELL.format(path=remote, offset=offset, size=FETCH_PART_BYTES), port,
                                  project)
                # "size sha256 base64" on the last line; an empty part leaves the base64 field empty.
                lines = output.strip("\n").splitlines()
                fields = lines[-1].split(" ") if lines else []
                if len(fields) != 3 or not fields[0].isdigit():
                    raise BridgeError(f"Reading {remote} on the runtime failed: {' '.join(fields)[:500]}")
                total, data = int(fields[0]), base64.b64decode(fields[2])
                if hashlib.sha256(data).hexdigest() != fields[1]:
                    raise BridgeError(f"A part of {remote} arrived damaged; fetch it again.")
                if not data and offset < total:
                    raise BridgeError(f"{remote} changed while it was copied; fetch it again.")
                f.write(data)
                offset += len(data)
                if progress:
                    progress(offset, total)
        os.replace(part, local)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return total
