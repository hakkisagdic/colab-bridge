"""
colab-bridge: keep a Google Colab tab connected to this machine and drive its notebook from scripts.

  colab-bridge start [--open]        run the bridge in the background (the Colab link of an earlier run keeps working)
  colab-bridge open                  open the link in colab-bridge's own Chrome, which keeps the tab at full speed
                                     while it is hidden (sign in to Google there once)
  colab-bridge link                  print the link to open in the Colab tab
  colab-bridge status                is a Colab tab connected?
  colab-bridge run FILE.py [--env NAME=VALUE ...]
                                     run a Python file as a notebook cell and print its output
  colab-bridge fetch REMOTE [LOCAL]  copy a file from the Colab runtime to this machine
  colab-bridge tools                 list the notebook tools the tab offers
  colab-bridge stop | restart        stop the background bridge, or restart it with the same link
  colab-bridge serve                 run the bridge in the foreground
  colab-bridge quota [--json]        the account's compute units: how many are left, how many an hour go, how long
                                     they last (read from the Colab tab in colab-bridge's own Chrome)

Long work without holding the notebook, and messages from the runtime:
  colab-bridge job start FILE [--name NAME] [--env NAME=VALUE ...] [--secret NAME ...] [--cwd DIR]
                                     run a Python file (or a .sh script) on the runtime in a process of its own; the
                                     cell that starts it returns at once
  colab-bridge job status [NAME] [--lines N]
                                     how the jobs do: running, ended (with the exit code) or lost; their logs' ends
  colab-bridge job stop NAME [--kill]
                                     end a job (TERM to its processes, or KILL)
  colab-bridge events [--since 24h] [--level alert]
                                     what processes on the runtime left in its outbox (/tmp/colab-bridge/outbox.jsonl);
                                     every cell brings it here, into <dir>/events.jsonl

Notices on your phone, through a Telegram bot of your own:
  colab-bridge telegram setup [--status-command CMD --status-dir DIR]
                                     once: the bot's token from the macOS keychain (service colab-bridge-telegram), your
                                     chat from the /start you send the bot; running bridges then send their notices and
                                     the runtime's alerts there
  colab-bridge notify TEXT [--every MINUTES]
                                     send TEXT (or stdin, for -) there: the same text at most once an hour, 20 messages
                                     an hour at most
  colab-bridge telegram status | off | listen
                                     what is set up; stop sending; answer /status (or /durum) from your chat with the
                                     status command's output, and nothing else

Sharing a runtime between projects (tabs opened from bridge links share one runtime):
  colab-bridge claim [--vram GB] [--for 90m] [NOTE ...]
                                     tell the others you use the runtime, with how much GPU memory, for how long
  colab-bridge release               end your project's claims
  colab-bridge who                   runtimes, the bridges on them, their claims and when each ran its last cell
  colab-bridge release-runtime       release the runtime (refused while another project claims it)
  colab-bridge forget [HOST]         drop a runtime that is gone from the record

Several bridges can run side by side, one per Colab tab: give each its own --port (default 8765, or
COLAB_BRIDGE_PORT); its link, log and process id live in --dir (default ~/.cache/colab-bridge/<port>, or
COLAB_BRIDGE_DIR). --project (default COLAB_BRIDGE_PROJECT, or the current folder's name) names who runs cells and
holds claims; claims live in ~/.cache/colab-bridge/registry.json (or COLAB_BRIDGE_REGISTRY), shared by all bridges.
"""

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

from colab_bridge import browser, client, events, jobs, quota, registry, server, telegram

PROBE_CELL = """# colab-bridge: runtime probe
import json, socket, subprocess
try:
    name, total, used = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                                        "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                       timeout=30).stdout.strip().splitlines()[0].split(", ")
    gpu = {"gpu": name, "total_gb": round(int(total) / 1024, 1), "free_gb": round((int(total) - int(used)) / 1024, 1)}
except Exception:
    gpu = {"gpu": None, "total_gb": None, "free_gb": None}
print(json.dumps({"host": socket.gethostname(), **gpu}))
"""
RELEASE_CELL = """# colab-bridge: release the runtime
from google.colab import runtime
print("releasing the runtime", flush=True)
runtime.unassign()
"""


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def read_link(state_dir: str) -> str:
    try:
        with open(os.path.join(state_dir, server.LINK_FILE)) as f:
            return f.read().strip()
    except OSError:
        return ""


