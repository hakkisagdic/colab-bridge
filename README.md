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

## Several tabs

Each tab gets its own bridge on its own control port:

```bash
colab-bridge --port 8766 start      # link, log and process id in ~/.cache/colab-bridge/8766
colab-bridge --port 8766 run cell.py
```

`--dir` (or `COLAB_BRIDGE_DIR`) moves that state elsewhere; `COLAB_BRIDGE_PORT` sets the default port.

Tabs opened from these links share one runtime and one kernel: the link always opens colab-mcp's scratch notebook
(`notebooks/empty.ipynb`), and Colab connects tabs of the same notebook to the same runtime. A second tab adds a
control channel, not a machine, and uses no extra compute units. Everything else is shared too:

- a cell running from one tab makes the other tab's cells wait, so keep cells short and run long jobs in the background;
- files, the working directory, environment variables and GPU memory are the same for both;
- releasing or restarting the runtime from one tab stops the other tab's work as well.

## From Python

```python
from colab_bridge import client

print(client.run_cell("# probe\nimport torch; print(torch.cuda.get_device_name(0))", port=8765))
client.fetch("/content/result.mp4", "result.mp4", port=8765)
```

`colab_bridge.client` uses only the standard library. The control protocol is one JSON line per connection to
`127.0.0.1:<port>`: `{"op": "status"}`, `{"op": "list"}` or `{"op": "call", "name": TOOL, "args": {...}}`, answered
with `{"ok": true, "result": ...}` or `{"ok": false, "error": "..."}`.

## Security

- The Colab link carries a token: whoever has it can run code in that runtime. Don't share it.
- The control socket listens on 127.0.0.1 without a password, so every program of this machine's users can use a
  running bridge.
- The bridge only relays the notebook's tool calls; your Google sign-in stays in the browser.

## Limits

- One tab per bridge. When the tab disconnects (a runtime reset, a reload), run `colab-bridge restart` and paste the
  link again.
- `fetch` moves files through cell output in 4 MB parts (a few MB/s), each checked with SHA-256: fine for results,
  not for datasets.

## License

MIT. Uses googlecolab/colab-mcp (Apache-2.0).
