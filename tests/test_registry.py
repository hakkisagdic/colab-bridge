"""The shared record of runtimes, bridges and claims (colab_bridge.registry), as plain data."""

import threading

import pytest

from colab_bridge import registry

NOW = 1_000_000.0


def empty():
    return {"claims": [], "bridges": {}, "hosts": {}}


def test_gpu_memory_claims_must_fit():
    data = empty()
    registry.claim(data, "alpha", "h", 8765, NOW, 3600, vram_gb=60, note="training", gpu="G", total_gb=95, free_gb=90)
    with pytest.raises(registry.ClaimError, match="alpha holds 60 GB until .* \\(training\\)"):
        registry.claim(data, "beta", "h", 8766, NOW, 600, vram_gb=40, gpu="G", total_gb=95, free_gb=90)
    registry.claim(data, "beta", "h", 8766, NOW, 600, vram_gb=30, gpu="G", total_gb=95, free_gb=90)
    # A new claim must fit what is free now; a renewal need not, its own job already uses that memory.
    with pytest.raises(registry.ClaimError, match="free right now"):
        registry.claim(data, "gamma", "h", 8765, NOW, 600, vram_gb=4, gpu="G", total_gb=95, free_gb=2)
    registry.claim(data, "alpha", "h", 8765, NOW + 60, 3600, vram_gb=60, gpu="G", total_gb=95, free_gb=5)
    assert [c["project"] for c in data["claims"]] == ["beta", "alpha"]
    assert data["claims"][1]["since"] == NOW, "a renewal keeps when the claim began"
    with pytest.raises(registry.ClaimError, match="no GPU"):
        registry.claim(data, "alpha", "cpu-host", 8765, NOW, 600, vram_gb=1)


def test_claims_end():
    data = empty()
    registry.claim(data, "alpha", "h", 8765, NOW, 600)
    assert registry.active(data["claims"], NOW + 599) and not registry.active(data["claims"], NOW + 601)
    registry.claim(data, "beta", "h", 8765, NOW, 600)
    assert [c["project"] for c in registry.release(data, "alpha")] == ["alpha"]
    assert [c["project"] for c in data["claims"]] == ["beta"]


def test_expected_host_is_the_latest_claim_through_the_bridge():
    data = empty()
    assert registry.expected_host(data, 8765, NOW) is None
    registry.claim(data, "alpha", "old", 8765, NOW, 600)
    registry.claim(data, "beta", "new", 8765, NOW + 10, 600)
    registry.claim(data, "gamma", "other", 8766, NOW + 20, 600)
    assert registry.expected_host(data, 8765, NOW + 30) == "new"
    assert registry.expected_host(data, 8765, NOW + 700) is None


def test_moving_off_a_gpu_runtime_is_noticed():
    data = empty()
    assert registry.note_run(data, 8765, "alpha", {"host": "gpu", "gpu": "G"}, NOW) is None
    assert registry.note_run(data, 8765, "alpha", {"host": "cpu", "gpu": None}, NOW + 1) == {"host": "gpu", "gpu": "G"}
    assert registry.note_run(data, 8765, "alpha", {"host": "gpu", "gpu": "G"}, NOW + 2) is None  # back from no GPU
    assert set(data["hosts"]) == {"gpu", "cpu"}, "the runtime left behind stays known"


def test_idle_runtimes_are_reminded_once_per_interval():
    data = empty()
    registry.note_run(data, 8765, "alpha", {"host": "h", "gpu": "G"}, NOW)
    registry.note_run(data, 8766, "beta", {"host": "h", "gpu": "G"}, NOW + 60)
    assert registry.idle_reminders(data, NOW + 1000, 1800) == []
    messages = registry.idle_reminders(data, NOW + 1900, 1800)
    assert len(messages) == 1, "one reminder for a runtime, however many bridges it has"
    assert "h (G) has run no cell for 31 min" in messages[0] and "--port 8765 release-runtime" in messages[0]
    assert registry.idle_reminders(data, NOW + 2000, 1800) == []
    assert len(registry.idle_reminders(data, NOW + 3800, 1800)) == 1