def bridge_pid(state_dir: str):
    """The process id of this state directory's bridge when that process still runs a bridge, else None."""
    try:
        with open(os.path.join(state_dir, server.PID_FILE)) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    return pid if "colab_bridge" in command else None


def cmd_serve(args):
    token, ws_port = (None, None) if args.new_link else server.link_parts(args.dir)
    token = os.environ.get("COLAB_BRIDGE_TOKEN") or token
    ws_port = int(os.environ.get("COLAB_BRIDGE_WS_PORT") or 0) or ws_port
    try:
        asyncio.run(server.serve(args.port, args.dir, token, ws_port, args.idle_reminder, args.notify_command))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_start(args):
    if port_open(args.port):
        print(f"A bridge already answers on 127.0.0.1:{args.port}.")
        link = read_link(args.dir)
        if link:
            print(link)
        return cmd_open(args) if args.open and link else 0
    os.makedirs(args.dir, exist_ok=True)
    command = [sys.executable, "-m", "colab_bridge", "--port", str(args.port), "--dir", args.dir, "serve",
               "--idle-reminder", str(args.idle_reminder)]
    if args.new_link:
        command.append("--new-link")
    if args.notify_command:
        command += ["--notify-command", args.notify_command]
    with open(os.path.join(args.dir, "bridge.out"), "a") as out:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True)
    deadline = time.time() + 30
    while time.time() < deadline and not port_open(args.port):
        time.sleep(0.5)
    if not port_open(args.port):
        print(f"The bridge did not start; see {os.path.join(args.dir, 'bridge.out')}.", file=sys.stderr)
        return 1
    print(f"Bridge running on 127.0.0.1:{args.port}. Open this link in the Colab tab (paste it, do not reload), or "
          "open it with `colab-bridge open`:")
    print(read_link(args.dir))
    return cmd_open(args) if args.open else 0


def cmd_open(args):
    link = read_link(args.dir)
    if not link:
        print(f"No link in {args.dir} yet: run `colab-bridge start`.", file=sys.stderr)
        return 1
    try:
        browser.open_link(link, args.chrome, args.profile, args.debug_port)
    except (FileNotFoundError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Opened the Colab link in colab-bridge's own Chrome ({args.profile or browser.DEFAULT_PROFILE}), which keeps "
          "the tab at full speed while it is hidden and lets the bridge read the account's compute units from it. The "
          "first time, sign in to Google in that window; then connect a runtime (for example a GPU) in the tab. Close "
          "any older tab of this bridge.")
    return 0


def cmd_stop(args):
    pid = bridge_pid(args.dir)
    if pid is None:
        print(f"No bridge of {args.dir} is running.")
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 10
    while time.time() < deadline and bridge_pid(args.dir) is not None:
        time.sleep(0.3)
    if bridge_pid(args.dir) is not None:
        print(f"The bridge (pid {pid}) did not stop.", file=sys.stderr)
        return 1
    os.remove(os.path.join(args.dir, server.PID_FILE))
    print(f"Stopped the bridge (pid {pid}).")
    return 0


def cmd_restart(args):
    return cmd_stop(args) or cmd_start(args)


def cmd_link(args):
    link = read_link(args.dir)
    if not link:
        print(f"No link in {args.dir} yet: run `colab-bridge start`.", file=sys.stderr)
        return 1
    print(link)
    return 0


def cmd_status(args):
    print(json.dumps(client.status(args.port)))
    return 0


def cmd_tools(args):
    for tool in client.request({"op": "list"}, args.port):
        print(tool["name"])
    return 0


def env_pairs(items: list):
    """--env NAME=VALUE values as a dict, or None when one is not NAME=VALUE."""
    pairs = [item.split("=", 1) for item in items]
    if any(len(pair) != 2 or not pair[0] for pair in pairs):
        print("--env takes NAME=VALUE", file=sys.stderr)
        return None
    return dict(pairs)


def cmd_run(args):
    with open(args.file, encoding="utf-8") as f:
        code = f.read()
    if args.env:
        env = env_pairs(args.env)
        if env is None:
            return 2
        code = client.with_env(code, env, os.path.basename(args.file))
    print(client.run_cell(code, args.port, args.project, state_dir=args.dir))
    return 0


def ago(seconds: float) -> str:
    return registry.minutes(seconds) if seconds >= 60 else f"{max(seconds, 0):.0f} s"


