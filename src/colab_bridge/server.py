"""
The bridge: connects one Google Colab browser tab to this machine through googlecolab/colab-mcp and serves the tab's
notebook tools (get_cells, add_code_cell, update_cell, run_code_cell, ...) on a control socket on 127.0.0.1, where any
local script can call them (colab_bridge.client).

Why not colab-mcp as an MCP server of an agent: that server lives and dies with the agent's session, and each start
makes a new token, so the Colab tab has to be linked again; its tools are listed only once a tab connects, which agents
that list tools at start never see; and only the agent itself can call them. The bridge outlives sessions, keeps its
link (token and WebSocket port) across restarts, and lets scripts and background jobs drive the notebook without an
agent in the loop.

Several projects can share a bridge, and several bridges a runtime (colab_bridge.registry): the bridge runs one cell at
a time, stops cells that would land on another runtime than their claims, tells the person at this machine when Colab
moves the tab away from a GPU runtime, and reminds them of runtimes that sit idle with nobody claiming them.

The Colab link carries a token: whoever holds it can run code in that runtime. The control socket listens on 127.0.0.1
only and has no password, so every program of this machine's users can use a running bridge.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time

from colab_bridge import notebook, registry
from colab_bridge.client import tool_value

LINK_FILE = "link.txt"
PID_FILE = "bridge.pid"
LOG_FILE = "bridge.log"
MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # notebook tool answers carry whole cells; websockets' 1 MiB default is too small
IDLE_MINUTES = 30  # remind about a runtime that ran no cell and had no claim for this long; 0 turns reminders off
IDLE_CHECK_S = 60


def free_dual_stack_port() -> int:
    """A port free on both 127.0.0.1 and ::1. colab-mcp binds port 0 per address family, which can give IPv4 and IPv6
    different ports while the Colab link names only one of them."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as v4:
            v4.bind(("127.0.0.1", 0))
            port = v4.getsockname()[1]
            try:
                with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as v6:
                    v6.bind(("::1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no port is free on both 127.0.0.1 and ::1")


def notify(message: str, command: str = None):
    """Tells the person at this machine: runs command with the message in COLAB_BRIDGE_MESSAGE, or shows a desktop
    notification on macOS; the bridge log always has it."""
    logging.warning("notice: %s", message)
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if command:
            subprocess.Popen(command, shell=True, env=dict(os.environ, COLAB_BRIDGE_MESSAGE=message),
                             start_new_session=True, **quiet)
        elif sys.platform == "darwin" and shutil.which("osascript"):
            subprocess.Popen(["osascript", "-e", "on run argv", "-e",
                              'display notification (item 1 of argv) with title "colab-bridge"', "-e", "end run",
                              message], **quiet)
    except OSError:
        logging.exception("the notification failed")


async def answer(req: dict, state: dict, lock: asyncio.Lock, alert, reader=None):
    """The result of one control request. state holds the tab's MCP client ("client", None until the tab connects),
    its connection error and the bridge's port; lock lets one notebook request run at a time; alert(message) tells
    the person at this machine."""
    tab = state["client"]
    if req["op"] == "status":
        return {"connected": tab is not None, "error": state["error"]}
    if tab is None:
        raise RuntimeError("the Colab tab is not connected yet: open the link from `colab-bridge link`")
    if req["op"] == "list":
        return [{"name": t.name, "description": t.description, "input_schema": t.inputSchema}
                for t in await tab.list_tools()]
    if req["op"] == "call":
        async with lock:
            return (await tab.call_tool_mcp(req["name"], req.get("args") or {})).model_dump(mode="json")
    if req["op"] == "run":
        async def call(name, args):
            return tool_value(name, (await tab.call_tool_mcp(name, args)).model_dump(mode="json"))

        async with lock:
            if reader is not None and reader.at_eof():
                raise RuntimeError("the client stopped waiting before its cell's turn")
            result = await notebook.drive_async(
                notebook.run_flow(req["code"], req.get("project"), state["port"], req.get("expect_host")), call)
        if result["moved"]:
            alert(result["moved"])
        if result["elsewhere"]:
            raise RuntimeError(result["elsewhere"])
        return {"output": result["output"], "runtime": result["runtime"]}
    raise ValueError(f"unknown op {req['op']}")


def control_handler(state: dict, alert):
    """The control socket's connection handler: one JSON request line in, one JSON answer line out."""
    lock = asyncio.Lock()

    async def handle(reader, writer):
        try:
            req = json.loads(await reader.readline())
            out = {"ok": True, "result": await answer(req, state, lock, alert, reader)}
        except Exception as e:
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        writer.close()

    return handle


async def remind_idle(idle_minutes: float, alert):
    """Every minute, reminds about runtimes that sat idle with nobody claiming them (registry.idle_reminders). Bridges
    sharing a registry take turns through it, so each reminder comes once."""
    while True:
        await asyncio.sleep(IDLE_CHECK_S)
        try:
            with registry.locked() as data:
                messages = registry.idle_reminders(data, time.time(), idle_minutes * 60)
            for message in messages:
                alert(message)
        except Exception:
            logging.exception("the idle check failed")


async def serve(port: int, state_dir: str, token: str = None, ws_port: int = None,
                idle_minutes: float = IDLE_MINUTES, notify_command: str = None):
    """Runs the bridge until it is stopped: writes the Colab link to <state_dir>/link.txt, waits for the tab and
    answers control requests on 127.0.0.1:port. token and ws_port reuse an earlier link. idle_minutes (0 for never)
    and notify_command set the idle reminders (see notify)."""
    import websockets
    from fastmcp import Client
    from websockets.typing import Subprotocol
    from colab_mcp.session import ColabTransport
    from colab_mcp.websocket_server import COLAB, SCRATCH_PATH, ColabWebSocketServer

    class BridgeServer(ColabWebSocketServer):
        def __init__(self):
            super().__init__()
            self.token = token or self.token

        def _validate_authorization(self, websocket, request):
            response = super()._validate_authorization(websocket, request)
            if response is not None:
                sent = [t[:4] + "..." for t in re.findall(r"access_token=([^&]+)", request.path)]
                logging.info("rejected Colab handshake: tokens %s, expected %s..., origin %s",
                             sent, self.token[:4], request.headers.get("Origin"))
            return response

        async def __aenter__(self):
            self.port = ws_port or free_dual_stack_port()
            self._server = await websockets.serve(
                self._connection_handler, host="localhost", port=self.port,
                subprotocols=[Subprotocol("mcp")], origins=self.allowed_origins,
                process_request=self._validate_authorization, max_size=MAX_MESSAGE_BYTES)
            return self

    state = {"client": None, "error": None, "port": port}

    async def connect(wss):
        try:
            state["client"] = await Client(ColabTransport(wss)).__aenter__()
            logging.info("Colab tab connected")
        except Exception as e:
            state["error"] = f"{type(e).__name__}: {e}"
            logging.error("Colab connection failed: %s", state["error"])

    def alert(message):
        notify(message, notify_command)

    os.makedirs(state_dir, exist_ok=True)
    logging.basicConfig(filename=os.path.join(state_dir, LOG_FILE), level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with open(os.path.join(state_dir, PID_FILE), "w") as f:
        f.write(str(os.getpid()))
    async with BridgeServer() as wss:
        link = f"{COLAB}{SCRATCH_PATH}#mcpProxyToken={wss.token}&mcpProxyPort={wss.port}"
        with open(os.path.join(state_dir, LINK_FILE), "w") as f:
            f.write(link + "\n")
        print(f"bridge ready on 127.0.0.1:{port}\n{link}", flush=True)
        tasks = [asyncio.create_task(connect(wss))]  # held, so the loop does not drop them
        if idle_minutes:
            tasks.append(asyncio.create_task(remind_idle(idle_minutes, alert)))
        server = await asyncio.start_server(control_handler(state, alert), "127.0.0.1", port, limit=MAX_MESSAGE_BYTES)
        async with server:
            await server.serve_forever()


def link_parts(state_dir: str) -> tuple:
    """(token, WebSocket port) of the link an earlier run wrote to state_dir, or (None, None)."""
    try:
        with open(os.path.join(state_dir, LINK_FILE)) as f:
            link = f.read().strip()
    except OSError:
        return None, None
    token = re.search(r"mcpProxyToken=([^&\s]+)", link)
    port = re.search(r"mcpProxyPort=(\d+)", link)
    return (token.group(1) if token else None), (int(port.group(1)) if port else None)
