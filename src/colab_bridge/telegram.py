"""
Telegram: the bridge's notices, the runtime's alerts and any script's messages on your phone, through a Telegram bot of
your own. Standard library only.

  colab-bridge telegram setup     once: the bot's token from the macOS keychain, your chat from the /start you send it
  colab-bridge notify TEXT        a message to that chat (telegram.notify(text) from Python)
  colab-bridge telegram listen    answers /status (or /durum) from that chat with a command of yours, and nothing else

The token is the bot's password. It stays in the macOS keychain (a generic password, service "colab-bridge-telegram"; on
other systems the COLAB_BRIDGE_TELEGRAM_TOKEN environment variable), is read when a message goes, and is never printed,
logged or put on a command line. The chat it talks to (its id and the name it showed) is kept in colab-bridge's config,
~/.config/colab-bridge/config.json (or COLAB_BRIDGE_CONFIG), under "telegram". Messages go through the Bot API's
sendMessage over HTTPS.

So that a loop gone wrong cannot flood the phone, the same text goes at most once an hour (same_text_minutes in the
config) and this machine sends at most 20 messages an hour (per_hour); the next message that goes says how many the cap
held back. The record of what went (telegram-sent.json, beside the registry) is shared by every bridge and script of
this machine, under a lock.

A running bridge forwards what the runtime's processes leave in its outbox at level "alert" (event_levels in the config;
colab_bridge.events), each once, and its own notices. The listener runs one configured command (status_command, in
status_dir) for messages from the stored chat alone: it never takes words from the message, so it can answer, never act.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

from colab_bridge import events, registry

API = "https://api.telegram.org"
API_ENV = "COLAB_BRIDGE_TELEGRAM_API"  # another Bot API server (the tests' fake one)
TOKEN_ENV = "COLAB_BRIDGE_TELEGRAM_TOKEN"
KEYCHAIN_SERVICE = "colab-bridge-telegram"
CONFIG_ENV = "COLAB_BRIDGE_CONFIG"
SENT_FILE = "telegram-sent.json"
LISTEN_LOCK = "telegram-listen.lock"
CURSOR_FILE = "telegram-cursor.json"  # in a bridge's folder: where its forwarding got to in events.jsonl
MAX_TEXT = 4000  # Telegram takes 4096 characters a message, counted in UTF-16
SAME_TEXT_MINUTES = 60
PER_HOUR = 20
EVENT_LEVELS = ["alert"]
STATUS_WORDS = ("/status", "/durum")
STATUS_TIMEOUT_S = 600
ANSWER_WITHIN_S = 600  # a /status older than this (sent while nothing listened) is not answered
SETUP_WAIT_S = 120
STORE_HINT = f'security add-generic-password -s {KEYCHAIN_SERVICE} -a "$USER" -w'


class TelegramError(RuntimeError):
    def __init__(self, message: str, retry_after: float = None):
        super().__init__(message)
        self.retry_after = retry_after


def config_path() -> str:
    return os.path.expanduser(os.environ.get(CONFIG_ENV) or "~/.config/colab-bridge/config.json")


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path: str, data, private: bool = False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(temporary, path)


def settings() -> dict:
    """The "telegram" section of colab-bridge's config: {} until Telegram is set up."""
    section = _read_json(config_path(), {}).get("telegram")
    return section if isinstance(section, dict) else {}


def save_settings(section: dict):
    """Writes the "telegram" section (none: removes it), the rest of the config as it was; readable by you alone."""
    data = _read_json(config_path(), {})
    if section:
        data["telegram"] = section
    else:
        data.pop("telegram", None)
    _write_json(config_path(), data, private=True)


def configured(cfg: dict = None) -> bool:
    cfg = settings() if cfg is None else cfg
    return isinstance(cfg.get("chat_id"), int) and not isinstance(cfg.get("chat_id"), bool)


def forwarded_levels(cfg: dict = None) -> list:
    """The levels of the runtime's events a bridge sends to Telegram: none until it is set up, or with "events": false
    in the config."""
    cfg = settings() if cfg is None else cfg
    if not configured(cfg) or cfg.get("events") is False:
        return []
    levels = cfg.get("event_levels")
    return [str(level) for level in levels] if isinstance(levels, list) else list(EVENT_LEVELS)


def keychain_token():
    """The bot's token from the macOS keychain, or None (another system, or no such item). Only `security` reads it."""
    if not shutil.which("security"):
        return None
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"], capture_output=True,
                             text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def has_keychain_token() -> bool:
    """Whether the keychain holds the token, asked without reading it."""
    if not shutil.which("security"):
        return False
    try:
        return subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE], capture_output=True,
                              timeout=30, stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def token() -> str:
    found = os.environ.get(TOKEN_ENV) or keychain_token()
    if not found:
        raise TelegramError(f"No bot token: store it in the keychain with `{STORE_HINT}` (it asks for the token "
                            f"without showing it), or set {TOKEN_ENV} where the keychain is not there.")
    return found


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "<token>") if secret else text


