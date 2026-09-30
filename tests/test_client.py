"""The client and CLI against the bridge's own request handler, with a fake Colab tab that runs cells in this process."""

import asyncio
import contextlib
import io
import json
import os
import socket
import socketserver
import threading
import time
from types import SimpleNamespace

import pytest

from colab_bridge import browser, cli, client, notebook, registry, server


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


def test_run_cell_reuses_a_titled_cell(bridge):
    assert client.run_cell("# probe\nprint(40 + 2)", bridge.port) == "42\n"
    assert client.run_cell("# probe\nprint('again')", bridge.port) == "again\n"
    assert len(bridge.cells) == 1 and bridge.runs == 2
    client.run_cell("# another\nprint(1)", bridge.port)
    assert len(bridge.cells) == 2


def test_projects_keep_their_own_cells(bridge):
    client.run_cell("# probe\nprint('a')", bridge.port, project="alpha")
    assert client.run_cell("# probe\nprint('b')", bridge.port, project="beta") == "b\n"
    assert [c["source"].splitlines()[0] for c in bridge.cells] == ["# probe [alpha]", "# probe [beta]"]
    assert client.run_cell("print('untitled')", bridge.port, project="alpha") == "untitled\n"
    assert bridge.cells[-1]["source"].splitlines()[0] == "# colab-bridge cell [alpha]"


def test_a_run_records_the_runtime(bridge):
    result = client.run("# probe\nprint(1)", bridge.port, project="alpha")
    assert result["runtime"]["host"] == socket.gethostname()
    data = registry.snapshot()
    assert data["bridges"][str(bridge.port)]["host"] == socket.gethostname()
    assert data["hosts"][socket.gethostname()]["last_project"] == "alpha"


def test_a_cell_meant_for_another_runtime_is_not_run(bridge, tmp_path):
    with registry.locked() as data:
        registry.claim(data, "alpha", "gpu-host", bridge.port, time.time(), 600, note="training")
    marker = tmp_path / "ran"
    with pytest.raises(client.BridgeError, match="Not run: .* meant for gpu-host"):
        client.run_cell(f"# job\nopen({str(marker)!r}, 'w').write('x')", bridge.port, project="beta")
    assert not marker.exists()


def test_moving_off_a_gpu_runtime_is_reported(bridge):
    with registry.locked() as data:
        registry.note_run(data, bridge.port, "alpha", {"host": "gpu-host", "gpu": "Big GPU"}, time.time())
    client.run_cell("# probe\nprint(1)", bridge.port, project="alpha")
    assert len(bridge.notices) == 1 and "from runtime gpu-host (Big GPU)" in bridge.notices[0]


