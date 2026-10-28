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

A cell blocks the notebook while it runs, and the bridge runs one cell at a time, so a long cell makes every other cell
wait behind it: run long work as a [job](#jobs) instead.

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

A running bridge reminds you of runtimes its tab is on that ran no cell for 30 minutes and that no project claims,
again every 30 minutes until you release them (`release-runtime`), claim them for a background job, or `forget` them
once they are gone. Bridges that share the record take turns, so each reminder comes once.

When a bridge's cell runs on another runtime than its last, the notebook's cells tell why. The same notebook means it
changed runtime (Runtime > Change runtime type, or Colab gave it a new one): Colab released the old runtime, and the
record drops it without a word. A new notebook means the link was opened in another tab, and the first tab may still
hold its runtime: you hear that once, then. Either way a runtime no tab is on is not reminded about, and the record
forgets it a day after its last cell, by when Colab has ended it.

On macOS reminders are desktop notifications; anywhere, `--notify-command` (or `COLAB_BRIDGE_NOTIFY`) runs a command of
yours with the message in `COLAB_BRIDGE_MESSAGE`, for example to send it to your phone. The bridge log has them all.
`--idle-reminder MINUTES` changes the wait, and `--idle-reminder 0` turns reminders off:

```bash
colab-bridge start --idle-reminder 20 --notify-command 'curl -s -d "$COLAB_BRIDGE_MESSAGE" ntfy.sh/my-topic'
```

The bridge learns about a runtime only from the cells it runs, and never runs a cell just to look: a cell would count as
use, and keep the runtime from timing out on its own.

## Compute units

Colab bills runtimes in compute units by the hour, and a balance that runs out ends GPU runtimes. When the Colab tab is
in colab-bridge's own Chrome (`colab-bridge open`), the bridge reads the account's compute units from it every 15
minutes and warns, as above, while they would last less than a day at the current rate, at most every 6 hours.
`colab-bridge quota` reads them now:

```console
$ colab-bridge quota
890.0 compute units left, 8.9 an hour over 1 runtime: about 100 h (read 14:05)
```

`--json` gives the reading, the balance now and the hours left to scripts; `who` shows the latest reading too. Between
readings, or when the tab is elsewhere, the balance is carried forward from the latest reading at its rate and marked as
an estimate. The bridge asks for the numbers the way Colab's own Resources panel does, from inside the tab with its
sign-in, through Chrome's debugging port on 127.0.0.1 (9765, or `--debug-port` / `COLAB_BRIDGE_DEBUG_PORT`). If Colab
moves them, `quota` lists the requests the page itself made, to find where they went.

## Jobs

A job is long work started by a short cell: the file goes to the runtime and runs there in a process of its own, its
output in a log, while the notebook is free for other cells at once. Later short cells say how it does, or stop it.

```bash
colab-bridge job start ship.py --name ship --env DAY=27 --secret HF_TOKEN
colab-bridge job status ship        # running for 4 min (pid 4242), and its log's last lines
colab-bridge job status             # every job on the runtime: running, ended (with its exit code) or lost
colab-bridge job stop ship          # TERM to the job and its own processes; --kill for KILL
```

A `.sh` file runs with bash, anything else with the kernel's Python. A job gets the kernel's environment, the `--env`
values, and the notebook secrets named with `--secret`, read on the runtime and never printed (`google.colab.userdata`
only answers inside the notebook, where a detached process cannot ask). It runs in `/content` unless `--cwd` says
otherwise. One job runs under a name at a time; starting the name again once it ended replaces its folder,
`/tmp/colab-bridge/jobs/<name>` (the file, `meta.json`, `log`, and `exit.json` once it ended). When a job ends it says so
in the runtime's outbox (below): `job ship finished after 12 min; its log ends: ...`, as an alert when it failed or was
killed.

## Events from the runtime

Processes on the runtime can leave short messages for this machine, which come here without a cell of their own. The
runtime cannot reach this machine, and the bridge never runs a cell just to look; instead a process appends a line to
the runtime's outbox, `/tmp/colab-bridge/outbox.jsonl`:

```bash
echo '{"level": "alert", "text": "the trainer died (exit 1)", "source": "watch"}' >> /tmp/colab-bridge/outbox.jsonl
echo "plain text is information" >> /tmp/colab-bridge/outbox.jsonl
```

Every cell a colab-bridge client sends (`run`, `fetch`, jobs) begins with one line of code that prints what this machine
has not had of the outbox yet; the client takes those lines out of the cell's output and keeps them in the bridge's
folder, `~/.cache/colab-bridge/<port>/events.jsonl`, one JSON object a line: `{"at", "host", "level", "text",
"source", "t"}` (`t`: when the runtime said it, if the line had it). A message arrives with the next cell through the
bridge, whoever sends it.

```bash
colab-bridge events                 # the last day's
colab-bridge events --level alert --since 2h --json
```

Where this machine got to in each runtime's outbox is kept beside them (`events-cursor.json`), and only a client that
kept the lines moves it: a cell whose answer never came back leaves them for the next cell, and two cells that read the
same lines keep them once. A line still being written waits for its end. The outbox is append-only (a runtime lives a
day at most); `"level"` is yours to choose, and `"alert"` is the one meant for a person.

## Notices on your phone (Telegram)

Desktop notifications reach you only at the desk. With a Telegram bot of your own, the bridges' notices (a tab moved
away from a GPU runtime, an idle runtime, compute units running low) and the runtime's alerts reach your phone, and so
does anything a script sends with `colab-bridge notify`.

