"""commands：status/start/stop/uninstall 四个动作与官方 CLI 调用。"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes.hermes_adapter import commands
from hermes.hermes_adapter.commands import PLUGIN_ID

from conftest import FakeCtx, make_runtime


class FakeSidecar:
    """commands 侧只需要这几个方法，行为都由测试摆好。"""

    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "plugin-data" / PLUGIN_ID / "runtime-home"
        self.runtime = None
        self.others: list[dict] = []
        self.status_info = {
            "running": False,
            "pid": None,
            "port": None,
            "settingsUrl": None,
            "home": str(self.home),
            "log": str(self.home / "dsh-pet" / "hermes-launch.log"),
        }
        self.start_result: dict = {"ok": True, "reused": False, "pid": 4242, "port": 3081, "others": []}
        self.start_error: BaseException | None = None
        self.stop_result: dict = {"ok": True, "stopped": True, "pid": 4242}
        self.alive: dict[int, bool] = {}
        self.ports: dict[int, bool] = {}
        self.terminated: list[int] = []
        self.stop_calls = 0

    def status(self):
        return self.status_info

    def leftover_instances(self):
        return self.others

    def start(self, *, cleanup_on_exit: bool = True):
        if self.start_error is not None:
            raise self.start_error
        return self.start_result

    def stop(self, timeout: float = 0):
        self.stop_calls += 1
        return self.stop_result

    def read_runtime(self):
        return self.runtime

    def pid_alive(self, pid: int) -> bool:
        return self.alive.get(pid, False)

    def port_open(self, port: int) -> bool:
        return self.ports.get(port, False)

    def terminate_pid(self, pid: int) -> None:
        self.terminated.append(pid)


@pytest.fixture
def fake_sidecar(tmp_path):
    return FakeSidecar(tmp_path)


# ── 注册与参数 ─────────────────────────────────────────────────────────
def test_attach_registers_cli_command(fake_sidecar, fake_ctx):
    commands.attach(fake_ctx, fake_sidecar)

    assert len(fake_ctx.cli_commands) == 1
    spec = fake_ctx.cli_commands[0]
    assert spec["name"] == PLUGIN_ID
    assert "status / start / stop / uninstall" in spec["help"]
    assert "uninstall" in spec["description"]

    fake_sidecar.status_info["running"] = True
    assert spec["handler_fn"](Namespace(action="status", purge=False, yes=True)) == 0


def test_setup_defaults_and_flags():
    parser = ArgumentParser()
    commands._setup(parser)

    default = parser.parse_args([])
    assert default.action == "status"
    assert default.purge is False and default.yes is False

    full = parser.parse_args(["uninstall", "--purge", "-y"])
    assert full.action == "uninstall" and full.purge is True and full.yes is True


def test_run_dispatches_each_action(monkeypatch, fake_sidecar):
    seen = []
    monkeypatch.setattr(commands, "_status", lambda sc: seen.append("status") or 10)
    monkeypatch.setattr(commands, "_start", lambda sc: seen.append("start") or 11)
    monkeypatch.setattr(commands, "_stop", lambda sc: seen.append("stop") or 12)
    monkeypatch.setattr(
        commands, "_uninstall", lambda sc, *, purge, assume_yes: seen.append(("uninstall", purge, assume_yes)) or 13
    )

    assert commands.run(Namespace(action="status", purge=False, yes=False), fake_sidecar) == 10
    assert commands.run(Namespace(action="start", purge=False, yes=False), fake_sidecar) == 11
    assert commands.run(Namespace(action="stop", purge=False, yes=False), fake_sidecar) == 12
    assert (
        commands.run(Namespace(action="uninstall", purge=True, yes=True), fake_sidecar) == 13
    )
    assert seen == ["status", "start", "stop", ("uninstall", True, True)]


# ── status ─────────────────────────────────────────────────────────────
def test_status_when_idle(capsys, fake_sidecar):
    assert commands._status(fake_sidecar) == 1

    out = capsys.readouterr().out
    assert "桌宠：未运行" in out
    assert f"hermes {PLUGIN_ID} start" in out


def test_status_when_running_and_neighbors_exist(capsys, fake_sidecar):
    fake_sidecar.status_info.update(
        running=True, pid=4242, port=3081, settingsUrl="http://127.0.0.1:3081/settings?token=x"
    )
    fake_sidecar.others = [{"port": 3080, "pid": 8, "root": "E:\\DSH"}]

    assert commands._status(fake_sidecar) == 0

    out = capsys.readouterr().out
    assert "桌宠：运行中" in out
    assert "pid       4242" in out
    assert "另有一只  3080 上的 dsh-pet 实例（pid 8，E:\\DSH）" in out


def test_status_neighbor_without_root(capsys, fake_sidecar):
    fake_sidecar.others = [{"port": 3080, "pid": 8, "root": ""}]
    commands._status(fake_sidecar)

    assert "未知根目录" in capsys.readouterr().out


# ── start ──────────────────────────────────────────────────────────────
def test_start_reports_failure(capsys, fake_sidecar):
    fake_sidecar.start_error = RuntimeError("electron 下载失败")

    assert commands._start(fake_sidecar) == 1
    assert "启动失败：electron 下载失败" in capsys.readouterr().out


def test_start_reports_reuse_and_neighbors(capsys, fake_sidecar):
    fake_sidecar.start_result = {
        "ok": True,
        "reused": True,
        "pid": 4242,
        "port": 3081,
        "others": [{"port": 3080, "pid": 8, "root": "x"}],
    }

    assert commands._start(fake_sidecar) == 0

    out = capsys.readouterr().out
    assert "桌宠已在运行（pid 4242，端口 3081）" in out
    assert "注意：3080 上还有一只 dsh-pet 实例（pid 8）" in out


def test_start_reports_fresh_launch(capsys, fake_sidecar):
    fake_sidecar.start_result = {
        "ok": True,
        "reused": False,
        "pid": 4242,
        "port": 3081,
        "electron": "C:\\Users\\x\\.dsh\\electron\\electron.exe",
    }

    assert commands._start(fake_sidecar) == 0

    out = capsys.readouterr().out
    assert "桌宠已启动（pid 4242，端口 3081）" in out
    assert "复用本机已有的一份" in out


def test_start_warns_when_electron_must_download(capsys, fake_sidecar):
    fake_sidecar.start_result = {"ok": True, "reused": False, "pid": 4242, "port": 3081, "electron": None}

    assert commands._start(fake_sidecar) == 0

    out = capsys.readouterr().out
    assert "350 MB" in out


# ── stop ───────────────────────────────────────────────────────────────
def test_stop_reports_unverified_pid(capsys, fake_sidecar):
    fake_sidecar.stop_result = {"ok": True, "stopped": False, "reason": "unverified-pid", "pid": 999}

    assert commands._stop(fake_sidecar) == 1
    out = capsys.readouterr().out
    assert "pid 999 和健康检查对不上" in out


def test_stop_reports_not_running(capsys, fake_sidecar):
    fake_sidecar.stop_result = {"ok": True, "stopped": False, "reason": "not-running"}

    assert commands._stop(fake_sidecar) == 0
    assert "桌宠本来就没在运行。" in capsys.readouterr().out


def test_stop_reports_forced_exit(capsys, fake_sidecar):
    fake_sidecar.stop_result = {"ok": True, "stopped": True, "pid": 4242, "forced": True}

    assert commands._stop(fake_sidecar) == 0
    assert "优雅退出超时后已强制结束" in capsys.readouterr().out


def test_stop_reports_graceful_exit(capsys, fake_sidecar):
    fake_sidecar.stop_result = {"ok": True, "stopped": True, "pid": 4242}

    assert commands._stop(fake_sidecar) == 0
    assert "桌宠已退出（pid 4242）。" in capsys.readouterr().out


# ── uninstall ──────────────────────────────────────────────────────────
def test_uninstall_needs_yes_in_non_interactive_shell(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands.sys, "stdin", SimpleNamespace(isatty=lambda: False))

    assert commands._uninstall(fake_sidecar, purge=False, assume_yes=False) == 2
    assert "非交互环境，确认不了" in capsys.readouterr().out


def test_uninstall_can_be_cancelled(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    assert commands._uninstall(fake_sidecar, purge=False, assume_yes=False) == 1
    assert "已取消，什么都没动。" in capsys.readouterr().out
    assert fake_sidecar.stop_calls == 0


def test_uninstall_confirmed_by_input(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))

    assert commands._uninstall(fake_sidecar, purge=False, assume_yes=False) == 0
    assert fake_sidecar.stop_calls == 1


def test_uninstall_full_flow_without_purge(capsys, fake_sidecar, monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: calls.append((argv, stdin_text)) or (0, "", ""))
    fake_sidecar.stop_result = {"ok": True, "stopped": True, "pid": 4242}

    assert commands._uninstall(fake_sidecar, purge=False, assume_yes=True) == 0

    out = capsys.readouterr().out
    assert "桌宠：已停" in out
    assert "hermes plugins disable：完成" in out
    assert "数据目录：保留在" in out
    assert "卸载完成。" in out
    assert calls == [(["plugins", "disable", PLUGIN_ID], None), (["plugins", "remove", PLUGIN_ID], "y\n")]


def test_uninstall_prints_cli_failure_tail(capsys, fake_sidecar, monkeypatch):
    outputs = iter([(0, "", ""), (1, "stdout 尾巴", "err 第一行\nerr 第二行\nerr 第三行")])
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: next(outputs))

    assert commands._uninstall(fake_sidecar, purge=False, assume_yes=True) == 0

    out = capsys.readouterr().out
    assert "hermes plugins remove：失败（退出码 1）" in out
    assert "err 第三行" in out


def test_uninstall_reports_unverified_pid(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    fake_sidecar.stop_result = {"ok": True, "stopped": False, "reason": "unverified-pid", "pid": 999}

    commands._uninstall(fake_sidecar, purge=False, assume_yes=True)

    assert "状态文件里的 pid 999 对不上健康检查，已跳过" in capsys.readouterr().out


def test_uninstall_reports_not_running(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    fake_sidecar.stop_result = {"ok": True, "stopped": False, "reason": "not-running"}

    commands._uninstall(fake_sidecar, purge=False, assume_yes=True)

    assert "桌宠：本来没在跑" in capsys.readouterr().out


def test_uninstall_purges_data_directory(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    target = fake_sidecar.home.parent
    target.mkdir(parents=True)
    (target / "junk.txt").write_text("x", encoding="utf-8")

    assert commands._uninstall(fake_sidecar, purge=True, assume_yes=True) == 0

    assert not target.exists()
    assert "数据目录：已删除" in capsys.readouterr().out


def test_uninstall_reports_incomplete_purge(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    monkeypatch.setattr(commands.shutil, "rmtree", lambda target, ignore_errors=False: None)
    (fake_sidecar.home.parent).mkdir(parents=True)

    commands._uninstall(fake_sidecar, purge=True, assume_yes=True)

    assert "删除未完成" in capsys.readouterr().out


def test_uninstall_removes_plugin_junction(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    monkeypatch.setattr(commands.os.path, "isjunction", lambda path: True, raising=False)
    plugin_dir = fake_sidecar.home.parents[2] / "plugins" / PLUGIN_ID
    plugin_dir.mkdir(parents=True)

    commands._uninstall(fake_sidecar, purge=False, assume_yes=True)

    assert not plugin_dir.exists()
    assert "已摘除链接" in capsys.readouterr().out


def test_uninstall_unlinks_plugin_symlink(capsys, fake_sidecar, monkeypatch):
    class FakeLink:
        def __init__(self):
            self.unlinked = 0

        def is_symlink(self):
            return True

        def unlink(self):
            self.unlinked += 1

    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    link = FakeLink()
    monkeypatch.setattr(commands, "_linked_plugin_dir", lambda sidecar: link)

    commands._uninstall(fake_sidecar, purge=False, assume_yes=True)

    assert link.unlinked == 1
    assert "已摘除链接" in capsys.readouterr().out


def test_uninstall_reports_junction_removal_failure(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    monkeypatch.setattr(commands.os.path, "isjunction", lambda path: True, raising=False)
    plugin_dir = fake_sidecar.home.parents[2] / "plugins" / PLUGIN_ID
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "leftover.py").write_text("x", encoding="utf-8")  # 非空目录，rmdir 必然失败

    commands._uninstall(fake_sidecar, purge=False, assume_yes=True)

    assert "摘链接失败" in capsys.readouterr().out


def test_uninstall_runs_post_check_and_kills_lingering_process(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    monkeypatch.setattr(commands.time, "sleep", lambda seconds: None)
    rt = make_runtime(pid=4242, port=3081)
    fake_sidecar.runtime = rt
    # 第一次查还活着，等到 terminate 之后再查已经没了
    fake_sidecar.alive = {4242: True}

    def terminate(pid):
        fake_sidecar.terminated.append(pid)
        fake_sidecar.alive[pid] = False

    monkeypatch.setattr(fake_sidecar, "terminate_pid", terminate)

    assert commands._uninstall(fake_sidecar, purge=False, assume_yes=True) == 0

    out = capsys.readouterr().out
    assert "桌宠进程 4242 没退，按父子关系强制结束" in out
    assert "复查：进程已结束" in out


def test_uninstall_fails_when_process_survives_kill(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    monkeypatch.setattr(commands.time, "sleep", lambda seconds: None)
    fake_sidecar.runtime = make_runtime(pid=4242, port=3081)
    fake_sidecar.alive = {4242: True}  # 杀了也还活着

    assert commands._uninstall(fake_sidecar, purge=False, assume_yes=True) == 1
    assert "进程 4242 仍然活着" in capsys.readouterr().out


def test_uninstall_fails_when_port_still_listening(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    fake_sidecar.runtime = make_runtime(pid=4242, port=3081)
    fake_sidecar.alive = {4242: False}
    fake_sidecar.ports = {3081: True}

    assert commands._uninstall(fake_sidecar, purge=False, assume_yes=True) == 1
    assert "端口 3081 仍在监听" in capsys.readouterr().out


def test_uninstall_lists_identical_neighbor_instances(capsys, fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands, "_hermes", lambda argv, stdin_text=None: (0, "", ""))
    fake_sidecar.others = [{"port": 3080, "pid": 8, "root": "E:\\DSH"}]

    assert commands._uninstall(fake_sidecar, purge=False, assume_yes=True) == 0
    assert "另有实例：3080（pid 8）不是本插件拉起的，没动它" in capsys.readouterr().out


# ── 辅助函数 ───────────────────────────────────────────────────────────
def test_linked_plugin_dir_ignores_plain_directory(fake_sidecar):
    plugin_dir = fake_sidecar.home.parents[2] / "plugins" / PLUGIN_ID
    plugin_dir.mkdir(parents=True)

    assert commands._linked_plugin_dir(fake_sidecar) is None


def test_linked_plugin_dir_detects_junction(fake_sidecar, monkeypatch):
    monkeypatch.setattr(commands.os.path, "isjunction", lambda path: True, raising=False)
    plugin_dir = fake_sidecar.home.parents[2] / "plugins" / PLUGIN_ID
    plugin_dir.mkdir(parents=True)

    assert commands._linked_plugin_dir(fake_sidecar) == plugin_dir


def test_linked_plugin_dir_tolerates_short_path():
    short = SimpleNamespace(home=Path("C:/only"))
    assert commands._linked_plugin_dir(short) is None


def test_hermes_uses_exe_from_path(monkeypatch):
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(commands.shutil, "which", lambda name: "C:/tools/hermes.exe" if name.endswith(".exe") else None)
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    code, out, err = commands._hermes(["plugins", "list"], stdin_text=None)

    assert (code, out, err) == (0, "ok", "")
    assert recorded["argv"] == ["C:/tools/hermes.exe", "plugins", "list"]
    assert recorded["kwargs"]["encoding"] == "utf-8"


def test_hermes_falls_back_to_python_entry(monkeypatch):
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        return SimpleNamespace(returncode=3, stdout=None, stderr=None)

    monkeypatch.setattr(commands.shutil, "which", lambda name: "hermes" if name == "hermes" else None)
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    code, out, err = commands._hermes(["plugins", "doctor"], stdin_text="y\n")

    assert code == 3 and out == "" and err == ""
    assert recorded["argv"][0] == sys.executable
    assert recorded["argv"][1] == "-c"


def test_hermes_reports_subprocess_error(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("hermes 不在")

    monkeypatch.setattr(commands.shutil, "which", lambda name: "C:/tools/hermes.exe")
    monkeypatch.setattr(commands.subprocess, "run", boom)

    code, out, err = commands._hermes(["plugins", "list"])

    assert code == 1 and out == "" and "hermes 不在" in err