def cmd_job_start(args):
    with open(args.file, encoding="utf-8") as f:
        code = f.read()
    env = env_pairs(args.env)
    if env is None:
        return 2
    name = jobs.check_name(args.name) if args.name else jobs.name_for(args.file)
    meta = jobs.start(code, name, args.port, args.project, env, args.secret, args.cwd, args.file.endswith(".sh"),
                      args.dir)
    if args.json:
        print(json.dumps(meta))
    else:
        print(f"Started job {meta['name']} on {meta['host']} (pid {meta['pid']}), its log in {meta['log']}. "
              f"See how it does with: colab-bridge job status {meta['name']}")
    return 0


def describe_job(job: dict, now: float) -> str:
    started = job.get("started") or now
    if job["state"] == "running":
        how = f"running for {ago(now - started)}"
    elif job["state"] == "ended":
        code = job.get("exit")
        how = ("stopped" if job.get("stopped") else "finished" if code == 0
               else f"killed by signal {-code}" if isinstance(code, int) and code < 0 else f"failed (exit {code})")
        how += f" {ago(now - (job.get('ended') or now))} ago, after {ago((job.get('ended') or now) - started)}"
    else:
        how = job["state"] + (": its supervisor is gone and said nothing" if job["state"] == "lost" else "")
    project = f" [{job['project']}]" if job.get("project") else ""
    return f"{job['name']}{project}: {how}" + (f" (pid {job['pid']})" if job["state"] == "running" else "")


def cmd_job_status(args):
    answer = jobs.status(args.name, args.port, args.project, args.lines, args.dir)
    if args.json:
        print(json.dumps(answer))
        return 0
    if not answer["jobs"]:
        print(f"No job on {answer['host']}.")
    now = time.time()
    for job in answer["jobs"]:
        print(describe_job(job, now))
        for line in job.get("last") or []:
            print(f"  {line}")
    return 0 if all(j["state"] != "unknown" for j in answer["jobs"]) else 1


def cmd_job_stop(args):
    answer = jobs.stop(args.name, args.port, args.project, args.kill, args.dir)
    if args.json:
        print(json.dumps(answer))
    else:
        print(f"Job {answer['name']}: {answer['state']} ({answer['said']}).")
    return 0


def cmd_events(args):
    found = [e for e in events.read(args.dir, time.time() - args.since)
             if not args.level or e.get("level") == args.level]
    for e in found:
        if args.json:
            print(json.dumps(e, ensure_ascii=False))
        else:
            source = f" ({e['source']})" if e.get("source") else ""
            print(f"{registry.clock(e.get('t') or e['at'])} {e.get('host')} {e.get('level')}: {e.get('text')}{source}")
    if not found and not args.json:
        print("No events from the runtime in that time. They come with the cells sent through this bridge.")
    return 0


def cmd_notify(args):
    text = sys.stdin.read() if args.text == ["-"] else " ".join(args.text)
    result = telegram.notify(text, args.every)
    every = args.every if args.every is not None else telegram.settings().get("same_text_minutes",
                                                                               telegram.SAME_TEXT_MINUTES)
    if args.json:
        print(json.dumps(result))
    elif result["sent"]:
        print("Sent.")
    elif result["skipped"] == "same text":
        print(f"Not sent: the same text went within the last {every:g} min.")
    elif result["skipped"]:
        print("Not sent: this machine's messages of the last hour reached the cap (per_hour in "
              f"{telegram.config_path()}); the next message that goes says how many were held back.")
    else:
        print(f"Not sent: {result['error']}", file=sys.stderr)
    return 1 if result["error"] else 0


def cmd_telegram_setup(args):
    section = telegram.setup(args.chat_id, args.keep_chat, args.wait, args.status_command, args.status_dir)
    print(f"Telegram is set up: notices go to {section['chat']} through {section['bot']} (the chat is kept in "
          f"{telegram.config_path()}; the token stays in the keychain). Running bridges send theirs from now on; one "
          "started before this version of colab-bridge sends them once it is restarted.")
    if section.get("status_command"):
        where = f" in {section['status_dir']}" if section.get("status_dir") else ""
        print(f"/status (or /durum) from that chat runs `{section['status_command']}`{where} while "
              "`colab-bridge telegram listen` runs.")
    return 0