1. In Telegram, ask [@BotFather](https://t.me/BotFather) for `/newbot`; it gives the bot's token.
2. Keep the token in the macOS keychain. This asks for it without showing it, and it never lands in your shell history:

   ```bash
   security add-generic-password -s colab-bridge-telegram -a "$USER" -w
   ```

   (Elsewhere, put it in `COLAB_BRIDGE_TELEGRAM_TOKEN` for the bridges and scripts that send.)
3. Send `/start` to your bot, then run `colab-bridge telegram setup`: it reads the token, finds your chat in the bot's
   updates and sends it a first message. Only a private chat that sent `/start` counts; if two did, it asks you to try
   again.
4. Try it: `colab-bridge notify "hello from colab-bridge"`.

```bash
colab-bridge notify "the nightly copy is done"   # or: some-command | colab-bridge notify -
colab-bridge telegram status                     # where messages go, how many went this hour (never the token)
colab-bridge telegram off                        # stop sending
```

The same text goes at most once an hour (`--every MINUTES` for one call, `same_text_minutes` in the config for all),
and this machine sends at most 20 messages an hour (`per_hour`); the next message that goes says how many the cap held
back. Running bridges send their notices, and each runtime event at level `alert` that their cells brought
(`event_levels` in the config, or `"events": false` for none) once; an event that could not go is tried again 30 seconds
later. A bridge started before this version sends them once it is restarted; `colab-bridge status` says which levels a
bridge forwards (`"forwards"`).

**Asking for the status.** Give setup a command and the folder it runs in, and keep `colab-bridge telegram listen`
running (a login item, a launchd job, a tmux pane):

```bash
colab-bridge telegram setup --status-command "python3 tools/status.py" --status-dir ~/my-project
colab-bridge telegram listen
```

`/status` (or `/durum`) from your chat then runs that command and sends back what it printed. That is all the listener
does: it runs no other command, takes no word of the message into the command (no shell runs it), and answers no other
chat. One listener runs per machine; a second one exits at once.

The config is `~/.config/colab-bridge/config.json` (or `COLAB_BRIDGE_CONFIG`), readable by you alone. From Python:
`from colab_bridge import telegram; telegram.notify("text")` returns `{"sent", "skipped", "error"}` and never raises.

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

from colab_bridge import events, jobs

jobs.start(open("train.py").read(), "train", port=8765, project="demo")
print(jobs.status("train", port=8765)["jobs"][0]["state"])
print(events.read(client.default_dir(8765)))  # what the runtime's outbox said, as it came with the cells
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
- colab-bridge's own Chrome listens for Chrome's debugging protocol on 127.0.0.1, so programs of this machine's users
  can drive it, its Google sign-in included: the same trust the control socket asks for. Your everyday Chrome has no
  such port.
- The Telegram bot's token stays in the keychain: colab-bridge reads it with `security` when a message goes, and never
  prints it, logs it or passes it on a command line. Whoever holds it can speak as your bot and read what is sent to
  it; if it leaks, ask @BotFather for `/revoke`. The listener answers your chat alone, with your command's output.

## Limits

- One tab per bridge. When the tab disconnects (a runtime reset, a reload), run `colab-bridge restart` and paste the
  link again.
- Claims, runtime checks and reminders cover what goes through colab-bridge on this machine. They do not see other
  machines, or cells run by hand in the tab.
- The record is locked with `flock`, so it needs macOS or Linux.
- `fetch` moves files through cell output in 4 MB parts (a few MB/s while Chrome keeps the tab at speed), each checked
  with SHA-256: fine for results, not for datasets.
- The runtime's events come with cells: while no cell runs through a bridge, they wait in the outbox. A cell brings
  at most 200 lines of it; the rest come with the next.

## License

MIT. Uses googlecolab/colab-mcp (Apache-2.0).
