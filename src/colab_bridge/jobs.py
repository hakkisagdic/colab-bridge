"""
Jobs: long work on the runtime that does not hold the notebook.

A cell holds the notebook while it runs, and the bridge runs one cell at a time, so one long cell makes every other cell
wait behind it, whoever sends them. A job is started by a short cell instead: its file goes to the runtime and runs
there in a process of its own, detached from the notebook, its output in a log; later short cells say how it is doing,
or stop it. When it ends, it leaves an event in the runtime's outbox (colab_bridge.events), which comes to this machine
with the next cell: "job ship finished after 12 min; its log ends: ...", level "alert" when it failed.

On the runtime each job has a folder, /tmp/colab-bridge/jobs/<name>: the file (job.py, or job.sh for a shell script),
meta.json ({"name", "project", "pid", "started", "cwd"}), its log, and exit.json once it ended ({"exit", "ended",
"stopped"}; a negative exit is the signal that ended it). A name runs once at a time; starting it again once it ended
replaces the folder. The job runs with the kernel's environment, the --env values and, read in the cell from the
notebook's secrets, the --secret ones (never printed); its supervisor, the process its pid names, leads the job's
process group, so that a stop reaches the job's own children too.
"""

import json
import os
import re

from colab_bridge import client, events

JOBS_DIR = "/tmp/colab-bridge/jobs"
MARKER = "@@colab-bridge-job@@ "
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# What the runtime's side of every job cell shares: whether a process lives, a log's last lines, a duration in words,
# and an event in the outbox.
COMMON = r'''
import json, os, signal, socket, subprocess, sys, time

def alive(pid):
    """Whether the process runs (a zombie, ended and not yet reaped, does not)."""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True

def tail(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 65536))
            lines = f.read().decode("utf-8", "replace").replace("\r", "\n").splitlines()
    except OSError:
        return []
    return [l.rstrip()[:500] for l in lines if l.strip()][-n:] if n > 0 else []

def duration(seconds):
    seconds = max(seconds, 0)
    return f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.0f} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"

def post(outbox, level, text, **fields):
    os.makedirs(os.path.dirname(outbox), exist_ok=True)
    with open(outbox, "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(t=time.time(), level=level, source="colab-bridge job", text=text, **fields)) + "\n")

def ended(folder, name, outbox, started, code, stopped):
    """Leaves the job's event in the outbox, then its exit.json: a job that shows as ended has said so."""
    now = time.time()
    if stopped:
        how, level = "was stopped", "info"
    elif code == 0:
        how, level = "finished", "info"
    elif code < 0:
        how, level = f"was killed by signal {-code}", "alert"
    else:
        how, level = f"failed with exit code {code}", "alert"
    last = tail(os.path.join(folder, "log"), 1)
    try:
        post(outbox, level, f"job {name} {how} after {duration(now - started)}"
             + (f"; its log ends: {last[0][:300]}" if last else ""), job=name, exit=code)
    finally:
        with open(os.path.join(folder, "exit.json.tmp"), "w") as f:
            json.dump({"exit": code, "ended": now, "stopped": stopped}, f)
        os.replace(os.path.join(folder, "exit.json.tmp"), os.path.join(folder, "exit.json"))
'''

# The supervisor, detached from the notebook (python -c, its own session): runs the job, its output to the log, and
# says how it ended. A TERM (job stop) ends the job; the supervisor waits for it and says so.
SUPERVISOR = COMMON + r'''
folder, name, outbox, command = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
signal.signal(signal.SIGTERM, lambda signum, frame: None)
started = time.time()
with open(os.path.join(folder, "log"), "ab", buffering=0) as log:
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    code = child.wait()
ended(folder, name, outbox, started, code, os.path.exists(os.path.join(folder, "stop")))
'''

# Starts the supervisor through a launcher that exits at once, so that the job is no child of the kernel's (an ended
# child the kernel never reaps would look alive).
LAUNCH = ("import subprocess, sys\n"
          "p = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
          "stderr=subprocess.DEVNULL, start_new_session=True)\n"
          "print(p.pid)\n")

START = COMMON + r'''
def answer(**fields):
    print(marker + json.dumps(fields), flush=True)

folder = os.path.join(jobs, name)
try:
    with open(os.path.join(folder, "meta.json")) as f:
        meta = json.load(f)
except (OSError, ValueError):
    meta = None
if meta and not os.path.exists(os.path.join(folder, "exit.json")) and alive(meta.get("pid")):
    answer(error=f"job {name} runs already (pid {meta['pid']}, started {duration(time.time() - meta['started'])} ago): "
                 "stop it first (colab-bridge job stop), or start this one under another --name")
else:
    environment = dict(os.environ)
    environment.update(env)
    if secrets:
        from google.colab import userdata
        for secret in secrets:
            environment[secret] = userdata.get(secret)
    import shutil
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    script = os.path.join(folder, "job.sh" if shell else "job.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(code)
    where = cwd or ("/content" if os.path.isdir("/content") else os.path.expanduser("~"))
    command = ["bash", script] if shell else [sys.executable, "-u", script]
    launched = subprocess.run([sys.executable, "-c", launch, sys.executable, "-c", supervisor, folder, name, outbox,
                               *command], cwd=where, env=environment, capture_output=True, text=True, timeout=60)
    if launched.returncode or not launched.stdout.strip().isdigit():
        answer(error=f"job {name} did not start: {(launched.stderr or launched.stdout).strip()[-500:]}")
    else:
        meta = {"name": name, "project": project, "pid": int(launched.stdout.strip()), "started": time.time(),
                "cwd": where, "log": os.path.join(folder, "log")}
        with open(os.path.join(folder, "meta.json.tmp"), "w") as f:
            json.dump(meta, f)
        os.replace(os.path.join(folder, "meta.json.tmp"), os.path.join(folder, "meta.json"))
        answer(host=socket.gethostname(), **meta)
'''

