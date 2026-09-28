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

Several bridges can run side by side, one per Colab tab: give each its own --port (default 8765, or
COLAB_BRIDGE_PORT); its link, log and process id live in --dir (default ~/.cache/colab-bridge/<port>, or
COLAB_BRIDGE_DIR).
"""

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time

from colab_bridge import client, server


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
        asyncio.run(server.serve(args.port, args.dir, token, ws_port))
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
    command = [sys.executable, "-m", "colab_bridge", "--port", str(args.port), "--dir", args.dir, "serve"]
    if args.new_link:
        command.append("--new-link")
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
    print(client.run_cell(code, args.port))
    return 0


def cmd_fetch(args):
    local = args.local or os.path.basename(args.remote)
    if os.path.isdir(local):
        local = os.path.join(local, os.path.basename(args.remote))
    started = time.time()

    def progress(done, total):
        print(f"\r{done / 1e6:.1f} of {total / 1e6:.1f} MB", end="", file=sys.stderr, flush=True)

    total = client.fetch(args.remote, local, args.port, progress)
    print(f"\n{local} ({total / 1e6:.1f} MB in {time.time() - started:.0f} s)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="colab-bridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=client.default_port(),
                        help="control port on 127.0.0.1 (default: 8765 or COLAB_BRIDGE_PORT)")
    parser.add_argument("--dir", help="where the link, log and process id live "
                                      "(default: ~/.cache/colab-bridge/<port> or COLAB_BRIDGE_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, function in (("serve", cmd_serve), ("start", cmd_start)):
        command = sub.add_parser(name)
        command.add_argument("--new-link", action="store_true",
                             help="make a new token and WebSocket port instead of reusing the earlier link")
        command.set_defaults(fn=function)
    for name, function in (("stop", cmd_stop), ("restart", cmd_restart), ("link", cmd_link),
                           ("status", cmd_status), ("tools", cmd_tools)):
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

    args = parser.parse_args(argv)
    args.dir = os.path.expanduser(args.dir or os.environ.get("COLAB_BRIDGE_DIR")
                                  or f"~/.cache/colab-bridge/{args.port}")
    try:
        return args.fn(args)
    except client.BridgeError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
