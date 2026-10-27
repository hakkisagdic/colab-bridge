"""
Colab's compute units, read from the Colab tab itself.

Colab bills a runtime in compute units (CU) by the hour, at a rate set by its machine, and a paid balance that runs out
ends GPU runtimes. Only Colab's own pages show the balance, to the person signed in, so the bridge reads it the way the
page does: colab-bridge's own Chrome (`colab-bridge open`) starts with a debugging port on 127.0.0.1, and through it the
bridge has the Colab tab fetch Colab's compute-unit answer with the tab's own sign-in. Only the numbers are kept.

That debugging port lets programs of this machine's users drive that Chrome, its Google sign-in included: the trust the
bridge's control socket already asks for, since any of them can run code in the runtime through it.

A reading: {"at", "balance", "rate", "assignments", "tier", "authuser"}, balance in CU and rate in CU an hour over every
runtime the account has. Between readings the balance is estimated as balance - rate x hours since: the rate changes
when runtimes come and go, so an estimate says so.
"""

import json
import os
import time
import urllib.parse
import urllib.request

DEBUG_PORT_ENV = "COLAB_BRIDGE_DEBUG_PORT"
DEFAULT_DEBUG_PORT = 9765
COLAB = "https://colab.research.google.com"
CCU_PATH = "/tun/m/ccu-info"  # what Colab's Resources panel asks for: balance, hourly rate, assigned runtimes
XSSI = ")]}'"  # Google's guard in front of JSON answers
WARN_EVERY_S = 6 * 3600
FETCH = """(async () => {
  const r = await fetch(%s, {credentials: "include"});
  return {status: r.status, text: await r.text()};
})()"""
# When the answer is not what parse() expects, the requests the page itself made point to where the numbers moved.
SEEN = """performance.getEntriesByType("resource").map(e => e.name)
  .filter(u => /ccu|quota|consumption|user-info/i.test(u)).slice(-5)"""


class QuotaError(RuntimeError):
    pass


def debug_port() -> int:
    return int(os.environ.get(DEBUG_PORT_ENV) or DEFAULT_DEBUG_PORT)


def colab_page(targets: list):
    """The Colab tab among Chrome's debugging targets, a bridge link's tab first; None when there is none."""
    pages = [t for t in targets if t.get("type") == "page" and (t.get("url") or "").startswith(COLAB + "/")]
    pages.sort(key=lambda t: "mcpProxyToken=" not in t["url"])
    return pages[0] if pages else None


def authuser(url: str) -> str:
    """Which signed-in account the page uses (Colab's authuser); "0", the first, when the page does not say."""
    return (urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("authuser") or ["0"])[0]


def parse(text: str, now: float, user: str = "0") -> dict:
    """A reading from Colab's answer. An account with no paid balance left runs on its free quota, in milli-CU."""
    body = text.strip()
    data = json.loads(body[len(XSSI):] if body.startswith(XSSI) else body)
    balance = data.get("currentBalance", data.get("paidComputeUnitsBalance"))
    rate = data.get("consumptionRateHourly")
    if balance is None or rate is None:
        raise QuotaError(f"Colab's answer has no balance or rate, only: {', '.join(sorted(data)) or 'nothing'}")
    free = (data.get("freeCcuQuotaInfo") or {}).get("remainingTokens")
    tier = data.get("subscriptionTier")
    if not balance and free is not None:
        balance, tier = int(free) / 1000, "free quota"
    return {"at": now, "balance": float(balance), "rate": float(rate),
            "assignments": int(data.get("assignmentsCount") or 0), "tier": tier, "authuser": user}


def hours_left(reading: dict):
    """How long the balance lasts at the reading's rate; None when nothing is being spent."""
    return reading["balance"] / reading["rate"] if reading.get("rate") else None


def estimate(reading: dict, now: float) -> dict:
    """The reading carried forward to now at its rate."""
    spent = reading["rate"] * max(now - reading["at"], 0) / 3600
    return {**reading, "balance": max(reading["balance"] - spent, 0.0)}


