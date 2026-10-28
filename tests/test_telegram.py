"""Telegram (colab_bridge.telegram): notices to a chat set up from a /start, sent once and at most so many an hour, the
runtime's alerts forwarded by the bridge, and a listener that answers /status from that chat alone. Against a fake Bot
API on a local port: no test reaches Telegram or this machine's keychain (conftest's no_telegram)."""

import fcntl
import io
import json
import logging
import os
import shlex
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from colab_bridge import cli, client, events, registry, server, telegram

KEYCHAIN_TOKEN = telegram.keychain_token  # before conftest's no_telegram takes it away for the test
HAS_KEYCHAIN_TOKEN = telegram.has_keychain_token
TOKEN = "123456:the-bots-secret-token"
CHAT = 42


class FakeBotApi:
    """The Bot API as Telegram answers it: getMe, getUpdates (its updates, those before the offset confirmed and gone),
    sendMessage and sendChatAction, each call kept in calls (refused ones in refused). refuse[method] is a list of
    answers to give first: (HTTP status, description, retry_after), or None for a normal answer."""

    def __init__(self):
        self.calls = []
        self.refused = []
        self.updates = []
        self.refuse = {}
        self.tokens = set()
        self.lock = threading.Lock()

    def answer(self, token, method, params):
        with self.lock:
            self.tokens.add(token)
            planned = self.refuse.get(method) or []
            refusal = planned.pop(0) if planned else None
            if refusal:
                self.refused.append((method, params))
                status, description, retry = refusal
                return status, {"ok": False, "error_code": status, "description": description,
                                **({"parameters": {"retry_after": retry}} if retry is not None else {})}
            self.calls.append((method, params))
            if method == "getMe":
                return 200, {"ok": True, "result": {"id": 7, "is_bot": True, "username": "example_test_bot"}}
            if method == "getUpdates":
                if params.get("offset"):
                    self.updates = [u for u in self.updates if u["update_id"] >= params["offset"]]
                return 200, {"ok": True, "result": list(self.updates)}
            if method == "sendMessage":
                return 200, {"ok": True, "result": {"message_id": len(self.calls), "text": params["text"]}}
            if method == "sendChatAction":
                return 200, {"ok": True, "result": True}
            return 404, {"ok": False, "error_code": 404, "description": "Not Found"}

    def sent(self, chat=None):
        return [p["text"] for m, p in self.calls if m == "sendMessage" and (chat is None or p["chat_id"] == chat)]

    def methods(self):
        return [m for m, _ in self.calls]


@pytest.fixture
def bot_api(monkeypatch):
    fake = FakeBotApi()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            _, bot, method = self.path.split("/", 2)
            params = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            status, body = fake.answer(bot[len("bot"):], method, params)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
        threading.Thread(target=http.serve_forever, daemon=True).start()
        monkeypatch.setenv(telegram.API_ENV, f"http://127.0.0.1:{http.server_address[1]}")
        monkeypatch.setenv(telegram.TOKEN_ENV, TOKEN)
        yield fake
        http.shutdown()


def set_up(**more):
    telegram.save_settings({"chat_id": CHAT, "chat": "Owner (@owner)", "bot": "@example_test_bot", **more})


def message(update_id, text, chat=CHAT, date=None, kind="private", **who):
    date = int(time.time()) if date is None else date
    return {"update_id": update_id, "message": {"message_id": update_id, "date": date,
                                                "text": text, "chat": {"id": chat, "type": kind, **who}}}


def test_a_notice_goes_to_the_chat_set_up(bot_api):
    assert telegram.notify("the learner died")["error"].startswith("Telegram is not set up")
    assert bot_api.calls == [], "nothing is asked of Telegram before it is set up"
    set_up()
    assert telegram.notify("the learner died") == {"sent": True, "skipped": None, "error": None}
    (method, params), = bot_api.calls
    assert (method, params["chat_id"], params["text"]) == ("sendMessage", CHAT, "the learner died")
    assert params["link_preview_options"] == {"is_disabled": True} and "parse_mode" not in params, "plain text"
    assert bot_api.tokens == {TOKEN}
    long = "x" * 5000
    telegram.notify(long)
    assert len(bot_api.sent()[-1]) <= telegram.MAX_TEXT, "cut to what Telegram takes"


def test_the_same_text_goes_once_an_hour_and_a_cap_holds_the_rest(bot_api):
    set_up(per_hour=3)
    assert telegram.notify("disk 91%", now=0)["sent"]
    assert telegram.notify("disk 91%", now=60) == {"sent": False, "skipped": "same text", "error": None}
    assert telegram.notify("disk 91%", every_minutes=0.5, now=61)["sent"], "a caller may allow it sooner"
    assert telegram.notify("disk 91%", now=3700)["sent"], "an hour later it goes again"
    assert [telegram.notify(t, now=3701)["sent"] for t in ("b", "c")] == [True, True]
    assert [telegram.notify(t, now=3702)["skipped"] for t in ("d", "e")] == ["hourly cap", "hourly cap"]
    assert telegram.sent_last_hour(now=3702) == {"sent": 3, "held": 2}
    assert telegram.notify("after the cap", now=7400)["sent"]
    assert bot_api.sent()[-1] == "after the cap\n\n(2 more held back by the cap of 3 messages an hour)"
    assert telegram.sent_last_hour(now=7400)["held"] == 0


