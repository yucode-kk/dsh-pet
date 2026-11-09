"""``hermes dsh-pet-hermes ...`` 管理命令：查状态、启停，以及一条命令把自己卸掉。

卸载走官方 CLI（disable + remove），不手改 config.yaml、不手删注册记录；本文件只负责
先把桌宠进程收好，再让 Hermes 自己把插件摘掉。数据目录默认保留，``--purge`` 才删。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from argparse import ArgumentParser, Namespace
from typing import Any

PLUGIN_ID = "dsh-pet-hermes"


def attach(ctx: Any, sidecar: Any) -> None:
    """注册 ``hermes dsh-pet-hermes <action>``。"""
    ctx.register_cli_command(
        name=PLUGIN_ID,
        help="管理 dsh-pet 桌宠：status / start / stop / uninstall",
        setup_fn=_setup,
        handler_fn=lambda args: run(args, sidecar),
        description=(
            "dsh-pet 桌宠的 Hermes 侧管理命令。uninstall 会先停掉桌宠，再调用 "
            "`hermes plugins disable/remove` 把插件摘干净；--purge 连数据目录一起删。"
        ),
    )


def _setup(parser: ArgumentParser) -> None:
    parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["status", "start", "stop", "uninstall"],
        help="要做什么（默认 status）",
    )
    parser.add_argument("--purge", action="store_true", help="卸载时连同插件数据目录一起删除")
    parser.add_argument("-y", "--yes", action="store_true", help="卸载不再二次确认")


def run(args: Namespace, sidecar: Any) -> int:
    action = args.action
    if action == "status":
        return _status(sidecar)
    if action == "start":
        return _start(sidecar)
    if action == "stop":
        return _stop(sidecar)
    return _uninstall(sidecar, purge=args.purge, assume_yes=args.yes)


# ── 单个动作 ───────────────────────────────────────────────────────────
def _status(sidecar: Any) -> int:
    info = sidecar.status()
    others = sidecar.leftover_instances()
    if not info["running"]:
        print("桌宠：未运行")
        print(f"  数据目录  {info['home']}")
        print(f"  启动方式  hermes {PLUGIN_ID} start")
    else:
        print("桌宠：运行中")
        print(f"  pid       {info['pid']}")
        print(f"  端口      {info['port']}")
        print(f"  设置页    {info['settingsUrl']}   （含本次运行令牌，只在本机有效）")
        print(f"  数据目录  {info['home']}")
        print(f"  日志      {info['log']}")
    for other in others:
        print(
            f"  另有一只  {other['port']} 上的 dsh-pet 实例（pid {other['pid']}，{other['root'] or '未知根目录'}）"
        )
    return 0 if info["running"] else 1


def _start(sidecar: Any) -> int:
    try:
        info = sidecar.start(cleanup_on_exit=False)  # CLI 退出不该顺手把桌宠带走
    except Exception as error:
        print(f"启动失败：{error}")
        return 1
    if info.get("reused"):
        print(f"桌宠已在运行（pid {info['pid']}，端口 {info['port']}），没有重复拉起。")
    else:
        print(f"桌宠已启动（pid {info['pid']}，端口 {info['port']}）。")
        if info.get("electron"):
            print(f"Electron 运行时复用本机已有的一份：{info['electron']}")
        else:
            print(
                "本机没有现成的 Electron，首次启动会下载到插件数据目录（约 350 MB），"
                "需要等一会儿窗口才出现。"
            )
    for other in info.get("others") or []:
        print(
            f"注意：{other['port']} 上还有一只 dsh-pet 实例（pid {other['pid']}），不是这次拉起的，"
            "屏幕上会多一个窗口。"
        )
    return 0


def _stop(sidecar: Any) -> int:
    result = sidecar.stop()
    if not result.get("stopped"):
        if result.get("reason") == "unverified-pid":
            print(f"状态文件里的 pid {result.get('pid')} 和健康检查对不上，已忽略，没结束任何进程。")
            return 1
        print("桌宠本来就没在运行。")
        return 0
    forced = "，优雅退出超时后已强制结束" if result.get("forced") else ""
    print(f"桌宠已退出（pid {result.get('pid')}{forced}）。插件仍然装着，下次启用会再拉起来。")
    return 0


def _uninstall(sidecar: Any, *, purge: bool, assume_yes: bool) -> int:
    home = str(sidecar.home)
    print("即将卸载 dsh-pet Hermes 插件：")
    print(f"  1) 停掉桌宠进程（当前在跑的会被优雅退出）")
    print(f"  2) hermes plugins disable {PLUGIN_ID}")
    print(f"  3) hermes plugins remove  {PLUGIN_ID}")
    print(f"  4) 数据目录 {'删除' if purge else '保留'}：{home}")
    if not assume_yes:
        if not sys.stdin.isatty():
            print("非交互环境，确认不了；确要卸载请加 -y。")
            return 2
        if input("继续？[y/N] ").strip().lower() not in {"y", "yes"}:
            print("已取消，什么都没动。")
            return 1

    before = sidecar.read_runtime()
    stopped = sidecar.stop()
    if stopped.get("stopped"):
        print("  · 桌宠：已停")
    elif stopped.get("reason") == "unverified-pid":
        print(f"  · 桌宠：状态文件里的 pid {stopped.get('pid')} 对不上健康检查，已跳过")
    else:
        print("  · 桌宠：本来没在跑")

    # 用目录链接（junction）装的情况下先摘链接：rmtree 碰到 junction 会直接报错，
    # 也不能让它顺着链接去删仓库里的源码。
    link = _linked_plugin_dir(sidecar)
    if link is not None:
        try:
            if link.is_symlink():
                link.unlink()
            else:
                os.rmdir(link)  # junction 用 rmdir 只摘链接，不动目标
            print(f"  · 插件目录：已摘除链接 {link}")
        except OSError as error:
            print(f"  · 插件目录：摘链接失败（{error}）")

    for step, extra in (
        (["plugins", "disable", PLUGIN_ID], None),
        (["plugins", "remove", PLUGIN_ID], "y\n"),
    ):
        code, out, err = _hermes(step, stdin_text=extra)
        print(f"  · hermes {' '.join(step[:2])}：{'完成' if code == 0 else f'失败（退出码 {code}）'}")
        if code != 0:
            tail = (err or out).strip().splitlines()[-3:]
            for line in tail:
                print(f"      {line}")

    if purge:
        target = sidecar.home.parent
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            print(f"  · 数据目录：删除未完成（多半被占用了），还留着 {target}")
        else:
            print(f"  · 数据目录：已删除 {target}")
    else:
        print(f"  · 数据目录：保留在 {home}（想清干净，重装后跑 uninstall --purge，或手动删... 也行）")

    # 收尾复查：状态文件那只真的退了、端口真的放开了。幽灵实例就是这么漏过去的——
    # 状态文件没了、进程还在，窗口继续在屏幕上装宠物，直到下次重启。
    hard_fail = False
    if before is not None and sidecar.pid_alive(before.pid):
        print(f"  · 复查：桌宠进程 {before.pid} 没退，按父子关系强制结束")
        sidecar.terminate_pid(before.pid)
        time.sleep(0.6)
        if sidecar.pid_alive(before.pid):
            print(f"  ! 进程 {before.pid} 仍然活着，请手动结束它")
            hard_fail = True
        else:
            print("  · 复查：进程已结束")
    if before is not None and sidecar.port_open(before.port):
        print(f"  · 复查：端口 {before.port} 仍在监听")
        hard_fail = True
    for other in sidecar.leftover_instances():
        print(f"  · 另有实例：{other['port']}（pid {other['pid']}）不是本插件拉起的，没动它")

    print("卸载完成。桌面端/网关重启后，插件名才从插件列表里彻底消失。")
    return 1 if hard_fail else 0


# ── 调官方 CLI ─────────────────────────────────────────────────────────
def _linked_plugin_dir(sidecar: Any) -> Any:
    """插件目录是符号链接/junction 时返回它，普通目录返回 None。"""
    try:
        plugin_dir = sidecar.home.parents[2] / "plugins" / PLUGIN_ID
    except IndexError:
        return None
    is_junction = getattr(os.path, "isjunction", lambda _p: False)
    if plugin_dir.is_symlink() or is_junction(plugin_dir):
        return plugin_dir
    return None


def _hermes(argv: list[str], *, stdin_text: str | None = None) -> tuple[int, str, str]:
    """调用 hermes 本体；优先用 PATH 里的 exe，退化成当前解释器加载 hermes_cli。"""
    exe = shutil.which("hermes.exe") or shutil.which("hermes")
    if exe and (exe.lower().endswith(".exe") or sys.platform != "win32"):
        cmd = [exe]
    else:
        cmd = [sys.executable, "-c", "import sys; from hermes_cli.main import main; sys.exit(main())"]
    try:
        proc = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
            cmd + argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",       # Hermes CLI 一律 UTF-8；用系统默认 GBK 会解码炸掉
            errors="replace",
            timeout=180,
            check=False,
        )
    except Exception as error:  # noqa: BLE001 - 只把失败如实报给用户
        return 1, "", str(error)
    return proc.returncode, proc.stdout or "", proc.stderr or ""