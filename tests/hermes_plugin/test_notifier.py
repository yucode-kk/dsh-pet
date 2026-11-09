"""notifier：钩子 → 队列 → 后台线程 → 请求 的单向通道。"""

from __future__ import annotations

import time

import pytest

from hermes.hermes_adapter import notifier as notifier_mod
from hermes.hermes_adapter.notifier import Notifier


class RecordingSidecar:
    def __init__(self, runtime=None):
        self.runtime = runtime
        self.calls = 0

    def current(self):
        self.calls += 1
        return self.runtime


class Runtime:
    pid = 4242
    port = 3081
    token = "tok-secret"


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    def fake_request(port, method, path, **kwargs):
        calls.append({"port": port, "method": method, "path": path, **kwargs})
        return 200, {"ok": True}

    monkeypatch.setattr(notifier_mod.api, "request", fake_request)
    return calls


def test_hooks_enqueue_expected_states(monkeypatch):
    n = Notifier(RecordingSidecar())
    monkeypatch.setattr(n, "_ensure_thread", lambda: None)

    n.on_session_start(session_id="s1")
    n.on_post_tool(tool_name="terminal")
    n.on_post_tool()
    n.on_session_end(turn_exit_reason="completed")

    assert [n._queue.get_nowait() for _ in range(4)] == [
        ("thinking", None),
        ("working", "工具 terminal"),
        ("working", None),
        ("clear", None),
    ]


def test_push_drops_new_item_when_queue_full(monkeypatch):
    n = Notifier(RecordingSidecar())
    monkeypatch.setattr(n, "_ensure_thread", lambda: None)
    for index in range(n._queue.maxsize):
        n._queue.put_nowait(("thinking", str(index)))

    n.push("working", "塞不进去")  # 不抛、不阻塞，旧状态先落地

    assert n._queue.qsize() == n._queue.maxsize


def test_ensure_thread_starts_one_worker_only(monkeypatch):
    n = Notifier(RecordingSidecar())
    gate = __import__("threading").Event()
    monkeypatch.setattr(Notifier, "_drain", lambda self: gate.wait(5))

    n.push("thinking", None)
    first = n._thread
    n.push("working", "工具 terminal")

    assert n._thread is first
    assert first is not None and first.is_alive()

    gate.set()
    first.join(5)
    assert not first.is_alive()


def test_drain_survives_deliver_failure(monkeypatch):
    n = Notifier(RecordingSidecar())

    def boom(self, state, task):
        raise RuntimeError("端点炸了")

    monkeypatch.setattr(Notifier, "_deliver", boom)
    n.push("thinking", None)

    deadline = time.monotonic() + 5
    while n._queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.02)
    assert n._queue.unfinished_tasks == 0


def test_deliver_skips_when_pet_not_running():
    sidecar = RecordingSidecar(runtime=None)
    n = Notifier(sidecar)
    n._deliver("working", "工具 terminal")
    assert sidecar.calls == 1


def test_deliver_clear_sends_delete(recorder):
    n = Notifier(RecordingSidecar(Runtime()))
    n._deliver("clear", None)

    assert recorder == [
        {"port": 3081, "method": "DELETE", "path": "/work-status", "token": "tok-secret", "timeout": 1.0}
    ]


def test_deliver_ignores_unknown_state(recorder):
    n = Notifier(RecordingSidecar(Runtime()))
    n._deliver("bogus", None)
    assert recorder == []


def test_deliver_posts_state_with_truncated_task(recorder):
    n = Notifier(RecordingSidecar(Runtime()))
    n._deliver("working", "工" * 80)

    assert len(recorder) == 1
    call = recorder[0]
    assert call["method"] == "POST"
    assert call["path"] == "/work-status"
    assert call["payload"]["state"] == "working"
    assert len(call["payload"]["task"]) == notifier_mod.TASK_LIMIT


def test_deliver_posts_state_without_task(recorder):
    n = Notifier(RecordingSidecar(Runtime()))
    n._deliver("thinking", None)

    assert recorder[0]["payload"] == {"state": "thinking"}


def test_states_cover_the_six_documented_slots():
    assert notifier_mod.STATES == {"thinking", "working", "result", "waiting", "success", "error"}


def test_ensure_thread_restarts_dead_worker_but_keeps_watchdog(monkeypatch):
    n = Notifier(RecordingSidecar())
    monkeypatch.setattr(Notifier, "_drain", lambda self: None)  # 起来就退，模拟发送线程挂掉

    n.push("thinking", None)
    time.sleep(0.05)
    dog = n._watchdog
    n.push("thinking", None)
    time.sleep(0.05)

    assert dog is not None and dog.is_alive()
    assert n._watchdog is dog  # 看门狗还活着就不重复起


def test_idle_watchdog_clears_stuck_working(monkeypatch, recorder):
    # 后台复核/临时子会话里的工具调用不一定有回合收尾，状态会一直挂着；
    # 看门狗超时后要把它清掉，否则气泡一直“工具 xxx”、动画一直循环。
    monkeypatch.setattr(notifier_mod, "IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(notifier_mod, "SWEEP_INTERVAL", 0.02)
    n = Notifier(RecordingSidecar(Runtime()))
    n.push("working", "工具 skill_manage")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(call["method"] == "DELETE" for call in recorder):
            break
        time.sleep(0.02)

    assert [call["method"] for call in recorder][-1] == "DELETE"
    assert n._last_state == "clear"


def test_idle_watchdog_leaves_finished_state_alone(monkeypatch, recorder):
    monkeypatch.setattr(notifier_mod, "IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(notifier_mod, "SWEEP_INTERVAL", 0.02)
    n = Notifier(RecordingSidecar(Runtime()))
    n.push("success", None)

    time.sleep(0.25)

    assert [call["method"] for call in recorder] == ["POST"]
    assert n._last_state == "success"