def cmd_telegram_status(args):
    cfg = telegram.settings()
    token = bool(os.environ.get(telegram.TOKEN_ENV)) or telegram.has_keychain_token()
    counts = telegram.sent_last_hour()
    state = {"configured": telegram.configured(cfg), "token": token, "bot": cfg.get("bot"), "chat": cfg.get("chat"),
             "status_command": cfg.get("status_command"), "status_dir": cfg.get("status_dir"),
             "listening": telegram.listening(), "event_levels": telegram.forwarded_levels(cfg),
             "sent_last_hour": counts["sent"], "held": counts["held"],
             "per_hour": cfg.get("per_hour", telegram.PER_HOUR),
             "same_text_minutes": cfg.get("same_text_minutes", telegram.SAME_TEXT_MINUTES),
             "config": telegram.config_path()}
    if args.json:
        print(json.dumps(state, ensure_ascii=False))
        return 0 if state["configured"] else 1
    if not state["configured"]:
        print("Telegram is not set up: run `colab-bridge telegram setup`." + ("" if token else
              f" First store the bot's token: {telegram.STORE_HINT}"))
        return 1
    print(f"Notices go to {state['chat']} through {state['bot']} ({state['config']}); the token is "
          + ("there." if token else f"missing: {telegram.STORE_HINT}"))
    print(f"Sent in the last hour: {state['sent_last_hour']} of {state['per_hour']}"
          + (f", {state['held']} held back by the cap" if state["held"] else "")
          + f"; the same text at most once in {state['same_text_minutes']:g} min. The runtime's events forwarded: "
          + (", ".join(state["event_levels"]) or "none"))
    if state["status_command"]:
        where = f" in {state['status_dir']}" if state["status_dir"] else ""
        listener = "a listener answers" if state["listening"] else "no listener runs (colab-bridge telegram listen)"
        print(f"/status runs `{state['status_command']}`{where}; {listener}.")
    return 0


def cmd_telegram_off(args):
    was = telegram.off()
    removes = f"`security delete-generic-password -s {telegram.KEYCHAIN_SERVICE}` removes it"
    print(f"Telegram is off: nothing more goes to the chat. The token stays in the keychain ({removes})." if was
          else "Telegram was not set up.")
    return 0


def cmd_telegram_listen(args):
    try:
        return telegram.listen(out=lambda line: print(line, flush=True))
    except KeyboardInterrupt:
        return 0


def cmd_fetch(args):
    local = args.local or os.path.basename(args.remote)
    if os.path.isdir(local):
        local = os.path.join(local, os.path.basename(args.remote))
    started = time.time()

    def progress(done, total):
        print(f"\r{done / 1e6:.1f} of {total / 1e6:.1f} MB", end="", file=sys.stderr, flush=True)

    total = client.fetch(args.remote, local, args.port, progress, args.project, state_dir=args.dir)
    print(f"\n{local} ({total / 1e6:.1f} MB in {time.time() - started:.0f} s)")
    return 0


def duration_seconds(text: str) -> float:
    """90m, 2h, 1h30m, 45s, or a number of minutes."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text) * 60
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", text)
    if not text or not match:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (examples: 90m, 2h, 1h30m)")
    hours, mins, secs = (float(g) if g else 0.0 for g in match.groups())
    return hours * 3600 + mins * 60 + secs


def probe(args) -> dict:
    """The runtime the bridge's tab is on: {"host", "gpu", "total_gb", "free_gb"}."""
    output = client.run_cell(PROBE_CELL, args.port, args.project, state_dir=args.dir).strip()
    try:
        return json.loads(output.splitlines()[-1])
    except (IndexError, ValueError):
        raise client.BridgeError(f"The runtime probe printed no answer: {output[-500:]}")


def cmd_claim(args):
    info = probe(args)
    note = " ".join(args.note)
    try:
        with registry.locked() as data:
            entry = registry.claim(data, args.project, info["host"], args.port, time.time(), args.duration, args.vram,
                                   note, info["gpu"], info["total_gb"], info["free_gb"])
            others = [c for c in registry.active(data["claims"], time.time())
                      if c["host"] == info["host"] and c["project"] != args.project]
    except registry.ClaimError as e:
        print(f"Not claimed: {e}", file=sys.stderr)
        return 1
    memory = f" with {args.vram:g} of {info['total_gb']:.1f} GB" if args.vram else ""
    print(f"{args.project} claims {info['host']} ({info['gpu'] or 'no GPU'}){memory} until "
          f"{registry.clock(entry['until'])}.")
    for c in others:
        print(f"  also: {registry.describe(c, time.time())}")
    return 0


