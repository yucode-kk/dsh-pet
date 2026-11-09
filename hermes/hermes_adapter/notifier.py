"""把 Hermes 的回合/工具事件翻译成桌宠工作状态，且绝不阻塞调度线程。

Hermes 的钩子跑在网关进程里，回调里做网络请求会拖慢回合，所以这里只往队列丢一条，
由一条常驻守护线程去发；桌宠没在跑、请求失败、超时都直接丢掉，不往外抛。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any, Optional

from . import api

#: 外部写入点接受的 6 个档位（见 openapi.yaml 的 setWorkStatus）。
STATES = frozenset({"thinking", "working", "result", "waiting", "success", "error"})
#: 气泡文案上限（码点），超了会被端点判成 bad-request。
TASK_LIMIT = 40

#: 多久没有新事件就把状态清回空闲（秒）。后台复核、临时子会话这类工具调用不一定
#: 走完整的回合收尾，光靠 on_session_end 会有“工具 xxx”永远挂在气泡上、动画一直循环。
IDLE_TIMEOUT = float(os.environ.get("DSH_PET_IDLE_TIMEOUT", "120"))
#: 看门狗巡检间隔（秒）。
SWEEP_INTERVAL = float(os.environ.get("DSH_PET_IDLE_SWEEP", "10"))
#: 这些档位是“进行中”，超过 IDLE_TIMEOUT 没动静就当结束了。
_ACTIVE = frozenset({"thinking", "working", "result", "waiting"})


class Notifier:
    """钩子 → 桌宠工作状态的最小单向通道。"""

    def __init__(self, sidecar: Any) -> None:
        self._sidecar = sidecar
        self._queue: "queue.Queue[tuple[str, Optional[str]]]" = queue.Queue(maxsize=8)
        self._thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_state: Optional[str] = None
        self._last_activity = 0.0

    # ── 钩子回调：只入队，立即返回 ─────────────────────────────────────
    def on_session_start(self, **kwargs: Any) -> None:
        self.push("thinking", None)

    def on_post_tool(self, tool_name: Optional[str] = None, **kwargs: Any) -> None:
        self.push("working", f"工具 {tool_name}" if tool_name else None)

    def on_session_end(self, **kwargs: Any) -> None:
        # 这里必须挂 on_session_end（真正的“回合结束”，payload 带 turn_exit_reason），
        # 而不是 post_llm_call：后者每次模型调用都会触发，带工具调用的回合会变成
        # working → 清空 → working 来回闪，还会把待机动画链重启一遍。
        self.push("clear", None)

    def push(self, state: str, task: Optional[str]) -> None:
        self._last_state = state
        self._last_activity = time.monotonic()
        try:
            self._queue.put_nowait((state, task))
        except queue.Full:
            return  # 攒不下就丢新的，旧状态先落地更重要
        self._ensure_thread()

    # ── 后台发送 ─────────────────────────────────────────────────────
    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            thread = threading.Thread(target=self._drain, name="dsh-pet-notifier", daemon=True)
            self._thread = thread
            thread.start()
            if self._watchdog is None or not self._watchdog.is_alive():
                dog = threading.Thread(target=self._idle_watch, name="dsh-pet-idle", daemon=True)
                self._watchdog = dog
                dog.start()

    def _idle_watch(self) -> None:
        """巡检：进行中的档位太久没新事件就当回合已经结束，清回空闲。"""
        while True:
            time.sleep(SWEEP_INTERVAL)
            stuck = (
                self._last_state in _ACTIVE
                and time.monotonic() - self._last_activity >= IDLE_TIMEOUT
            )
            if stuck:
                self.push("clear", None)

    def _drain(self) -> None:
        while True:
            state, task = self._queue.get()
            try:
                self._deliver(state, task)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    def _deliver(self, state: str, task: Optional[str]) -> None:
        runtime = self._sidecar.current()
        if runtime is None:
            return
        if state == "clear":
            api.request(
                runtime.port, "DELETE", "/work-status", token=runtime.token, timeout=1.0
            )
            return
        if state not in STATES:
            return
        payload: dict[str, Any] = {"state": state}
        if task:
            payload["task"] = task[:TASK_LIMIT]
        api.request(
            runtime.port, "POST", "/work-status", token=runtime.token, payload=payload, timeout=1.0
        )