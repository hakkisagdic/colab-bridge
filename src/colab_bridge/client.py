"""
Client for a running colab-bridge. Standard library only, so any script can drive a Colab notebook through it.

The bridge listens on 127.0.0.1 and takes one JSON line per connection:
  {"op": "status"}                                  -> {"ok": true, "result": {"connected": bool, "error": str|null}}
  {"op": "list"}                                    -> the notebook tools the Colab tab offers
  {"op": "call", "name": TOOL, "args": {...}}       -> the tool's MCP result
Errors come back as {"ok": false, "error": "..."}.
"""

import base64
import hashlib
import json
import os
import re
import socket

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


def call_tool(name: str, args: dict, port: int = None):
    """Calls one of the Colab tab's notebook tools (get_cells, add_code_cell, update_cell, run_code_cell, ...)."""
    result = request({"op": "call", "name": name, "args": args}, port)
    if result.get("isError"):
        raise BridgeError(f"{name} failed: " + json.dumps(result.get("content"), ensure_ascii=False)[:2000])
    return result.get("structuredContent") or json.loads("".join(c.get("text", "") for c in result["content"]))


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


def run_cell(code: str, port: int = None) -> str:
    """Runs code as a notebook cell and returns what it printed. A cell whose first line is the same is updated and run
    again instead of adding another, so a script that starts with a title comment does not pile up cells."""
    cells = call_tool("get_cells", {"cellIndexStart": 0, "cellIndexEnd": 1000, "includeOutputs": False},
                      port).get("cells", [])
    title = code.splitlines()[0] if code else ""
    existing = next((c for c in cells if "".join(c.get("source") or []).splitlines()[:1] == [title]), None)
    if existing:
        cell_id = existing["id"]
        call_tool("update_cell", {"cellId": cell_id, "content": code}, port)
    else:
        cell_id = call_tool("add_code_cell", {"cellIndex": len(cells), "language": "python", "code": code},
                            port)["newCellId"]
    result = call_tool("run_code_cell", {"cellId": cell_id}, port)
    return output_text(result.get("outputs") if isinstance(result, dict) else [])


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


def fetch(remote: str, local: str, port: int = None, progress=None) -> int:
    """Copies a file from the Colab runtime to local in parts read by notebook cells, each checked with SHA-256; the
    local file appears only when every part arrived. Returns its size. progress(done, total) is called per part."""
    part = local + ".part"
    offset, total = 0, None
    try:
        with open(part, "wb") as f:
            while total is None or offset < total:
                output = run_cell(FETCH_CELL.format(path=remote, offset=offset, size=FETCH_PART_BYTES), port)
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
