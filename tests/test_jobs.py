"""Jobs (colab_bridge.jobs): long work started by a short cell, in processes of its own, asked about and stopped by
short cells. The fake tab's cells run in this process, so the jobs run here for real."""

import json
import os
import sys
import time
import types

import pytest

from colab_bridge import cli, client, events, jobs


def wait_for(name, port, state="ended", seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        job = jobs.status(name, port)["jobs"][0]
        if job["state"] == state:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {name} is {job['state']}, not {state}")


def kept():
    return [(e["level"], e["text"]) for e in events.read(client.default_dir())]


@pytest.fixture
def stop_all(bridge):
    """Stops whatever a test leaves running."""
    yield
    for job in jobs.status(None, bridge.port)["jobs"]:
        if job["state"] == "running":
            jobs.stop(job["name"], bridge.port, kill=True)


def test_a_job_runs_on_after_its_cell_and_says_how_it_ended(bridge, stop_all):
    code = "import time\nprint('working', flush=True)\ntime.sleep(1)\nprint('3 runs shipped')\n"
    started = time.time()
    meta = jobs.start(code, "ship", bridge.port, project="alpha")
    assert time.time() - started < 5, "the cell that starts it returns at once"
    assert meta["name"] == "ship" and meta["project"] == "alpha" and meta["pid"] > 0
    assert jobs.status("ship", bridge.port)["jobs"][0]["state"] == "running"
    with pytest.raises(jobs.JobError, match="job ship runs already"):
        jobs.start(code, "ship", bridge.port)
    job = wait_for("ship", bridge.port)
    assert (job["exit"], job["stopped"], job["last"][-1]) == (0, False, "3 runs shipped")
    # Its end came home as an event, with the next cell.
    client.run_cell("# probe\nprint(1)", bridge.port)
    assert kept() == [("info", "job ship finished after 1 s; its log ends: 3 runs shipped")]
    # Ended, the name can start again.
    jobs.start("print('again')\n", "ship", bridge.port)
    assert wait_for("ship", bridge.port)["last"] == ["again"]


def test_a_failed_job_is_an_alert(bridge, stop_all):
    jobs.start("import sys\nprint('Traceback: no space left on device')\nsys.exit(3)\n", "pack", bridge.port)
    job = wait_for("pack", bridge.port)
    assert job["exit"] == 3
    client.run_cell("# probe\nprint(1)", bridge.port)
    assert kept()[-1] == ("alert", "job pack failed with exit code 3 after 0 s; its log ends: Traceback: no space left "
                                   "on device")


def test_a_job_is_stopped_with_its_children(bridge, stop_all, tmp_path):
    marker = tmp_path / "child-alive"
    child = tmp_path / "child.py"
    child.write_text(f"import time\nwhile True:\n    open({str(marker)!r}, 'w').close()\n    time.sleep(0.1)\n")
    code = ("import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
            "print('started its child', flush=True)\n"
            "time.sleep(60)\n")
    jobs.start(code, "long", bridge.port)
    deadline = time.time() + 10
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists()
    answer = jobs.stop("long", bridge.port)
    assert answer["state"] == "ended"
    job = jobs.status("long", bridge.port)["jobs"][0]
    assert job["stopped"] is True and job["exit"] == -15
    marker.unlink()
    time.sleep(0.5)
    assert not marker.exists(), "the job's own child stopped with it"
    assert kept()[-1][0] == "info" and kept()[-1][1].startswith("job long was stopped after")
    assert jobs.stop("long", bridge.port)["said"] == "it had ended already"


def test_kill_ends_a_job_that_ignores_term(bridge, stop_all):
    code = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('stubborn', flush=True)\ntime.sleep(60)\n"
    jobs.start(code, "stubborn", bridge.port)
    wait_for("stubborn", bridge.port, "running")
    time.sleep(0.5)  # its handler set
    answer = jobs.stop("stubborn", bridge.port, kill=True)
    assert answer["state"] == "ended" and answer["said"] == "killed"
    job = jobs.status("stubborn", bridge.port)["jobs"][0]
    assert (job["state"], job["exit"], job["stopped"]) == ("ended", -9, True)


def test_a_shell_job_env_and_secrets(bridge, stop_all, monkeypatch, tmp_path):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    jobs.start('echo "from bash in $PWD: $GREETING"\n', "sh-job", bridge.port, env={"GREETING": "hello"},
               cwd=str(tmp_path), shell=True)
    assert wait_for("sh-job", bridge.port)["last"] == [f"from bash in {os.path.realpath(tmp_path)}: hello"]
    # A notebook secret is read on the runtime, in the cell, and given to the job alone.
    colab = types.ModuleType("google.colab")
    colab.userdata = types.SimpleNamespace(get=lambda name: {"HF_TOKEN": "hf_secret"}[name])
    monkeypatch.setitem(sys.modules, "google", sys.modules.get("google") or types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.colab", colab)
    output = client.run_cell(jobs._cell("start a job", jobs.START, {
        "jobs": jobs.JOBS_DIR, "outbox": events.OUTBOX, "marker": jobs.MARKER, "name": "secret", "project": None,
        "code": "import os\nprint(len(os.environ['HF_TOKEN']))\n", "env": {}, "secrets": ["HF_TOKEN"], "cwd": None,
        "shell": False, "launch": jobs.LAUNCH, "supervisor": jobs.SUPERVISOR}), bridge.port)
    assert "hf_secret" not in output, "the secret is never printed"
    assert wait_for("secret", bridge.port)["last"] == ["9"]
    assert "HF_TOKEN" not in os.environ


def test_status_of_every_job_and_of_a_lost_one(bridge, stop_all):
    assert jobs.status(None, bridge.port)["jobs"] == []
    jobs.start("print('a')\n", "a", bridge.port)
    wait_for("a", bridge.port)
    jobs.start("import time\ntime.sleep(30)\n", "b", bridge.port)
    # A job whose supervisor died without a word (killed with the machine's processes, say).
    folder = os.path.join(jobs.JOBS_DIR, "gone")
    os.makedirs(folder)
    with open(os.path.join(folder, "meta.json"), "w") as f:
        json.dump({"name": "gone", "pid": 2 ** 22 + 7, "started": time.time() - 60}, f)
    states = {j["name"]: j["state"] for j in jobs.status(None, bridge.port)["jobs"]}
    assert states == {"a": "ended", "b": "running", "gone": "lost"}
    assert jobs.status("nothing", bridge.port)["jobs"] == [{"name": "nothing", "state": "unknown"}]
    with pytest.raises(jobs.JobError, match="no job nothing"):
        jobs.stop("nothing", bridge.port)
    with pytest.raises(jobs.JobError, match="not a job name"):
        jobs.status("../etc", bridge.port)


def test_names():
    assert jobs.name_for("/x/cell-train adapters.py") == "cell-train-adapters"
    assert jobs.name_for("ship.sh") == "ship"
    with pytest.raises(jobs.JobError):
        jobs.check_name("-x")


def test_cli_jobs(bridge, stop_all, tmp_path, capsys):
    port = ["--port", str(bridge.port), "--project", "alpha"]
    script = tmp_path / "count.py"
    script.write_text("import os, time\nprint('counting to', os.environ['TO'])\ntime.sleep(0.5)\nprint('done')\n")
    assert cli.main(port + ["job", "start", str(script), "--env", "TO=3", "--json"]) == 0
    meta = json.loads(capsys.readouterr().out)
    assert meta["name"] == "count"
    assert cli.main(port + ["job", "status", "count"]) == 0
    assert capsys.readouterr().out.startswith("count [alpha]: running for")
    wait_for("count", bridge.port)
    assert cli.main(port + ["job", "status"]) == 0
    out = capsys.readouterr().out
    assert "count [alpha]: finished" in out and "  done" in out
    assert cli.main(port + ["job", "start", str(script), "--env", "broken"]) == 2
    assert cli.main(port + ["job", "start", str(script), "--name", "../bad"]) == 1
    assert "not a job name" in capsys.readouterr().err
    assert cli.main(port + ["job", "stop", "count"]) == 0
    assert "it had ended already" in capsys.readouterr().out