STATUS = COMMON + r'''
names = [only] if only else sorted(os.listdir(jobs)) if os.path.isdir(jobs) else []
found = []
for name in names:
    folder = os.path.join(jobs, name)
    try:
        with open(os.path.join(folder, "meta.json")) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        if only:
            found.append({"name": name, "state": "unknown"})
        continue
    try:
        with open(os.path.join(folder, "exit.json")) as f:
            end = json.load(f)
    except (OSError, ValueError):
        end = None
    state = "ended" if end else "running" if alive(meta.get("pid")) else "lost"
    found.append(dict(meta, **(end or {}), state=state, last=tail(os.path.join(folder, "log"), lines)))
found.sort(key=lambda j: j.get("started") or 0, reverse=True)
print(marker + json.dumps({"host": socket.gethostname(), "jobs": found}), flush=True)
'''

STOP = COMMON + r'''
def answer(**fields):
    print(marker + json.dumps(fields), flush=True)

folder = os.path.join(jobs, name)
try:
    with open(os.path.join(folder, "meta.json")) as f:
        meta = json.load(f)
except (OSError, ValueError):
    meta = None
if meta is None:
    answer(error=f"no job {name} on this runtime")
elif os.path.exists(os.path.join(folder, "exit.json")):
    answer(name=name, state="ended", said="it had ended already")
elif not alive(meta["pid"]):
    answer(name=name, state="lost", said="its supervisor is gone, and it said nothing of how it ended")
else:
    open(os.path.join(folder, "stop"), "w").close()
    os.killpg(meta["pid"], signal.SIGKILL if kill else signal.SIGTERM)
    deadline = time.time() + (2 if kill else 10)
    while time.time() < deadline and not os.path.exists(os.path.join(folder, "exit.json")) and alive(meta["pid"]):
        time.sleep(0.1)
    if kill and not os.path.exists(os.path.join(folder, "exit.json")):
        ended(folder, name, outbox, meta["started"], -9, True)  # the supervisor was killed with it
    answer(name=name, state="ended" if os.path.exists(os.path.join(folder, "exit.json")) else "stopping",
           said=("killed" if kill else "asked to stop (TERM)"))
'''


class JobError(client.BridgeError):
    pass


def check_name(name: str) -> str:
    if not NAME.fullmatch(name or ""):
        raise JobError(f"not a job name: {name!r} (letters, digits, '.', '_' and '-', at most 64, a letter or digit "
                       "first)")
    return name


def name_for(path: str) -> str:
    """A job's name from its file's: cell.py -> cell."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.splitext(os.path.basename(path))[0]).strip("-._")
    return check_name(stem[:64] or "job")


def _cell(title: str, source: str, names: dict) -> str:
    return f"# colab-bridge: {title}\nexec({source!r}, {names!r})\n"


def _answer(output: str) -> dict:
    lines = [line for line in output.splitlines() if line.startswith(MARKER)]
    if not lines:
        raise JobError(f"The job cell gave no answer: {output.strip()[-800:]}")
    answer = json.loads(lines[-1][len(MARKER):])
    if "error" in answer:
        raise JobError(answer["error"])
    return answer


def _run(cell: str, port, project, state_dir, timeout=300) -> dict:
    return _answer(client.run(cell, port, project, timeout=timeout, state_dir=state_dir)["output"])


def start(code: str, name: str, port: int = None, project: str = None, env: dict = None, secrets=(), cwd: str = None,
          shell: bool = False, state_dir: str = None) -> dict:
    """Starts code (Python, or a bash script with shell) as the job `name` on the runtime and returns its meta.json
    with the runtime's host. Refused while a job of that name runs."""
    names = {"jobs": JOBS_DIR, "outbox": events.OUTBOX, "marker": MARKER, "name": check_name(name),
             "project": project, "code": code, "env": {str(k): str(v) for k, v in (env or {}).items()},
             "secrets": list(secrets), "cwd": cwd, "shell": shell, "launch": LAUNCH, "supervisor": SUPERVISOR}
    return _run(_cell("start a job", START, names), port, project, state_dir)


def status(name: str = None, port: int = None, project: str = None, lines: int = 5, state_dir: str = None) -> dict:
    """{"host", "jobs": [...]}: one job, or every job on the runtime, the newest first, each with its state
    ("running", "ended", or "lost": its supervisor gone without a word) and the last lines of its log."""
    names = {"jobs": JOBS_DIR, "marker": MARKER, "only": check_name(name) if name else None, "lines": lines}
    return _run(_cell("job status", STATUS, names), port, project, state_dir)


def stop(name: str, port: int = None, project: str = None, kill: bool = False, state_dir: str = None) -> dict:
    """Ends a job: TERM to its process group (KILL with kill), waiting a few seconds for it to end."""
    names = {"jobs": JOBS_DIR, "outbox": events.OUTBOX, "marker": MARKER, "name": check_name(name),
             "kill": kill}
    return _run(_cell("stop a job", STOP, names), port, project, state_dir)