def describe(reading: dict, now: float, live: bool = True) -> str:
    current = reading if live else estimate(reading, now)
    when = time.strftime("%H:%M", time.localtime(reading["at"]))
    left = hours_left(current)
    use = (f"{current['rate']:g} an hour over {current['assignments']} runtime{'s' * (current['assignments'] != 1)}: "
           f"about {left:.0f} h" if left is not None else "none being used")
    if live:
        return f"{current['balance']:.1f} compute units left, {use} (read {when})"
    return (f"about {current['balance']:.1f} compute units left, {use} (estimated from {reading['balance']:.1f} read "
            f"at {when}; the rate may have changed since)")


def warning(record: dict, now: float, floor_h: float, every_s: float = WARN_EVERY_S):
    """The warning due for the registry's quota record, at most one every every_s, while the balance would last less
    than floor_h at the latest rate; None otherwise. Records when it warned."""
    reading = record.get("reading")
    if not reading:
        return None
    current = estimate(reading, now)
    left = hours_left(current)
    if left is None or left >= floor_h or now - (record.get("warned") or 0) < every_s:
        return None
    record["warned"] = now
    return (f"Colab compute units run low: about {current['balance']:.0f} left at {current['rate']:g} an hour, so "
            f"about {left:.0f} h. Release runtimes you no longer need (colab-bridge release-runtime), or add units.")


def targets(port: int) -> list:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as answer:
            return json.load(answer)
    except (OSError, ValueError) as e:
        raise QuotaError(f"colab-bridge's Chrome does not answer on 127.0.0.1:{port} ({e}). Open the Colab tab with "
                         "`colab-bridge open` (a Chrome of the bridge's that is already running keeps the switches it "
                         "started with: quit it first)") from e


def evaluate(ws_url: str, expression: str, timeout: float = 30):
    """The value of an expression (awaited when it is a promise) evaluated in a page, over Chrome's DevTools protocol."""
    from websockets.sync.client import connect

    try:
        with connect(ws_url, max_size=16 * 1024 * 1024, open_timeout=10) as ws:
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": expression, "awaitPromise": True, "returnByValue": True}}))
            deadline = time.time() + timeout
            while True:
                message = json.loads(ws.recv(timeout=max(deadline - time.time(), 0.1)))
                if message.get("id") == 1:
                    break
    except (OSError, TimeoutError) as e:
        raise QuotaError(f"the Colab tab did not answer over Chrome's debugging port: {e}") from e
    if "error" in message:
        raise QuotaError(f"Chrome refused to evaluate in the Colab tab: {message['error'].get('message')}")
    result = message["result"]
    if result.get("exceptionDetails"):
        details = result["exceptionDetails"]
        raise QuotaError("the Colab tab could not fetch: "
                         + ((details.get("exception") or {}).get("description") or details.get("text") or "?"))
    return result["result"].get("value")


def read(port: int = None, user: str = None, now: float = None) -> dict:
    """A fresh reading from the Colab tab in colab-bridge's Chrome. user overrides the page's authuser. Raises
    QuotaError."""
    page = colab_page(targets(port or debug_port()))
    if page is None:
        raise QuotaError("colab-bridge's Chrome has no Colab tab: open the bridge's link there with `colab-bridge open`")
    user = user or authuser(page["url"])
    answer = evaluate(page["webSocketDebuggerUrl"], FETCH % json.dumps(f"{COLAB}{CCU_PATH}?authuser={user}")) or {}
    try:
        if answer.get("status") != 200:
            raise QuotaError(f"Colab answered {answer.get('status')} for the compute units: "
                             f"{(answer.get('text') or '').strip()[:200]}")
        return parse(answer["text"], time.time() if now is None else now, user)
    except (QuotaError, ValueError) as e:
        try:
            seen = evaluate(page["webSocketDebuggerUrl"], SEEN) or []
        except QuotaError:
            seen = []
        hint = f" The page itself asked: {', '.join(seen)}" if seen else ""
        raise QuotaError(f"{e}.{hint}") from e
