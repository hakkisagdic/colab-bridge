"""The client and CLI against a fake bridge that speaks the control protocol and runs cells in this process."""

import contextlib
import io
import json
import os
import socket
import socketserver
import threading

import pytest

from colab_bridge import cli, client, server


class FakeNotebook:
    """Notebook tools as the Colab tab answers them: cells hold source, run_code_cell executes it here."""

    def __init__(self):
        self.cells = []
        self.runs = 0

    def tool(self, name: str, args: dict):
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
            except Exception as e:  # a cell's exception comes back as an error output, as in Colab
                return {"outputs": [{"output_type": "stream", "text": [output.getvalue()]},
                                    {"output_type": "error", "ename": type(e).__name__, "evalue": str(e)}]}
            return {"outputs": [{"output_type": "stream", "text": [output.getvalue()]}]}
        raise ValueError(name)


@pytest.fixture
def bridge():
    notebook = FakeNotebook()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            req = json.loads(self.rfile.readline())
            if req["op"] == "status":
                out = {"ok": True, "result": {"connected": True, "error": None}}
            elif req["op"] == "list":
                out = {"ok": True, "result": [{"name": name} for name in ("get_cells", "run_code_cell")]}
            else:
                try:
                    result = notebook.tool(req["name"], req.get("args") or {})
                    out = {"ok": True, "result": {"isError": False, "structuredContent": result, "content": []}}
                except Exception as e:
                    out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self.wfile.write((json.dumps(out) + "\n").encode())

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as tcp:
        threading.Thread(target=tcp.serve_forever, daemon=True).start()
        notebook.port = tcp.server_address[1]
        yield notebook
        tcp.shutdown()


def test_run_cell_reuses_a_titled_cell(bridge):
    assert client.run_cell("# probe\nprint(40 + 2)", bridge.port) == "42\n"
    assert client.run_cell("# probe\nprint('again')", bridge.port) == "again\n"
    assert len(bridge.cells) == 1 and bridge.runs == 2
    client.run_cell("# another\nprint(1)", bridge.port)
    assert len(bridge.cells) == 2


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
