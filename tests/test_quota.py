"""The account's compute units (colab_bridge.quota): Colab's answer, the estimate between readings, the warning, and
the reading through Chrome's debugging protocol with the network parts faked."""

import json

import pytest

from colab_bridge import browser, quota

NOW = 1_000_000.0
ANSWER = quota.XSSI + '\n{"currentBalance": 890.0, "consumptionRateHourly": 8.9, "assignmentsCount": 1}'


def test_parse_reads_colab_answer_behind_its_guard():
    reading = quota.parse(ANSWER, NOW, "2")
    assert reading == {"at": NOW, "balance": 890.0, "rate": 8.9, "assignments": 1, "tier": None, "authuser": "2"}
    # The same numbers under the names Colab's API gives them.
    api = quota.parse('{"paidComputeUnitsBalance": 10, "consumptionRateHourly": 0, "assignmentsCount": 0, '
                      '"subscriptionTier": "SUBSCRIPTION_TIER_PRO_PLUS"}', NOW)
    assert (api["balance"], api["rate"], api["tier"]) == (10.0, 0.0, "SUBSCRIPTION_TIER_PRO_PLUS")


def test_parse_falls_back_to_the_free_quota():
    reading = quota.parse('{"currentBalance": 0, "consumptionRateHourly": 1.5, '
                          '"freeCcuQuotaInfo": {"remainingTokens": "2500"}}', NOW)
    assert (reading["balance"], reading["tier"]) == (2.5, "free quota")


def test_parse_says_what_the_answer_had_instead():
    with pytest.raises(quota.QuotaError, match="only: eligibleGpus"):
        quota.parse('{"eligibleGpus": []}', NOW)


def test_estimate_and_hours_left():
    reading = quota.parse(ANSWER, NOW)
    assert quota.hours_left(reading) == pytest.approx(100)
    later = quota.estimate(reading, NOW + 2 * 3600)
    assert later["balance"] == pytest.approx(890 - 17.8) and later["at"] == NOW
    assert quota.estimate(reading, NOW + 1000 * 3600)["balance"] == 0
    assert quota.hours_left({**reading, "rate": 0}) is None


def test_describe_says_when_it_is_an_estimate():
    reading = quota.parse(ANSWER, NOW)
    assert "890.0 compute units left, 8.9 an hour over 1 runtime: about 100 h" in quota.describe(reading, NOW)
    text = quota.describe(reading, NOW + 3600, live=False)
    assert text.startswith("about 881.1 compute units left") and "estimated from 890.0" in text
    assert "none being used" in quota.describe({**reading, "rate": 0.0}, NOW)


def test_warning_comes_below_the_floor_at_most_every_six_hours():
    record = {"reading": quota.parse(ANSWER, NOW)}  # 100 h at this rate
    assert quota.warning(record, NOW, floor_h=24) is None
    record["reading"]["balance"] = 100.0  # about 11 h
    assert "about 11 h" in quota.warning(record, NOW, floor_h=24)
    assert quota.warning(record, NOW + 3600, floor_h=24) is None
    assert quota.warning(record, NOW + quota.WARN_EVERY_S + 1, floor_h=24)
    assert quota.warning({}, NOW, floor_h=24) is None


def test_the_colab_tab_is_found_among_chromes_pages():
    pages = [{"type": "page", "url": "https://example.com/"},
             {"type": "page", "url": "https://colab.research.google.com/drive/abc?authuser=1"},
             {"type": "page", "url": "https://colab.research.google.com/notebooks/empty.ipynb?authuser=2"
                                     "#mcpProxyToken=t&mcpProxyPort=1"},
             {"type": "service_worker", "url": "https://colab.research.google.com/sw.js"}]
    page = quota.colab_page(pages)
    assert "mcpProxyToken" in page["url"] and quota.authuser(page["url"]) == "2"
    assert quota.colab_page(pages[:1]) is None
    assert quota.authuser("https://colab.research.google.com/notebooks/empty.ipynb") == "0"


def test_read_asks_the_tab_with_its_account(monkeypatch):
    asked = []

    def evaluate(ws_url, expression, timeout=30):
        asked.append(expression)
        return {"status": 200, "text": ANSWER}

    monkeypatch.setattr(quota, "targets", lambda port: [
        {"type": "page", "url": "https://colab.research.google.com/x?authuser=2", "webSocketDebuggerUrl": "ws://t"}])
    monkeypatch.setattr(quota, "evaluate", evaluate)
    reading = quota.read(9765, now=NOW)
    assert reading["balance"] == 890.0 and reading["authuser"] == "2"
    assert json.dumps(f"{quota.COLAB}{quota.CCU_PATH}?authuser=2") in asked[0]


def test_read_points_to_what_the_page_asked_when_the_answer_moved(monkeypatch):
    answers = iter([{"status": 404, "text": "Not Found"}, ["https://colab.research.google.com/tun/m/new-ccu"]])
    monkeypatch.setattr(quota, "targets", lambda port: [
        {"type": "page", "url": "https://colab.research.google.com/x", "webSocketDebuggerUrl": "ws://t"}])
    monkeypatch.setattr(quota, "evaluate", lambda ws_url, expression, timeout=30: next(answers))
    with pytest.raises(quota.QuotaError, match="answered 404.*The page itself asked: .*/tun/m/new-ccu"):
        quota.read(9765)


def test_read_without_a_colab_tab(monkeypatch):
    monkeypatch.setattr(quota, "targets", lambda port: [{"type": "page", "url": "chrome://newtab/"}])
    with pytest.raises(quota.QuotaError, match="no Colab tab"):
        quota.read(9765)


def test_the_bridges_chrome_listens_for_debugging():
    command = browser.chrome_command("chrome", "/p", "https://colab.research.google.com/x", 9777)
    assert "--remote-debugging-port=9777" in command and command[-1] == "https://colab.research.google.com/x"
