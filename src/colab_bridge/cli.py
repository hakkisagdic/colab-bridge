"""
colab-bridge: keep a Google Colab tab connected to this machine and drive its notebook from scripts.

  colab-bridge start                 run the bridge in the background (the Colab link of an earlier run keeps working)
  colab-bridge link                  print the link to open in the Colab tab
  colab-bridge status                is a Colab tab connected?
  colab-bridge run FILE.py [--env NAME=VALUE ...]
                                     run a Python file as a notebook cell and print its output
  colab-bridge fetch REMOTE [LOCAL]  copy a file from the Colab runtime to this machine
  colab-bridge tools                 list the notebook tools the tab offers
  colab-bridge stop | restart        stop the background bridge, or restart it with the same link
  colab-bridge serve                 run the bridge in the foreground

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

from colab_bridge import client, registry, server

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
        return 0
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
    print(f"Bridge running on 127.0.0.1:{args.port}. Open this link in the Colab tab (paste it, do not reload):")
    print(read_link(args.dir))
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


def cmd_run(args):
    with open(args.file, encoding="utf-8") as f:
        code = f.read()
    if args.env:
        pairs = [item.split("=", 1) for item in args.env]
        if any(len(pair) != 2 or not pair[0] for pair in pairs):
            print("--env takes NAME=VALUE", file=sys.stderr)
            return 2
        code = client.with_env(code, dict(pairs), os.path.basename(args.file))
    print(client.run_cell(code, args.port, args.project))
    return 0


def cmd_fetch(args):
    local = args.local or os.path.basename(args.remote)
    if os.path.isdir(local):
        local = os.path.join(local, os.path.basename(args.remote))
    started = time.time()

    def progress(done, total):
        print(f"\r{done / 1e6:.1f} of {total / 1e6:.1f} MB", end="", file=sys.stderr, flush=True)

    total = client.fetch(args.remote, local, args.port, progress, args.project)
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
    output = client.run_cell(PROBE_CELL, args.port, args.project).strip()
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


def cmd_who(args):
    data = registry.snapshot()
    if args.json:
        print(json.dumps(data, indent=1))
        return 0
    now = time.time()
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
        output = client.run_cell(RELEASE_CELL, args.port, args.project, expect_host=host, timeout=180)
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

    for name, function in (("serve", cmd_serve), ("start", cmd_start), ("restart", cmd_restart)):
        command = sub.add_parser(name)
        command.add_argument("--new-link", action="store_true",
                             help="make a new token and WebSocket port instead of reusing the earlier link")
        bridge_options(command)
        command.set_defaults(fn=function)
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
    who = sub.add_parser("who", help="runtimes, their bridges and claims")
    who.add_argument("--json", action="store_true")
    who.set_defaults(fn=cmd_who)
    release_runtime = sub.add_parser("release-runtime", help="release the runtime this bridge's tab is on")
    release_runtime.add_argument("--force", action="store_true", help="even while other projects claim it")
    release_runtime.set_defaults(fn=cmd_release_runtime)
    forget = sub.add_parser("forget", help="drop a runtime that is gone from the record")
    forget.add_argument("host", nargs="?", help="its host name (default: the one this bridge's tab is on)")
    forget.set_defaults(fn=cmd_forget)

    args = parser.parse_args(argv)
    args.dir = os.path.expanduser(args.dir or os.environ.get("COLAB_BRIDGE_DIR")
                                  or f"~/.cache/colab-bridge/{args.port}")
    args.project = args.project or os.environ.get("COLAB_BRIDGE_PROJECT") or os.path.basename(os.getcwd()) or "default"
    try:
        return args.fn(args)
    except client.BridgeError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