def call(method: str, params: dict = None, secret: str = None, timeout: float = 30):
    """One Bot API method's result. Raises TelegramError, whose message never holds the token."""
    secret = secret or token()
    try:
        request = urllib.request.Request(f"{(os.environ.get(API_ENV) or API).rstrip('/')}/bot{secret}/{method}",
                                         data=json.dumps(params or {}).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                answer = json.loads(response.read())
        except urllib.error.HTTPError as e:  # the Bot API says why in the body, as JSON
            try:
                answer = json.loads(e.read())
            except ValueError:
                answer = {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001  an error's URL holds the token: the kind, scrubbed, and no chain
        raise TelegramError(_scrub(f"Telegram could not be reached ({type(e).__name__}: {e})", secret)) from None
    if not isinstance(answer, dict) or not answer.get("ok"):
        answer = answer if isinstance(answer, dict) else {}
        retry = (answer.get("parameters") or {}).get("retry_after")
        why = answer.get("description") or "no reason given"
        raise TelegramError(_scrub(f"Telegram refused {method}: {why}", secret),
                            retry if isinstance(retry, (int, float)) else None)
    return answer.get("result")


def send_message(text: str, chat_id: int, secret: str = None):
    """Sends text (cut to what Telegram takes) to the chat as plain text; waits once when Telegram asks for a pause of a
    few seconds."""
    text = text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"
    params = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
    try:
        return call("sendMessage", params, secret)
    except TelegramError as e:
        if e.retry_after is None or e.retry_after > 10:
            raise
        time.sleep(e.retry_after)
        return call("sendMessage", params, secret)


def _read_record(path: str) -> dict:
    data = _read_json(path, {})
    sent = [s for s in data.get("sent") or [] if isinstance(s, list) and len(s) == 2 and isinstance(s[0], (int, float))]
    held = data.get("held")
    return {"sent": sent, "held": held if isinstance(held, int) and held > 0 else 0}


def _record_path() -> str:
    return os.path.join(os.path.dirname(registry.registry_path()), SENT_FILE)


@contextlib.contextmanager
def _record():
    """What this machine sent in the last hour and how many messages the cap held back, under a lock; written back."""
    path = _record_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            record = _read_record(path)
            yield record
            _write_json(path, record)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def notify(text: str, every_minutes: float = None, now: float = None) -> dict:
    """Sends text to the chat set up, unless the same text went within every_minutes (default: the config's
    same_text_minutes, 60) or this machine's messages of the last hour reached the config's per_hour (20). Returns
    {"sent": bool, "skipped": None, "same text" or "hourly cap", "error": None or why it could not go}; never raises."""
    cfg = settings()
    if not configured(cfg):
        return {"sent": False, "skipped": None, "error": "Telegram is not set up: run `colab-bridge telegram setup`"}
    text = str(text).strip()
    if not text:
        return {"sent": False, "skipped": None, "error": "nothing to send"}
    now = time.time() if now is None else now
    window = 60 * float(cfg.get("same_text_minutes", SAME_TEXT_MINUTES) if every_minutes is None else every_minutes)
    cap = int(cfg.get("per_hour", PER_HOUR))
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    try:
        with _record() as record:
            record["sent"] = [s for s in record["sent"] if now - s[0] < max(window, 3600)]
            if any(d == digest and now - t < window for t, d in record["sent"]):
                return {"sent": False, "skipped": "same text", "error": None}
            if sum(now - t < 3600 for t, _ in record["sent"]) >= cap:
                record["held"] += 1
                return {"sent": False, "skipped": "hourly cap", "error": None}
            held = record["held"]
            try:
                note = f"\n\n({held} more held back by the cap of {cap} messages an hour)" if held else ""
                send_message(text + note, cfg["chat_id"])
            except TelegramError as e:
                return {"sent": False, "skipped": None, "error": str(e)}
            record["sent"].append([now, digest])
            record["held"] = 0
            return {"sent": True, "skipped": None, "error": None}
    except OSError as e:
        return {"sent": False, "skipped": None, "error": f"the record of sent messages failed: {e}"}


def sent_last_hour(now: float = None) -> dict:
    """How many messages went in the hour before now, and how many the cap holds back (read only)."""
    now = time.time() if now is None else now
    record = _read_record(_record_path())
    return {"sent": sum(now - t < 3600 for t, _ in record["sent"]), "held": record["held"]}


def describe_event(event: dict, port) -> str:
    where = f"Colab runtime {event.get('host') or '?'}, bridge {port}"
    return f"{event.get('text')}\n— {where}" + (f", {event['source']}" if event.get("source") else "")


def forward_events(state_dir: str, port, now: float = None) -> int:
    """Sends the runtime's events of the forwarded levels (forwarded_levels) that the bridge's folder got since the last
    call, each once, and returns how many went. Where it got to is kept in the folder (telegram-cursor.json: the newest
    event's time and how many events of that time are done, since a cell's events share one): an event moves it once it
    went or was skipped on purpose (the same text, the cap); one that could not go waits for the next call, and so do
    those after it. The first call starts from now: what came before Telegram was set up is not sent."""
    levels = forwarded_levels()
    if not levels:
        return 0
    now = time.time() if now is None else now
    path = os.path.join(state_dir, CURSOR_FILE)
    cursor = _read_json(path, {})
    with events._locked(state_dir):  # a cell's events are written whole under this lock: never half of them
        kept = [e for e in events.read(state_dir) if isinstance(e.get("at"), (int, float))]
    if not isinstance(cursor.get("at"), (int, float)):
        _write_json(path, {"at": max([now] + [e["at"] for e in kept]), "n": 0})
        return 0
    at, done = float(cursor["at"]), int(cursor.get("n") or 0)
    waiting, same = [], 0
    for e in kept:
        if e["at"] < at:
            continue
        if e["at"] == at:
            same += 1
            if same <= done:
                continue
        waiting.append(e)
    sent = 0
    for e in waiting:
        if e.get("level") in levels:
            result = notify(describe_event(e, port))
            if result["error"]:
                break  # Telegram cannot be reached: this one and the later ones next time
            sent += result["sent"]
        at, done = (at, done + 1) if e["at"] == at else (e["at"], 1)
    if waiting:
        _write_json(path, {"at": at, "n": done})
    return sent


def describe_chat(chat: dict) -> str:
    name = " ".join(str(chat[k]) for k in ("first_name", "last_name") if chat.get(k)) or chat.get("title") or "a chat"
    return name + (f" (@{chat['username']})" if chat.get("username") else "")


def setup(chat_id: int = None, keep_chat: bool = False, wait_s: float = SETUP_WAIT_S, status_command: str = None,
          status_dir: str = None, out=print) -> dict:
    """Sets Telegram up: the token from the keychain, the bot's name (getMe), and the chat: the one that sent /start to
    the bot within wait_s (a private chat; the updates read are confirmed), the stored one with keep_chat, or chat_id.
    Sends that chat a first message, then keeps it in the config with the status command (status_command, run in
    status_dir; "" removes it). Raises TelegramError."""
    secret = token()
    bot = "@" + str((call("getMe", secret=secret) or {}).get("username") or "your bot")
    before = settings()
    if keep_chat:
        if not configured(before):
            raise TelegramError("No chat is set up yet to keep: run `colab-bridge telegram setup` and send /start.")
        chat_id, name = before["chat_id"], before.get("chat") or "the chat set up before"
    elif chat_id is not None:
        name = before.get("chat") if before.get("chat_id") == chat_id else "the chat given"
    else:
        out(f"Send /start to {bot} in Telegram now; waiting for it up to {wait_s:.0f} s.")
        chats, offset, deadline = {}, None, time.monotonic() + wait_s
        while True:
            wait = max(0, min(30, int(deadline - time.monotonic())))
            asked = time.monotonic()
            params = {"timeout": wait, "allowed_updates": ["message"], **({"offset": offset} if offset else {})}
            for update in call("getUpdates", params, secret, timeout=wait + 30) or []:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                if str(message.get("text") or "").startswith("/start") and chat.get("type") == "private":
                    chats[chat["id"]] = describe_chat(chat)
            if chats or time.monotonic() >= deadline:
                break
            if time.monotonic() - asked < 1:
                time.sleep(1)  # a server that does not hold the request open
        if offset:
            call("getUpdates", {"offset": offset, "timeout": 0}, secret)  # confirmed: the listener does not see them
        if not chats:
            raise TelegramError(f"No /start reached {bot} in {wait_s:.0f} s: send it to the bot, then run setup again.")
        if len(chats) > 1:
            names = "; ".join(chats.values())
            raise TelegramError(f"/start came from {len(chats)} chats ({names}): make sure only yours sends it, then "
                                "run setup again.")
        (chat_id, name), = chats.items()
    section = {k: v for k, v in before.items() if k not in ("chat_id", "chat", "bot")}
    section.update(chat_id=chat_id, chat=name, bot=bot)
    if status_command is not None:
        section["status_command"] = status_command.strip() or None
    if status_dir is not None:
        section["status_dir"] = os.path.abspath(os.path.expanduser(status_dir)) if status_dir else None
    ask = " Ask /status (or /durum) for the status." if section.get("status_command") else ""
    send_message(f"colab-bridge on {socket.gethostname()} sends its notices to this chat.{ask}", chat_id, secret)
    save_settings(section)
    return section


def off() -> bool:
    """Telegram off: the chat and the status command leave the config (the token stays in the keychain)."""
    was = configured()
    save_settings({})
    return was


def listening() -> bool:
    """Whether a `colab-bridge telegram listen` runs on this machine (it holds the listener's lock)."""
    path = os.path.join(os.path.dirname(registry.registry_path()), LISTEN_LOCK)
    try:
        with open(path) as lock:  # no lock file: no listener ever ran
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def run_status(cfg: dict, typing=None) -> str:
    """The status command's output (its first MAX_TEXT characters), or why there is none. It runs without a shell, in
    status_dir, with nothing of the message; typing() is called every few seconds while it runs."""
    command = cfg.get("status_command")
    try:
        process = subprocess.Popen(shlex.split(command), cwd=cfg.get("status_dir") or None, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    except (OSError, ValueError) as e:
        return f"The status command could not start ({type(e).__name__}: {e})."
    deadline = time.monotonic() + STATUS_TIMEOUT_S
    while True:
        try:
            output, _ = process.communicate(timeout=4)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() > deadline:
                process.kill()
                output, _ = process.communicate()
                output = f"{output}\n(the status command did not end in {STATUS_TIMEOUT_S} s and was stopped)"
                break
            if typing:
                typing()
    output = output.strip() or "(the status command printed nothing)"
    if process.returncode not in (0, None, -9):
        output += f"\n(exit {process.returncode})"
    return output if len(output) <= MAX_TEXT else output[:MAX_TEXT - 20] + "\n… (cut)"


def answer(message: dict, cfg: dict, secret: str, now: float, out=print):
    """One message to the bot: from the chat set up, /status (or /durum) runs the status command and its output goes
    back; anything else from that chat gets one line on what is answered; from any other chat, nothing at all."""
    chat = (message.get("chat") or {}).get("id")
    if chat != cfg["chat_id"]:
        out(f"{registry.clock(now)} a message from another chat: not answered")
        return
    if now - (message.get("date") or 0) > ANSWER_WITHIN_S:
        return  # sent while nothing listened: its moment is gone
    words = str(message.get("text") or "").split()
    word = words[0].split("@")[0].lower() if words else ""
    if word not in STATUS_WORDS:
        send_message("Only /status (or /durum) is answered here: it sends this machine's status.", chat, secret)
        return
    out(f"{registry.clock(now)} {word}: running the status command")

    def typing():
        with contextlib.suppress(TelegramError):
            call("sendChatAction", {"chat_id": chat, "action": "typing"}, secret)

    typing()
    send_message(run_status(cfg, typing), chat, secret)


def listen(out=print, polls: int = None) -> int:
    """Answers the bot's messages until Telegram is turned off or loses its status command (each poll reads the config
    again): long polls of getUpdates, one message at a time (answer). One listener a machine: another one exits at once.
    polls: stop after that many (the tests)."""
    folder = os.path.dirname(registry.registry_path())
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, LISTEN_LOCK), "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            out("Another `colab-bridge telegram listen` answers this bot already.")
            return 0
        place = {"offset": None}
        try:
            _listen(out, polls, place)
        finally:
            if place["offset"]:  # the messages answered are confirmed: the next listener does not answer them again
                with contextlib.suppress(Exception):
                    call("getUpdates", {"offset": place["offset"], "timeout": 0, "limit": 1})
    return 0


def _listen(out, polls, place):
    failures = 0
    while polls is None or polls > 0:
        polls = None if polls is None else polls - 1
        cfg = settings()
        if not configured(cfg) or not cfg.get("status_command"):
            out("Telegram is not set up with a status command (colab-bridge telegram setup --status-command ...): "
                "nothing to answer.")
            break
        try:
            secret = token()
            wait = 50 if polls is None else 0
            offset = {"offset": place["offset"]} if place["offset"] else {}
            updates = call("getUpdates", {"timeout": wait, "allowed_updates": ["message"], **offset}, secret,
                           timeout=wait + 30) or []
            failures = 0
        except TelegramError as e:
            failures += 1
            out(f"{registry.clock(time.time())} {e}")
            time.sleep(min(300, 5 * 2 ** min(failures, 6)) if polls is None else 0)
            continue
        for update in updates:
            place["offset"] = update["update_id"] + 1
            if update.get("message"):
                try:
                    answer(update["message"], cfg, secret, time.time(), out)
                except TelegramError as e:
                    out(f"{registry.clock(time.time())} the answer did not go: {e}")