def test_claims_hold_off_reminders():
    data = empty()
    registry.note_run(data, 8765, "alpha", {"host": "h", "gpu": "G"}, NOW)
    registry.claim(data, "alpha", "h", 8765, NOW, 3600, note="background job")
    assert registry.idle_reminders(data, NOW + 3000, 1800) == []
    assert registry.idle_reminders(data, NOW + 3600 + 1000, 1800) == [], "idle counts from the claim's end"
    assert len(registry.idle_reminders(data, NOW + 3600 + 1900, 1800)) == 1


def test_a_notebook_that_changed_runtime_forgets_the_old_one():
    # Runtime > Change runtime type in the bridge's tab: the same notebook (its cells) on a new runtime. Colab released
    # the old one, so nothing is said about it and it leaves the record.
    data = empty()
    registry.note_run(data, 8765, "alpha", {"host": "h100", "gpu": "H100"}, NOW, cells=["a", "b"])
    registry.claim(data, "alpha", "h100", 8765, NOW, 600)
    assert registry.note_run(data, 8765, "alpha", {"host": "g4", "gpu": "RTX"}, NOW + 60, cells=["a", "b", "c"]) is None
    assert set(data["hosts"]) == {"g4"} and data["claims"] == []
    assert data["bridges"]["8765"]["cells"] == ["a", "b", "c"]


def test_a_runtime_left_in_another_tab_is_noticed_once_then_forgotten_after_a_day():
    # The link opened in another tab: a new notebook, so the first tab may still hold its runtime. That is said once,
    # when it happens (note_run's answer), never reminded about, and the record lets it go a day after its last cell.
    data = empty()
    registry.note_run(data, 8765, "alpha", {"host": "h100", "gpu": "H100"}, NOW, cells=["a"])
    left = registry.note_run(data, 8765, "alpha", {"host": "g4", "gpu": "RTX"}, NOW + 60, cells=["x"])
    assert left == {"host": "h100", "gpu": "H100"}
    # 2026-10-19: an H100 the tab had left 14.5 h before was reminded about every half hour, though it was long gone.
    for hours in (0.5, 1, 14.5, 23.9):
        assert all("h100" not in m for m in registry.idle_reminders(data, NOW + 60 + hours * 3600, 1800))
    assert "h100" in data["hosts"]
    registry.idle_reminders(data, NOW + 24 * 3600, 1800)
    assert "h100" not in data["hosts"] and "g4" in data["hosts"]


def test_runs_without_cell_ids_keep_the_old_notice():
    # A client that does not say which cells the notebook holds (or a first run) cannot tell a notebook from another.
    data = empty()
    registry.note_run(data, 8765, "alpha", {"host": "h100", "gpu": "H100"}, NOW, cells=["a"])
    assert registry.note_run(data, 8765, "alpha", {"host": "g4", "gpu": "RTX"}, NOW + 60) == {"host": "h100", "gpu": "H100"}


def test_forget():
    data = empty()
    registry.note_run(data, 8765, "alpha", {"host": "h", "gpu": "G"}, NOW)
    registry.claim(data, "alpha", "h", 8765, NOW, 600)
    assert registry.forget(data, "h") and not registry.forget(data, "h")
    assert data == {"claims": [], "bridges": {"8765": {"host": None, "last_run": NOW, "last_project": "alpha"}},
                    "hosts": {}}


def test_writers_take_turns(tmp_path):
    path = str(tmp_path / "registry.json")

    def add(i):
        for j in range(20):
            with registry.locked(path) as data:
                data["hosts"][f"h{i}-{j}"] = {}

    threads = [threading.Thread(target=add, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(registry.snapshot(path)["hosts"]) == 80
