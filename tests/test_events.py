"""The runtime's events (colab_bridge.events): lines a process there leaves in its outbox, brought home by the cells
the client sends anyway, each kept once."""

import json
import os
import socket

from colab_bridge import cli, client, events

HOST = socket.gethostname()  # the fake tab's cells run in this process, so the "runtime" is this machine


def post(*lines):
    os.makedirs(os.path.dirname(events.OUTBOX), exist_ok=True)
    with open(events.OUTBOX, "a", encoding="utf-8") as f:
        f.writelines(line + "\n" for line in lines)


def kept():
    return [(e["level"], e["text"]) for e in events.read(client.default_dir())]


def test_cells_bring_the_outbox_home_once(bridge):
    assert client.run_cell("# probe\nprint(40 + 2)", bridge.port) == "42\n"  # no outbox yet: nothing to bring
    assert kept() == []
    post(json.dumps({"level": "alert", "text": "the trainer died (exit 1)", "source": "watch", "t": 5.0}),
         "plain words are information")
    result = client.run("# probe\nprint(40 + 2)", bridge.port)
    assert result["output"] == "42\n", "the drain's lines are taken out of the output"
    assert [(e["level"], e["text"], e["source"], e["t"], e["host"]) for e in result["events"]] == [
        ("alert", "the trainer died (exit 1)", "watch", 5.0, HOST),
        ("info", "plain words are information", None, None, HOST)]
    client.run_cell("# another\nprint(1)", bridge.port)
    assert len(kept()) == 2, "lines already kept do not come again"
    post('{"level": "info", "message": "the nightly copy saved 12 files"}')
    client.run_cell("print('untitled')", bridge.port)
    assert kept()[-1] == ("info", "the nightly copy saved 12 files")
    assert [c["source"].splitlines()[0] for c in bridge.cells] == ["# probe", "# another", "# colab-bridge cell"], \
        "the drain keeps each cell's title, and untitled code untitled"


def test_an_older_bridge_brings_them_too(old_bridge):
    post("from the outbox")
    assert client.run_cell("# probe\nprint(1)", old_bridge.port) == "1\n"
    assert kept() == [("info", "from the outbox")]


def test_a_cell_whose_answer_was_lost_leaves_the_lines_for_the_next(bridge):
    post("first", "second")
    # The cell ran and drained, but its output never came back here (its client stopped waiting): nothing is kept,
    # and the cursor does not move.
    code = events.with_drain("# probe\nprint(1)", events.offsets(client.default_dir()))
    bridge.tool("add_code_cell", {"cellIndex": 0, "language": "python", "code": code})
    bridge.tool("run_code_cell", {"cellId": "cell-0"})
    assert kept() == []
    client.run_cell("# probe\nprint(1)", bridge.port)
    assert kept() == [("info", "first"), ("info", "second")]


def test_two_cells_that_read_the_same_lines_keep_them_once(tmp_path):
    drained = [{"host": "h", "end": 6, "line": "first"}, {"host": "h", "end": 13, "line": "second"}]
    assert len(events.record(str(tmp_path), drained)) == 2
    assert events.record(str(tmp_path), drained) == []
    assert [e["text"] for e in events.record(str(tmp_path), drained + [{"host": "h", "end": 19, "line": "third"}])] \
        == ["third"]
    assert events.offsets(str(tmp_path)) == {"h": 19}


def test_a_line_still_being_written_waits(bridge):
    post("whole")
    with open(events.OUTBOX, "a") as f:
        f.write('{"text": "half')
    client.run_cell("# probe\nprint(1)", bridge.port)
    assert kept() == [("info", "whole")]
    with open(events.OUTBOX, "a") as f:
        f.write(' done"}\n')
    client.run_cell("# probe\nprint(1)", bridge.port)
    assert kept() == [("info", "whole"), ("info", "half done")]