def test_failures_say_why_and_never_the_token(bot_api, monkeypatch, caplog, capsys):
    set_up()
    bot_api.refuse["sendMessage"] = [(401, "Unauthorized", None)]
    result = telegram.notify("one")
    assert "Unauthorized" in result["error"] and TOKEN not in result["error"]
    assert telegram.notify("one")["sent"], "a text that did not go is not counted as sent"
    bot_api.refuse["sendMessage"] = [(429, "Too Many Requests: retry after 1", 1)]
    assert telegram.notify("two")["sent"], "a short pause Telegram asks for is waited once"
    for api in ("http://127.0.0.1:9", "no-such-scheme"):  # a closed port; a URL urllib cannot even read
        monkeypatch.setenv(telegram.API_ENV, api)
        error = telegram.notify(f"three {api}")["error"]
        assert error.startswith("Telegram could not be reached") and TOKEN not in error
    with caplog.at_level(logging.WARNING):
        server.to_telegram("a notice")
    assert "could not be reached" in caplog.text and TOKEN not in caplog.text
    assert cli.main(["notify", "four"]) == 1
    out = capsys.readouterr()
    assert "Not sent: Telegram could not be reached" in out.err and TOKEN not in out.out + out.err


def test_the_token_comes_from_the_keychain_through_security_alone(monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="from-the-keychain\n" if "-w" in argv else "attributes")

    monkeypatch.setattr(telegram.shutil, "which", lambda name: "/usr/bin/security")
    monkeypatch.setattr(telegram.subprocess, "run", run)
    monkeypatch.setattr(telegram, "keychain_token", KEYCHAIN_TOKEN)
    monkeypatch.setattr(telegram, "has_keychain_token", HAS_KEYCHAIN_TOKEN)
    assert telegram.token() == "from-the-keychain"
    assert telegram.has_keychain_token()
    assert seen == [["security", "find-generic-password", "-s", "colab-bridge-telegram", "-w"],
                    ["security", "find-generic-password", "-s", "colab-bridge-telegram"]], "asked, never given"
    monkeypatch.setattr(telegram.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 44, stdout=""))
    with pytest.raises(telegram.TelegramError, match="security add-generic-password -s colab-bridge-telegram"):
        telegram.token()


def test_setup_takes_the_chat_that_sent_start(bot_api, tmp_path):
    lines = []
    bot_api.updates = [message(1, "hello"), message(2, "/start", first_name="Ada", username="ada"),
                       message(3, "/start", chat=-100, kind="group", title="a group")]
    section = telegram.setup(wait_s=5, status_command="python3 tools/status.py", status_dir=str(tmp_path),
                             out=lines.append)
    assert "Send /start to @example_test_bot" in lines[0]
    assert telegram.settings() == section == {"chat_id": CHAT, "chat": "Ada (@ada)", "bot": "@example_test_bot",
                                              "status_command": "python3 tools/status.py",
                                              "status_dir": str(tmp_path)}
    assert bot_api.updates == [], "the updates it read are confirmed: the listener never sees them"
    assert "sends its notices to this chat" in bot_api.sent(CHAT)[0] and "/status" in bot_api.sent(CHAT)[0]
    assert stat.S_IMODE(os.stat(telegram.config_path()).st_mode) == 0o600
    telegram.setup(keep_chat=True, status_command="")
    assert telegram.settings()["chat_id"] == CHAT and telegram.settings()["status_command"] is None


def test_setup_refuses_no_start_or_several(bot_api):
    with pytest.raises(telegram.TelegramError, match="No /start reached @example_test_bot"):
        telegram.setup(wait_s=0, out=lambda line: None)
    bot_api.updates = [message(1, "/start", first_name="Ada"), message(2, "/start", chat=43, first_name="Someone")]
    with pytest.raises(telegram.TelegramError, match=r"/start came from 2 chats \(Ada; Someone\)"):
        telegram.setup(wait_s=0, out=lambda line: None)
    assert not telegram.configured() and bot_api.sent() == []
    bot_api.refuse["sendMessage"] = [(403, "Forbidden: bot was blocked by the user", None)]
    with pytest.raises(telegram.TelegramError, match="blocked"):
        telegram.setup(chat_id=CHAT)
    assert not telegram.configured(), "a chat the first message cannot reach is not kept"


def test_the_listener_answers_status_from_the_chat_set_up_and_nothing_else(bot_api, tmp_path):
    ran = tmp_path / "ran"
    command = (f"{shlex.quote(sys.executable)} -c "
               + shlex.quote("import pathlib; pathlib.Path('ran').open('a').write('x'); print('fleet ok')"))
    set_up(status_command=command, status_dir=str(tmp_path))
    bot_api.updates = [message(1, "/durum"),
                       message(2, "/status; rm -rf ~", chat=99),
                       message(3, "/stop the trainer"),
                       message(4, "/status@example_test_bot", date=int(time.time()) - 3600)]
    lines = []
    assert telegram.listen(out=lines.append, polls=1) == 0
    assert bot_api.sent(CHAT) == ["fleet ok",
                                  "Only /status (or /durum) is answered here: it sends this machine's status."]
    assert bot_api.sent(99) == [] and any("another chat: not answered" in line for line in lines)
    assert ran.read_text() == "x", "the command ran once: not for the other chat, nor for the hour-old /status"
    assert ("sendChatAction", {"chat_id": CHAT, "action": "typing"}) in bot_api.calls
    assert telegram.listen(out=lines.append, polls=1) == 0 and bot_api.updates == [], "answered updates are confirmed"