def test_one_cell_runs_at_a_time(bridge):
    bridge.tab.delay = 0.2
    threads = [threading.Thread(target=client.run_cell, args=(f"# cell {i}\nprint({i})", bridge.port))
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    # Each run's tool calls come together: get_cells, add_code_cell, run_code_cell.
    assert bridge.calls == ["get_cells", "add_code_cell", "run_code_cell"] * 3


def test_an_older_bridge_runs_cells_from_the_client(old_bridge, tmp_path):
    assert client.run_cell("# probe\nprint(40 + 2)", old_bridge.port, project="alpha") == "42\n"
    assert old_bridge.cells[0]["source"].splitlines()[0] == "# probe [alpha]"
    assert registry.snapshot()["bridges"][str(old_bridge.port)]["host"] == socket.gethostname()
    with registry.locked() as data:
        registry.claim(data, "alpha", "gpu-host", old_bridge.port, time.time(), 600)
    with pytest.raises(client.BridgeError, match="Not run"):
        client.run_cell("# probe\nprint(1)", old_bridge.port, project="alpha")


def test_env_is_set_for_one_cell_only(bridge):
    code = "# env probe\nimport os\nprint(os.environ['COLAB_BRIDGE_TEST'])"
    assert "COLAB_BRIDGE_TEST" not in os.environ
    assert client.run_cell(client.with_env(code, {"COLAB_BRIDGE_TEST": "--scene room"}), bridge.port) == "--scene room\n"
    assert "COLAB_BRIDGE_TEST" not in os.environ
    assert bridge.cells[0]["source"].splitlines()[0] == "# env probe", "the title stays the first line"


def test_fetch_copies_a_file_in_checked_parts(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(client, "FETCH_PART_BYTES", 1000)
    remote = tmp_path / "remote.bin"
    remote.write_bytes(os.urandom(2500))
    local = tmp_path / "local.bin"
    seen = []
    assert client.fetch(str(remote), str(local), bridge.port, lambda done, total: seen.append((done, total))) == 2500
    assert local.read_bytes() == remote.read_bytes()
    assert seen == [(1000, 2500), (2000, 2500), (2500, 2500)]

    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert client.fetch(str(empty), str(tmp_path / "copy.bin"), bridge.port) == 0


def test_fetch_of_a_missing_file_leaves_nothing(bridge, tmp_path):
    local = tmp_path / "local.bin"
    with pytest.raises(client.BridgeError, match="FileNotFoundError"):
        client.fetch(str(tmp_path / "missing.bin"), str(local), bridge.port)
    assert not local.exists() and not (tmp_path / "local.bin.part").exists()


def test_output_text():
    outputs = [{"output_type": "stream", "text": ["\x1b[31mred\x1b[0m ", "line\n"]},
               {"output_type": "error", "ename": "ValueError", "evalue": "bad"},
               {"output_type": "execute_result", "data": {"text/plain": ["42"]}}]
    assert client.output_text(outputs) == "red line\nValueError: bad42"


def test_split_runtime():
    text = f"before\n{notebook.RUNTIME_MARKER}{{\"host\": \"h\", \"gpu\": null}}\nafter\n"
    assert notebook.split_runtime(text) == ("before\nafter\n", {"host": "h", "gpu": None})
    assert notebook.split_runtime("plain\n") == ("plain\n", None)


def test_no_bridge_says_how_to_start_one():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(client.BridgeError, match="colab-bridge start"):
        client.status(port)


def test_link_parts(tmp_path):
    assert server.link_parts(str(tmp_path)) == (None, None)
    (tmp_path / "link.txt").write_text("https://colab.research.google.com/notebooks/empty.ipynb"
                                       "#mcpProxyToken=abc-DEF_123&mcpProxyPort=51234\n")
    assert server.link_parts(str(tmp_path)) == ("abc-DEF_123", 51234)


def test_cli(bridge, tmp_path, capsys):
    cell = tmp_path / "cell.py"
    cell.write_text("# cli probe\nimport os\nprint('hello', os.environ.get('WHO'))\n")
    assert cli.main(["--port", str(bridge.port), "run", str(cell), "--env", "WHO=colab"]) == 0
    assert "hello colab" in capsys.readouterr().out
    assert cli.main(["--port", str(bridge.port), "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"connected": True, "error": None}
    assert cli.main(["--port", str(bridge.port), "tools"]) == 0
    assert capsys.readouterr().out.split() == ["get_cells", "run_code_cell"]
    assert cli.main(["--port", str(bridge.port), "--dir", str(tmp_path), "link"]) == 1  # no link yet
    assert cli.main(["--port", str(bridge.port), "run", str(cell), "--env", "broken"]) == 2


def test_cli_claims(bridge, capsys):
    port = ["--port", str(bridge.port)]
    assert cli.main(port + ["--project", "alpha", "claim", "--for", "30m", "nightly", "render"]) == 0
    assert f"alpha claims {socket.gethostname()} (no GPU) until" in capsys.readouterr().out
    assert cli.main(port + ["--project", "beta", "claim", "--vram", "10"]) == 1  # this fake runtime has no GPU
    assert "has no GPU" in capsys.readouterr().err
    assert cli.main(port + ["who"]) == 0
    out = capsys.readouterr().out
    assert f"tabs:  bridge {bridge.port}" in out and "claim: alpha holds no GPU memory until" in out
    assert "(nightly render)" in out
    assert cli.main(port + ["--project", "alpha", "release"]) == 0
    assert "Ended alpha's claims" in capsys.readouterr().out
    assert registry.active(registry.snapshot()["claims"], time.time()) == []


def test_cli_release_runtime(bridge, capsys, monkeypatch):
    port = ["--port", str(bridge.port)]
    assert cli.main(port + ["--project", "alpha", "release-runtime"]) == 1  # no cell has run yet
    assert cli.main(port + ["--project", "beta", "claim", "training"]) == 0
    capsys.readouterr()
    assert cli.main(port + ["--project", "alpha", "release-runtime"]) == 1
    assert "Not released" in capsys.readouterr().err and registry.snapshot()["claims"]
    # google.colab is not here; a cell that prints what the real one prints before unassigning.
    monkeypatch.setattr(cli, "RELEASE_CELL", "# colab-bridge: release the runtime\nprint('releasing the runtime')\n")
    assert cli.main(port + ["--project", "alpha", "release-runtime", "--force"]) == 0
    assert f"Released {socket.gethostname()}" in capsys.readouterr().out
    data = registry.snapshot()
    assert not data["claims"] and not data["hosts"] and data["bridges"][str(bridge.port)]["host"] is None


def test_duration_seconds():
    assert cli.duration_seconds("90m") == 5400 and cli.duration_seconds("2h") == 7200
    assert cli.duration_seconds("1h30m") == 5400 and cli.duration_seconds("45") == 2700
    with pytest.raises(Exception):
        cli.duration_seconds("soon")


def test_fetch_warns_when_the_copy_crawls(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(client, "FETCH_PART_BYTES", 2000)
    monkeypatch.setattr(client, "SLOW_PART_BYTES", 1000)
    remote = tmp_path / "remote.bin"
    remote.write_bytes(os.urandom(5000))
    bridge.tab.delay = 0.3  # each part takes 0.3 s: about 7 KB/s, as in a hidden Chrome tab
    warnings = []
    assert client.fetch(str(remote), str(tmp_path / "local.bin"), bridge.port, warn=warnings.append) == 5000
    assert len(warnings) == 1 and "KB/s" in warnings[0] and "colab-bridge open" in warnings[0], warnings


def test_open_uses_a_chrome_of_its_own(tmp_path, monkeypatch, capsys):
    record = tmp_path / "chrome-args.txt"
    chrome = tmp_path / "chrome"
    chrome.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {record}\n")
    chrome.chmod(0o755)
    base = ["--port", "8799", "--dir", str(tmp_path)]
    assert cli.main(base + ["open", "--chrome", str(chrome)]) == 1, "no link yet"
    link = "https://colab.research.google.com/notebooks/empty.ipynb#mcpProxyToken=t&mcpProxyPort=1"
    (tmp_path / "link.txt").write_text(link + "\n")
    assert cli.main(base + ["open", "--chrome", str(chrome), "--profile", str(tmp_path / "profile")]) == 0
    deadline = time.time() + 10
    while time.time() < deadline and not (record.exists() and link in record.read_text()):
        time.sleep(0.05)
    args = record.read_text().splitlines()
    assert args[0] == f"--user-data-dir={tmp_path / 'profile'}" and args[-2:] == ["--new-window", link], args
    assert all(flag in args for flag in browser.UNTHROTTLED), args
    monkeypatch.setenv("COLAB_BRIDGE_CHROME", "/opt/chrome")
    assert browser.find_chrome() == "/opt/chrome" and browser.find_chrome("/x/chrome") == "/x/chrome"
