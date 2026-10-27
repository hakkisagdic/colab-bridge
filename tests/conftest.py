"""The fakes the client tests share: a Colab tab whose cells run in this process, behind the bridge's own request
handler (or a bridge from before the run request). The runtime's paths (its outbox, its jobs) and the bridge's folder
are a test's own, so no test touches /tmp/colab-bridge or ~/.cache/colab-bridge."""

import asyncio
import contextlib
import io
import json
import socketserver
import threading
from types import SimpleNamespace

import pytest

from colab_bridge import events, jobs, registry, server


class FakeNotebook:
    """Notebook tools as the Colab tab answers them: cells hold source, run_code_cell executes it here."""

    def __init__(self):
        self.cells = []
        self.runs = 0
        self.calls = []

    def tool(self, name: str, args: dict):
        self.calls.append(name)
        if name == "get_cells":
            return {"cells": [{"id": c["id"], "source": c["source"].splitlines(True)} for c in self.cells]}
        if name == "add_code_cell":
            cell = {"id": f"cell-{len(self.cells)}", "source": args["code"]}
            self.cells.insert(args["cellIndex"], cell)
            return {"newCellId": cell["id"]}
        if name == "update_cell":
            next(c for c in self.cells if c["id"] == args["cellId"])["source"] = args["content"]
            return {"ok": True}
        if name == "run_code_cell":
            self.runs += 1
            code = next(c for c in self.cells if c["id"] == args["cellId"])["source"]
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output):
                    exec(compile(code, "<cell>", "exec"), {})
            except BaseException as e:  # a cell's exception, SystemExit too, comes back as an error output
                return {"outputs": [{"output_type": "stream", "text": [output.getvalue()]},
                                    {"output_type": "error", "ename": type(e).__name__, "evalue": str(e)}]}
            return {"outputs": [{"output_type": "stream", "text": [output.getvalue()]}]}
        raise ValueError(name)


class Dumped:
    def __init__(self, data):
        self.data = data

    def model_dump(self, mode=None):
        return self.data


class FakeTab:
    """The tab's MCP client as the bridge sees it. Running a cell takes `delay` seconds, so requests can overlap."""

    def __init__(self, fake: FakeNotebook, delay: float = 0.0):
        self.fake = fake
        self.delay = delay

    async def list_tools(self):
        return [SimpleNamespace(name=name, description="", inputSchema={}) for name in ("get_cells", "run_code_cell")]

    async def call_tool_mcp(self, name, args):
        if name == "run_code_cell" and self.delay:
            await asyncio.sleep(self.delay)
        try:
            return Dumped({"isError": False, "structuredContent": self.fake.tool(name, args), "content": []})
        except Exception as e:
            return Dumped({"isError": True, "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}]})


@pytest.fixture(autouse=True)
def own_registry(tmp_path, monkeypatch):
    monkeypatch.setenv(registry.REGISTRY_ENV, str(tmp_path / "registry.json"))


@pytest.fixture(autouse=True)
def own_paths(tmp_path, monkeypatch):
    """The runtime's outbox and jobs folder, and the bridge's folder, in the test's own folder."""
    monkeypatch.setattr(events, "OUTBOX", str(tmp_path / "runtime" / "outbox.jsonl"))
    monkeypatch.setattr(jobs, "JOBS_DIR", str(tmp_path / "runtime" / "jobs"))
    monkeypatch.setenv("COLAB_BRIDGE_DIR", str(tmp_path / "bridge"))


@pytest.fixture
def bridge():
    """The bridge's request handler (server.control_handler) on a local port, with a fake tab."""
    fake = FakeNotebook()
    fake.tab = FakeTab(fake)
    fake.notices = []
    state = {"client": fake.tab, "error": None, "port": None}
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        tcp = loop.run_until_complete(asyncio.start_server(server.control_handler(state, fake.notices.append),
                                                           "127.0.0.1", 0))
        state["port"] = tcp.sockets[0].getsockname()[1]
        ready.set()
        loop.run_forever()
        tcp.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(10)
    fake.port = state["port"]
    yield fake
    loop.call_soon_threadsafe(loop.stop)
    thread.join(10)


@pytest.fixture
def old_bridge():
    """A bridge from before the run request: status, list and call only."""
    fake = FakeNotebook()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            req = json.loads(self.rfile.readline())
            if req["op"] == "status":
                out = {"ok": True, "result": {"connected": True, "error": None}}
            elif req["op"] == "call":
                try:
                    result = fake.tool(req["name"], req.get("args") or {})
                    out = {"ok": True, "result": {"isError": False, "structuredContent": result, "content": []}}
                except Exception as e:
                    out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            else:
                out = {"ok": False, "error": f"ValueError: unknown op {req['op']}"}
            self.wfile.write((json.dumps(out) + "\n").encode())

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as tcp:
        threading.Thread(target=tcp.serve_forever, daemon=True).start()
        fake.port = tcp.server_address[1]
        yield fake
        tcp.shutdown()