def test_many_lines_come_over_several_cells(bridge, monkeypatch):
    monkeypatch.setattr(events, "MAX_LINES", 3)
    post(*[f"line {i}" for i in range(7)])
    counts = [len(client.run("# probe\nprint(1)", bridge.port)["events"]) for _ in range(4)]
    assert counts == [3, 3, 1, 0]
    assert [text for _, text in kept()] == [f"line {i}" for i in range(7)]


def test_a_line_longer_than_a_cell_brings_is_taken_in_parts(bridge, monkeypatch):
    monkeypatch.setattr(events, "MAX_BYTES", 16)
    post("x" * 40, "after")
    for _ in range(5):
        client.run_cell("# probe\nprint(1)", bridge.port)
    texts = [text for _, text in kept()]
    assert "".join(texts[:-1]) == "x" * 40 and texts[-1] == "after"


def test_an_outbox_cut_short_is_read_again_from_its_start(bridge):
    post("before the cut, a long line")
    client.run_cell("# probe\nprint(1)", bridge.port)
    os.remove(events.OUTBOX)
    post("after")
    client.run_cell("# probe\nprint(1)", bridge.port)
    assert kept() == [("info", "before the cut, a long line"), ("info", "after")]


def test_each_runtime_has_its_own_place(tmp_path):
    state = str(tmp_path)
    events.record(state, [{"host": "old", "end": 50, "line": "on the first runtime"}])
    # A new runtime's outbox starts at 0 again.
    assert [e["text"] for e in events.record(state, [{"host": "new", "end": 10, "line": "on the next"}])] == ["on the next"]
    assert events.offsets(state) == {"old": 50, "new": 10}
    assert events.with_drain("# t\nprint(1)", events.offsets(state)).startswith("# t\nexec(")


def test_a_drain_that_fails_says_so_once(tmp_path, bridge):
    state = str(tmp_path)
    failed = [{"host": "h", "error": "PermissionError: [Errno 13] Permission denied"}]
    assert [e["level"] for e in events.record(state, failed)] == ["error"]
    assert events.record(state, failed) == []
    # For real: an outbox that is a folder cannot be read; the cell runs all the same.
    os.makedirs(events.OUTBOX)
    assert client.run_cell("# probe\nprint(7)", bridge.port) == "7\n"
    assert kept()[0][0] == "error" and "could not read the runtime's outbox" in kept()[0][1]


def test_the_kept_events_stay_small(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "KEEP_BYTES", 2000)
    state = str(tmp_path)
    for i in range(60):
        events.record(state, [{"host": "h", "end": i + 1, "line": f"event {i}"}])
    texts = [e["text"] for e in events.read(state)]
    assert len(texts) < 60 and texts[-1] == "event 59"
    assert os.path.getsize(os.path.join(state, events.EVENTS_FILE)) <= 2000


def test_parse():
    assert events.parse('{"level": "alert", "text": "disk 91%", "source": "watch", "t": 3}') == {
        "level": "alert", "text": "disk 91%", "source": "watch", "t": 3.0}
    assert events.parse("just words\n") == {"level": "info", "text": "just words", "source": None, "t": None}
    assert events.parse('{"other": 1}')["text"] == '{"other": 1}'
    assert events.parse("[1, 2]")["text"] == "[1, 2]"


def test_cli_events(bridge, capsys, tmp_path):
    post(json.dumps({"level": "alert", "text": "learner died"}), "all well")
    cell = str(tmp_path / "cell.py")
    with open(cell, "w") as f:
        f.write("# cli probe\nprint('hi')\n")
    assert cli.main(["--port", str(bridge.port), "run", cell]) == 0
    assert capsys.readouterr().out == "hi\n\n"
    assert cli.main(["--port", str(bridge.port), "events", "--level", "alert"]) == 0
    out = capsys.readouterr().out
    assert "alert: learner died" in out and "all well" not in out
    assert cli.main(["--port", str(bridge.port), "events", "--json"]) == 0
    assert [json.loads(line)["text"] for line in capsys.readouterr().out.splitlines()] == ["learner died", "all well"]