def cmd_release(args):
    with registry.locked() as data:
        ended = registry.release(data, args.project)
    live = [c for c in ended if c["until"] > time.time()]
    print(f"Ended {args.project}'s claims: " + ", ".join(f"{c['host']} ({c['note'] or 'no note'})" for c in live)
          if live else f"{args.project} holds no claim.")
    return 0


def cmd_quota(args):
    now = time.time()
    try:
        reading = quota.read(args.debug_port, args.authuser)
        with registry.locked() as data:
            data["quota"]["reading"] = reading
        live, problem = True, None
    except quota.QuotaError as e:
        reading, live, problem = registry.snapshot()["quota"].get("reading"), False, str(e)
    if args.json:
        current = reading and (reading if live else quota.estimate(reading, now))
        print(json.dumps({"live": live, "reading": reading, "now": current,
                          "hours_left": current and quota.hours_left(current), "problem": problem}))
        return 0 if reading else 1
    if reading:
        print(quota.describe(reading, now, live))
    if problem:
        print(("Not read now: " if reading else "") + problem, file=sys.stderr)
    return 0 if reading else 1


def cmd_who(args):
    data = registry.snapshot()
    if args.json:
        print(json.dumps(data, indent=1))
        return 0
    now = time.time()
    reading = data["quota"].get("reading")
    if reading:
        print("compute units: " + quota.describe(reading, now, live=now - reading["at"] < 120))
    hosts = sorted(set(data["hosts"]) | {c["host"] for c in data["claims"]}
                   | {b["host"] for b in data["bridges"].values() if b.get("host")})
    if not hosts:
        print("No runtime is known yet: run a cell through a bridge first.")
    for host in hosts:
        seen = data["hosts"].get(host, {})
        print(f"{host}  {seen.get('gpu') or 'no GPU'}")
        ports = sorted(p for p, b in data["bridges"].items() if b.get("host") == host)
        print("  tabs:  " + (", ".join(f"bridge {p}" for p in ports) if ports
                             else "none now (Colab moved the tab away, or the runtime is gone)"))
        if seen.get("last_run"):
            print(f"  last cell {registry.minutes(now - seen['last_run'])} ago, by {seen.get('last_project')} "
                  f"through bridge {seen.get('last_bridge')}")
        for c in sorted((c for c in data["claims"] if c["host"] == host), key=lambda c: c["until"]):
            if c["until"] > now:
                print(f"  claim: {registry.describe(c, now)}")
            else:
                print(f"  ended: {c['project']} at {registry.clock(c['until'])}" + (f" ({c['note']})" if c["note"] else ""))
    return 0


def cmd_release_runtime(args):
    data = registry.snapshot()
    host = data["bridges"].get(str(args.port), {}).get("host")
    if not host:
        print("This bridge has run no cell yet, so it is not known which runtime it would release: run a cell first "
              "(for example `colab-bridge claim`).", file=sys.stderr)
        return 1
    others = [c for c in registry.active(data["claims"], time.time()) if c["host"] == host
              and c["project"] != args.project]
    if others and not args.force:
        print(f"Not released: {host} is claimed: " + "; ".join(registry.describe(c, time.time()) for c in others)
              + ". Agree with them first, or pass --force.", file=sys.stderr)
        return 1
    try:
        output = client.run_cell(RELEASE_CELL, args.port, args.project, expect_host=host, timeout=180,
                                 state_dir=args.dir)
    except client.BridgeError as e:
        print(f"{e}\nThe runtime may not be released: look at the Colab tab. If {host} is gone, run: "
              f"colab-bridge forget {host}", file=sys.stderr)
        return 1
    if "releasing the runtime" not in output:
        print(f"The release cell did not run: {output.strip()[-500:]}", file=sys.stderr)
        return 1
    with registry.locked() as data:
        registry.forget(data, host)
    print(f"Released {host}.")
    return 0


