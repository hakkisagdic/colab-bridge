# colab-bridge

Keep a Google Colab tab connected to your machine and drive its notebook from scripts: run cells, copy files back.

colab-bridge is a small daemon around [googlecolab/colab-mcp](https://github.com/googlecolab/colab-mcp), the connection
Colab offers to local tools. colab-mcp is meant to be an agent's MCP server, and in that role:

- it lives and dies with the agent's session, and every start makes a new token, so the Colab tab has to be linked
  again each time;
- its notebook tools appear only after a tab connects, which agents that list their tools once at start never see;
- only the agent itself can call them, one model turn per call.

The bridge runs that connection on its own. Its link (token and WebSocket port) survives restarts, any script or
background job can call the notebook over a local socket, and polling a long job costs no model tokens.

## Install

```bash
uv tool install --python 3.13 git+https://github.com/hakkisagdic/colab-bridge
```

colab-mcp needs Python 3.13 or newer.

## Use

```bash
colab-bridge start          # runs the bridge in the background and prints the Colab link
```

Paste the link into a Colab tab's address bar and press Enter; don't reload an open tab instead, since Colab drops the
part after `#` once the page has loaded. Then connect a runtime (for example a GPU) in that tab.

Or let the bridge open it: `colab-bridge open` (or `colab-bridge start --open`) opens the link in a Chrome of the
bridge's own, which keeps the tab at full speed while it is hidden (see [Hidden tabs](#hidden-tabs)). Sign in to Google
in that window the first time.

```bash
colab-bridge status                           # {"connected": true, "error": null}
colab-bridge run cell.py                      # runs cell.py as a notebook cell, prints its output
colab-bridge run cell.py --env SCENE=garden   # environment variables for that cell only
colab-bridge fetch /content/result.mp4        # copies a runtime file here
colab-bridge restart                          # restarts the bridge; the link stays the same
colab-bridge stop
```

A file whose first line is a comment such as `# my probe` reuses the notebook cell with that first line instead of
adding a new one each time.

A cell blocks the notebook while it runs, so start long jobs in the background from a cell
(`subprocess.Popen(..., start_new_session=True)`, output to a log file) and check them with short cells.

## Several projects on one runtime

Tabs opened from these links share one runtime and one kernel: the link always opens colab-mcp's scratch notebook
(`notebooks/empty.ipynb`), and Colab connects tabs of the same notebook to the same runtime. A second tab adds a
control channel, not a machine, and uses no extra compute units. Everything else is shared too: a cell running from one
tab makes the other tab's cells wait, files, the working directory, environment variables and GPU memory are the same
for both, and releasing or restarting the runtime from one tab stops the other tab's work as well.

So projects that use Colab from one machine share a runtime either way, and colab-bridge helps them do it:

- **One bridge, several projects.** Every project uses the same bridge and says who it is with `--project` (or
  `COLAB_BRIDGE_PROJECT`; by default the current folder's name). The bridge runs one cell at a time, and each project's
  cells carry its name in their title (`# probe [my-project]`), so two projects never edit each other's cells.
- **One bridge per tab.** Each tab gets its own bridge on its own control port:

  ```bash
  colab-bridge --port 8766 start      # link, log and process id in ~/.cache/colab-bridge/8766
  colab-bridge --port 8766 run cell.py
  ```

  `--dir` (or `COLAB_BRIDGE_DIR`) moves that state elsewhere; `COLAB_BRIDGE_PORT` sets the default port.

In both cases the projects coordinate through one record on this machine, `~/.cache/colab-bridge/registry.json` (or
`COLAB_BRIDGE_REGISTRY`), which every bridge and client reads and writes:

```bash
colab-bridge claim --vram 40 --for 90m nightly training   # "I use 40 GB of GPU memory for 90 minutes"
colab-bridge who                                          # runtimes, the tabs on them, claims, last cells
colab-bridge release                                      # done: end my project's claims
colab-bridge release-runtime                              # release the runtime, unless another project claims it
```

Claims are advisory: nothing stops a cell from using more memory than it claimed. But a claim that does not fit next to
the others, or in the memory free right now, is refused with who holds what; `claim` again to renew one before it ends.

Colab moves a tab that sits idle to a new runtime without saying so, and the next cell then runs there: often a CPU
runtime, while the GPU runtime your job is on keeps running. The bridge guards against both halves of that:

- every cell reports which runtime ran it, and a cell sent through a bridge whose project claims are on another runtime
  is stopped before its code runs;
- when a tab moves away from a GPU runtime, the bridge tells you: that runtime may still be running and using compute
  units.

## Reminders against waste

A running bridge reminds you of runtimes that ran no cell for 30 minutes and that no project claims, again every 30
minutes until you release them (`release-runtime`), claim them for a background job, or `forget` them once they are
gone. Bridges that share the record take turns, so each reminder comes once.

On macOS reminders are desktop notifications; anywhere, `--notify-command` (or `COLAB_BRIDGE_NOTIFY`) runs a command of
yours with the message in `COLAB_BRIDGE_MESSAGE`, for example to send it to your phone. The bridge log has them all.
`--idle-reminder MINUTES` changes the wait, and `--idle-reminder 0` turns reminders off:

```bash
colab-bridge start --idle-reminder 20 --notify-command 'curl -s -d "$COLAB_BRIDGE_MESSAGE" ntfy.sh/my-topic'
```

The bridge learns about a runtime only from the cells it runs, and never runs a cell just to look: a cell would count as
use, and keep the runtime from timing out on its own.

## Hidden tabs

Chrome slows down pages it considers hidden: a background tab, a minimized window, or one covered by other windows.
After a few minutes their timers fire at most once a minute, and their renderer gets less of the CPU. A Colab tab that a
bridge drives is hidden most of the time, so its cells answer late and `fetch` falls to a few KB/s; `fetch` says so when
it happens. It may also be why Colab moves an idle tab to a new runtime.

`colab-bridge open` avoids that. It opens the link in a separate Chrome profile (`~/.cache/colab-bridge/chrome`, or
`--profile DIR`; `--chrome PATH` picks another Chrome or Chromium) started with `--disable-background-timer-throttling`,
`--disable-renderer-backgrounding`, `--disable-backgrounding-occluded-windows` and without intensive wake-up throttling.
Your everyday Chrome and its profile are left alone. Without it, keep the Colab tab visible while cells run and files
copy.

## From Python

```python
from colab_bridge import client

print(client.run_cell("# probe\nimport torch; print(torch.cuda.get_device_name(0))", port=8765, project="demo"))
client.fetch("/content/result.mp4", "result.mp4", port=8765, project="demo")
```

`colab_bridge.client` uses only the standard library. The control protocol is one JSON line per connection to
`127.0.0.1:<port>`: `{"op": "status"}`, `{"op": "list"}`, `{"op": "call", "name": TOOL, "args": {...}}` or
`{"op": "run", "code": CODE, "project": NAME}`, answered with `{"ok": true, "result": ...}` or
`{"ok": false, "error": "..."}`. `run` runs the code as a cell, one request at a time, and answers
`{"output": ..., "runtime": {"host": ..., "gpu": ...}}`; the client falls back to single `call`s with bridges from
before it.

## Security

- The Colab link carries a token: whoever has it can run code in that runtime. Don't share it.
- The control socket listens on 127.0.0.1 without a password, so every program of this machine's users can use a
  running bridge.
- The bridge only relays the notebook's tool calls; your Google sign-in stays in the browser.
- `colab-bridge open` passes the link to Chrome on its command line, where other users of this machine can read it in
  the process list. On a machine you share, paste the link into the tab instead.

## Limits

- One tab per bridge. When the tab disconnects (a runtime reset, a reload), run `colab-bridge restart` and paste the
  link again.
- Claims, runtime checks and reminders cover what goes through colab-bridge on this machine. They do not see other
  machines, or cells run by hand in the tab.
- The record is locked with `flock`, so it needs macOS or Linux.
- `fetch` moves files through cell output in 4 MB parts (a few MB/s while Chrome keeps the tab at speed), each checked
  with SHA-256: fine for results, not for datasets.

## License

MIT. Uses googlecolab/colab-mcp (Apache-2.0).
