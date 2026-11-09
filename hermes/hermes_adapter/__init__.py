"""dsh-pet 的 Hermes 适配层：把插件生命周期接到桌宠现有的独立运行时上。

这里不实现任何桌宠功能——动画、对话、余额、设置页全部复用仓库里的 dsh-pet。
本层只做三件事：注册钩子把 Hermes 的活动映射成工作状态、注册管理命令（含一条卸载入口）、
在插件被停用/卸载时把桌宠进程收好。
"""

from __future__ import annotations

import atexit
import logging
import sys
import threading
from typing import Any

from . import commands
from .notifier import Notifier
from .sidecar import PLUGIN_ID, Sidecar

__all__ = ["register", "PLUGIN_ID"]

_log = logging.getLogger("dsh_pet_hermes")

#: 自动拉起前先等一下，别挡住注册流程，也避开 doctor 之类的一次性进程。
_AUTOSTART_DELAY = 2.0


def _is_gateway_run(argv: list[str] | None = None) -> bool:
    """只有 ``hermes gateway run`` 才有桌宠的常驻生命周期；一次性 CLI 不起。

    网关就是桌面端、TUI、消息平台共用的那个进程，桌宠跟着它才符合预期；
    顺手执行 ``hermes plugins list`` 这类命令不应该弹出一只宠物。
    """
    words = list(sys.argv[1:] if argv is None else argv)
    return any(words[index : index + 2] == ["gateway", "run"] for index in range(len(words) - 1))


def _is_doctor() -> bool:
    """``hermes plugins doctor`` 用临时 HERMES_HOME 跑注册，期间不该起进程。"""
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return "hermes-plugin-doctor" in str(get_hermes_home())
    except Exception:
        return False


def register(ctx: Any) -> None:
    """Hermes 插件入口：注册钩子、管理命令与收尾。"""
    sidecar = Sidecar(ctx)
    notifier = Notifier(sidecar)

    commands.attach(ctx, sidecar)

    ctx.register_hook("on_session_start", notifier.on_session_start)
    ctx.register_hook("post_tool_call", notifier.on_post_tool)
    ctx.register_hook("on_session_end", notifier.on_session_end)

    # 插件被停用/卸载时收尾；只停“自己拉起来的”那只，绝不误停别的实例。
    unload = getattr(ctx, "on_unload", None)
    if callable(unload):
        unload(sidecar.stop_if_owned)
    atexit.register(sidecar.stop_if_owned)

    if sidecar.autostart and _is_gateway_run() and not _is_doctor():
        threading.Thread(
            target=_autostart,
            args=(sidecar,),
            name="dsh-pet-hermes-autostart",
            daemon=True,
        ).start()


def _autostart(sidecar: Sidecar) -> None:
    """延迟拉起；失败只记日志，不影响 Hermes 启动。"""
    import time

    time.sleep(_AUTOSTART_DELAY)
    try:
        info = sidecar.start()
    except Exception as error:  # noqa: BLE001 - 启动失败不能拖垮网关
        _log.warning("dsh-pet 桌宠启动失败：%s", error)
        return
    _log.info(
        "dsh-pet 桌宠%s（pid %s，端口 %s，数据根 %s）",
        "已在运行，复用原有实例" if info.get("reused") else "已启动",
        info.get("pid"),
        info.get("port"),
        sidecar.home,
    )