def test_one_listener_and_none_without_a_status_command(bot_api, tmp_path):
    lines = []
    set_up()
    assert telegram.listen(out=lines.append, polls=5) == 0 and bot_api.calls == []
    assert "nothing to answer" in lines[-1]
    set_up(status_command="true")
    path = os.path.join(os.path.dirname(registry.registry_path()), telegram.LISTEN_LOCK)
    with open(path, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert telegram.listening()
        assert telegram.listen(out=lines.append, polls=5) == 0 and bot_api.calls == []
        assert "answers this bot already" in lines[-1]
    assert not telegram.listening()


def test_a_bridge_forwards_the_runtimes_alerts_once(bot_api, tmp_path):
    state = str(tmp_path / "bridge")

    def post(*lines):  # lines a cell brought from the runtime's outbox, kept as the client keeps them
        cursor = events.offsets(state).get("vm", 0)
        events.record(state, [{"host": "vm", "end": cursor + i + 1, "line": line} for i, line in enumerate(lines)])

    post('{"level": "alert", "text": "before Telegram"}')
    assert telegram.forward_events(state, 8766) == 0 and not os.path.exists(os.path.join(state, telegram.CURSOR_FILE))
    set_up()
    assert telegram.forward_events(state, 8766) == 0, "the first look starts from now"
    post('{"level": "alert", "text": "the learner died (exit 1)", "source": "watch"}', "all well",
         '{"level": "alert", "text": "disk 91%"}')
    assert telegram.forward_events(state, 8766) == 2
    assert bot_api.sent() == ["the learner died (exit 1)\n— Colab runtime vm, bridge 8766, watch",
                              "disk 91%\n— Colab runtime vm, bridge 8766"]
    assert telegram.forward_events(state, 8766) == 0, "each once"
    post('{"level": "alert", "text": "one"}', '{"level": "alert", "text": "two"}')
    bot_api.refuse["sendMessage"] = [None, (502, "Bad Gateway", None)]
    assert telegram.forward_events(state, 8766) == 1
    assert telegram.forward_events(state, 8766) == 1, "the one that could not go goes next time, alone"
    assert bot_api.sent()[-2:] == ["one\n— Colab runtime vm, bridge 8766", "two\n— Colab runtime vm, bridge 8766"]
    set_up(events=False)
    post('{"level": "alert", "text": "not forwarded"}')
    assert telegram.forward_events(state, 8766) == 0 and telegram.forwarded_levels() == []


def test_a_bridge_sends_its_notices_and_says_it_forwards(bot_api, bridge):
    alert = server.alerter(8766, notify_command="true")  # not the desktop: a command that does nothing
    alert("Colab moved the tab away from a GPU runtime")
    time.sleep(0.3)
    assert bot_api.calls == [] and client.status(bridge.port)["forwards"] == []
    set_up()
    assert client.status(bridge.port)["forwards"] == ["alert"]
    alert("Colab moved the tab away from a GPU runtime")
    deadline = time.time() + 10
    while not bot_api.sent() and time.time() < deadline:
        time.sleep(0.05)
    assert bot_api.sent() == ["Colab moved the tab away from a GPU runtime\n— colab-bridge 8766"]


def test_cli(bot_api, capsys, monkeypatch):
    assert cli.main(["telegram", "status"]) == 1
    assert "not set up" in capsys.readouterr().out
    bot_api.updates = [message(1, "/start", chat=987654321, first_name="Ada")]
    assert cli.main(["telegram", "setup", "--wait", "5"]) == 0
    assert "notices go to Ada through @example_test_bot" in capsys.readouterr().out
    assert cli.main(["notify", "hello", "there"]) == 0 and capsys.readouterr().out == "Sent.\n"
    assert cli.main(["notify", "hello", "there"]) == 0
    assert capsys.readouterr().out == "Not sent: the same text went within the last 60 min.\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO("from a pipe\n"))
    assert cli.main(["notify", "--json", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["sent"] and bot_api.sent()[-1] == "from a pipe"
    assert cli.main(["telegram", "status", "--json"]) == 0
    out = capsys.readouterr().out
    state = json.loads(out)
    assert (state["configured"], state["chat"], state["sent_last_hour"], state["listening"]) == (True, "Ada", 2, False)
    assert TOKEN not in out and "987654321" not in out, "neither the token nor the chat's id"
    assert cli.main(["telegram", "off"]) == 0 and not telegram.configured()
    assert cli.main(["notify", "after off"]) == 1 and "not set up" in capsys.readouterr().err