def cmd_forget(args):
    host = args.host or registry.snapshot()["bridges"].get(str(args.port), {}).get("host")
    if not host:
        print("Name the runtime to forget (see `colab-bridge who`).", file=sys.stderr)
        return 1
    with registry.locked() as data:
        known = registry.forget(data, host)
    print(f"Forgot {host} and the claims on it." if known else f"{host} was not in the record.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="colab-bridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=client.default_port(),
                        help="control port on 127.0.0.1 (default: 8765 or COLAB_BRIDGE_PORT)")
    parser.add_argument("--dir", help="where the link, log and process id live "
                                      "(default: ~/.cache/colab-bridge/<port> or COLAB_BRIDGE_DIR)")
    parser.add_argument("--project", help="who runs cells and holds claims (default: COLAB_BRIDGE_PROJECT, or the "
                                          "current folder's name)")
    sub = parser.add_subparsers(dest="command", required=True)

    def bridge_options(command):
        command.add_argument("--idle-reminder", type=float, default=server.IDLE_MINUTES, metavar="MINUTES",
                             help="remind about a runtime that ran no cell and had no claim for this long "
                                  f"(default: {server.IDLE_MINUTES:g}; 0 turns reminders off)")
        command.add_argument("--notify-command", default=os.environ.get("COLAB_BRIDGE_NOTIFY"), metavar="COMMAND",
                             help="shell command for notices, with the message in COLAB_BRIDGE_MESSAGE (default: "
                                  "COLAB_BRIDGE_NOTIFY, else a desktop notification on macOS)")

    def open_options(command):
        command.add_argument("--chrome", default=None, metavar="PATH",
                             help="Chrome or Chromium to open the link with (default: COLAB_BRIDGE_CHROME, the installed "
                                  "Google Chrome, or one on PATH)")
        command.add_argument("--profile", default=None, metavar="DIR",
                             help=f"its profile (default: {browser.DEFAULT_PROFILE})")
        debug_option(command)

    def debug_option(command):
        command.add_argument("--debug-port", type=int, default=None, metavar="PORT",
                             help="the debugging port of colab-bridge's Chrome, on 127.0.0.1 (default: "
                                  f"{quota.DEFAULT_DEBUG_PORT} or {quota.DEBUG_PORT_ENV})")

    for name, function in (("serve", cmd_serve), ("start", cmd_start), ("restart", cmd_restart)):
        command = sub.add_parser(name)
        command.add_argument("--new-link", action="store_true",
                             help="make a new token and WebSocket port instead of reusing the earlier link")
        bridge_options(command)
        if name != "serve":
            command.add_argument("--open", action="store_true", help="then open the link as `colab-bridge open` does")
            open_options(command)
        command.set_defaults(fn=function)
    open_command = sub.add_parser("open", help="open the link in colab-bridge's own Chrome, unthrottled")
    open_options(open_command)
    open_command.set_defaults(fn=cmd_open)
    for name, function in (("stop", cmd_stop), ("link", cmd_link), ("status", cmd_status), ("tools", cmd_tools),
                           ("release", cmd_release)):
        command = sub.add_parser(name)
        command.set_defaults(fn=function, new_link=False)
    run = sub.add_parser("run")
    run.add_argument("file")
    run.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                     help="environment variable for this cell only (repeatable)")
    run.set_defaults(fn=cmd_run)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("remote", help="path of the file on the Colab runtime")
    fetch.add_argument("local", nargs="?", help="where to save it (default: its file name, here)")
    fetch.set_defaults(fn=cmd_fetch)
    claim = sub.add_parser("claim", help="tell the other projects you use the runtime")
    claim.add_argument("--vram", type=float, metavar="GB", help="GPU memory you will use, in GB as nvidia-smi counts")
    claim.add_argument("--for", dest="duration", type=duration_seconds, default=duration_seconds("60m"),
                       metavar="DURATION", help="how long: 90m, 2h, 1h30m (default: 60m); claim again to renew")
    claim.add_argument("note", nargs="*", help="what runs, for the others")
    claim.set_defaults(fn=cmd_claim)
    quota_command = sub.add_parser("quota", help="the account's compute units, from the Colab tab")
    quota_command.add_argument("--json", action="store_true")
    quota_command.add_argument("--authuser", default=None, metavar="N",
                               help="which signed-in Google account to ask about (default: the tab's)")
    debug_option(quota_command)
    quota_command.set_defaults(fn=cmd_quota)
    who = sub.add_parser("who", help="runtimes, their bridges and claims")
    who.add_argument("--json", action="store_true")
    who.set_defaults(fn=cmd_who)
    release_runtime = sub.add_parser("release-runtime", help="release the runtime this bridge's tab is on")
    release_runtime.add_argument("--force", action="store_true", help="even while other projects claim it")
    release_runtime.set_defaults(fn=cmd_release_runtime)
    forget = sub.add_parser("forget", help="drop a runtime that is gone from the record")
    forget.add_argument("host", nargs="?", help="its host name (default: the one this bridge's tab is on)")
    forget.set_defaults(fn=cmd_forget)
    job = sub.add_parser("job", help="long work on the runtime in a process of its own, not in a cell")
    job_sub = job.add_subparsers(dest="job_command", required=True)
    job_start = job_sub.add_parser("start", help="start a Python file (or a .sh script) as a job")
    job_start.add_argument("file")
    job_start.add_argument("--name", help="the job's name (default: the file's), one job at a time under it")
    job_start.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                           help="environment variable for the job (repeatable)")
    job_start.add_argument("--secret", action="append", default=[], metavar="NAME",
                           help="a notebook secret the job gets as an environment variable, read on the runtime "
                                "(repeatable)")
    job_start.add_argument("--cwd", help="where it runs (default: /content, else the home folder)")
    job_start.add_argument("--json", action="store_true")
    job_start.set_defaults(fn=cmd_job_start)
    job_status = job_sub.add_parser("status", help="how one job, or every job, does")
    job_status.add_argument("name", nargs="?")
    job_status.add_argument("--lines", type=int, default=5, help="lines of each log's end (default: 5)")
    job_status.add_argument("--json", action="store_true")
    job_status.set_defaults(fn=cmd_job_status)
    job_stop = job_sub.add_parser("stop", help="end a job")
    job_stop.add_argument("name")
    job_stop.add_argument("--kill", action="store_true", help="KILL instead of TERM")
    job_stop.add_argument("--json", action="store_true")
    job_stop.set_defaults(fn=cmd_job_stop)
    events_command = sub.add_parser("events", help="what processes on the runtime left for this machine")
    events_command.add_argument("--since", type=duration_seconds, default=duration_seconds("24h"), metavar="DURATION",
                                help="how far back: 30m, 2h (default: 24h)")
    events_command.add_argument("--level", help="only these: alert, info, ...")
    events_command.add_argument("--json", action="store_true", help="one JSON object a line")
    events_command.set_defaults(fn=cmd_events)
    notify_command = sub.add_parser("notify", help="send a message to your Telegram chat")
    notify_command.add_argument("text", nargs="+", help="the message (- reads it from stdin)")
    notify_command.add_argument("--every", type=float, metavar="MINUTES",
                                help="send this text at most once in this many minutes (default: same_text_minutes in "
                                     "the config, 60)")
    notify_command.add_argument("--json", action="store_true", help='{"sent", "skipped", "error"}')
    notify_command.set_defaults(fn=cmd_notify)
    tg = sub.add_parser("telegram", help="notices on your phone through a Telegram bot of your own")
    tg_sub = tg.add_subparsers(dest="telegram_command", required=True)
    tg_setup = tg_sub.add_parser("setup", help="the token from the keychain, your chat from its /start to the bot")
    tg_setup.add_argument("--wait", type=float, default=telegram.SETUP_WAIT_S, metavar="SECONDS",
                          help=f"how long to wait for the /start (default: {telegram.SETUP_WAIT_S})")
    tg_setup.add_argument("--chat-id", type=int, help="this chat instead of the one that sends /start")
    tg_setup.add_argument("--keep-chat", action="store_true", help="the chat set up before (to change the rest)")
    tg_setup.add_argument("--status-command", metavar="CMD",
                          help='what /status (or /durum) runs, without a shell (e.g. "python3 tools/status.py"); '
                               '"" removes it')
    tg_setup.add_argument("--status-dir", metavar="DIR", help="where it runs")
    tg_setup.set_defaults(fn=cmd_telegram_setup)
    tg_status = tg_sub.add_parser("status", help="what is set up (never the token or the chat's id)")
    tg_status.add_argument("--json", action="store_true")
    tg_status.set_defaults(fn=cmd_telegram_status)
    tg_sub.add_parser("off", help="stop sending: the chat leaves the config").set_defaults(fn=cmd_telegram_off)
    tg_sub.add_parser("listen", help="answer /status (or /durum) from your chat, nothing else (runs until stopped)") \
        .set_defaults(fn=cmd_telegram_listen)

    args = parser.parse_args(argv)
    args.dir = os.path.expanduser(args.dir) if args.dir else client.default_dir(args.port)
    args.project = args.project or os.environ.get("COLAB_BRIDGE_PROJECT") or os.path.basename(os.getcwd()) or "default"
    try:
        return args.fn(args)
    except (client.BridgeError, telegram.TelegramError) as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
