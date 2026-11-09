"""插件注册：钩子、管理命令、卸载收尾，以及自动拉起的三个门禁。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes
import hermes.hermes_adapter as plugin


@pytest.fixture
def gateway_env(tmp_path, monkeypatch, package_tree):
    """让 register() 真的能构造出 Sidecar：数据目录和包位置都指到 tmp。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("DSH_PET_HOME", str(package_tree))
    monkeypatch.delitem(sys.modules, "plugins.plugin_storage", raising=False)
    return tmp_path


class ImmediateThread:
    """把后台线程换成同步执行，测试里不必等 2 秒。"""

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True
        self.target(*self.args)


def test_package_entry_exports_register():
    assert hermes.register is plugin.register


def test_is_gateway_run_matches_only_gateway_run():
    assert plugin._is_gateway_run(["gateway", "run"]) is True
    assert plugin._is_gateway_run(["--verbose", "gateway", "run", "--port", "1"]) is True
    assert plugin._is_gateway_run(["gateway", "status"]) is False
    assert plugin._is_gateway_run(["plugins", "list"]) is False
    assert plugin._is_gateway_run([]) is False


def test_is_gateway_run_reads_process_argv(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
    assert plugin._is_gateway_run() is True

    monkeypatch.setattr(sys, "argv", ["hermes", "dsh-pet-hermes", "start"])
    assert plugin._is_gateway_run() is False


def test_is_doctor_detects_doctor_home(tmp_path, monkeypatch):
    fake = type(sys)("hermes_constants")
    fake.get_hermes_home = lambda: tmp_path / "hermes-plugin-doctor-abc"
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)

    assert plugin._is_doctor() is True

    fake.get_hermes_home = lambda: tmp_path / "hermes"
    assert plugin._is_doctor() is False


def test_is_doctor_false_when_helper_missing(monkeypatch):
    monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
    assert plugin._is_doctor() is False


def test_is_doctor_false_when_helper_raises(monkeypatch):
    fake = type(sys)("hermes_constants")

    def boom():
        raise RuntimeError("还没初始化")

    fake.get_hermes_home = boom
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)

    assert plugin._is_doctor() is False


def test_register_wires_hooks_and_unload(gateway_env, monkeypatch):
    from conftest import FakeCtx

    ctx = FakeCtx({"autostart": False})
    plugin.register(ctx)

    hook_names = [name for name, _ in ctx.hooks]
    assert hook_names == ["on_session_start", "post_tool_call", "on_session_end"]
    assert all(callable(handler) for _, handler in ctx.hooks)
    assert len(ctx.cli_commands) == 1
    assert len(ctx.unload_callbacks) == 1
    # 卸载回调可以安全调用（没拉起过进程就什么都不做）
    ctx.unload_callbacks[0]()
    assert (gateway_env / "plugin-data" / "dsh-pet-hermes" / "runtime-home").parent.exists()


def test_register_skips_autostart_when_not_gateway(gateway_env, monkeypatch):
    from conftest import FakeCtx

    monkeypatch.setattr(plugin, "_is_gateway_run", lambda argv=None: False)
    monkeypatch.setattr(plugin, "_autostart", lambda sidecar: pytest.fail("不该自动拉起"))
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)

    ctx = FakeCtx({"autostart": True})
    plugin.register(ctx)

    assert [name for name, _ in ctx.hooks] == ["on_session_start", "post_tool_call", "on_session_end"]


def test_register_skips_autostart_during_doctor(gateway_env, monkeypatch):
    from conftest import FakeCtx

    monkeypatch.setattr(plugin, "_is_gateway_run", lambda argv=None: True)
    monkeypatch.setattr(plugin, "_is_doctor", lambda: True)
    monkeypatch.setattr(plugin, "_autostart", lambda sidecar: pytest.fail("doctor 期间不该拉起"))
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)

    plugin.register(FakeCtx({"autostart": True}))


def test_register_skips_autostart_when_disabled(gateway_env, monkeypatch):
    from conftest import FakeCtx

    monkeypatch.setattr(plugin, "_is_gateway_run", lambda argv=None: True)
    monkeypatch.setattr(plugin, "_is_doctor", lambda: False)
    monkeypatch.setattr(plugin, "_autostart", lambda sidecar: pytest.fail("配置关了就不该拉起"))
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)

    plugin.register(FakeCtx({"autostart": False}))


def test_register_starts_autostart_thread_on_gateway(gateway_env, monkeypatch):
    from conftest import FakeCtx

    started = []
    monkeypatch.setattr(plugin, "_is_gateway_run", lambda argv=None: True)
    monkeypatch.setattr(plugin, "_is_doctor", lambda: False)
    monkeypatch.setattr(plugin, "_autostart", lambda sidecar: started.append(sidecar))
    monkeypatch.setattr(plugin.threading, "Thread", ImmediateThread)

    plugin.register(FakeCtx({"autostart": True}))

    assert len(started) == 1
    assert started[0].home.name == "runtime-home"


def test_register_tolerates_context_without_unload(gateway_env, monkeypatch):
    class MinimalCtx:
        def __init__(self):
            self.hooks = []

        def get_config(self, key, default=None):
            return False if key == "autostart" else default

        def register_hook(self, name, handler):
            self.hooks.append(name)

        def register_cli_command(self, **kwargs):
            pass

    plugin.register(MinimalCtx())


def test_autostart_logs_reuse(gateway_env, monkeypatch, caplog):
    from conftest import FakeCtx

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    sidecar = plugin.Sidecar(FakeCtx())
    monkeypatch.setattr(
        sidecar, "start", lambda **kwargs: {"ok": True, "reused": True, "pid": 4242, "port": 3081}
    )

    with caplog.at_level("INFO"):
        plugin._autostart(sidecar)

    assert any("已在运行" in record.message for record in caplog.records)


def test_autostart_logs_fresh_launch(gateway_env, monkeypatch, caplog):
    from conftest import FakeCtx

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    sidecar = plugin.Sidecar(FakeCtx())
    monkeypatch.setattr(
        sidecar, "start", lambda **kwargs: {"ok": True, "reused": False, "pid": 5151, "port": 3082}
    )

    with caplog.at_level("INFO"):
        plugin._autostart(sidecar)

    assert any("已启动" in record.message for record in caplog.records)


def test_autostart_swallows_start_errors(gateway_env, monkeypatch, caplog):
    from conftest import FakeCtx

    monkeypatch.setattr("time.sleep", lambda seconds: None)
    sidecar = plugin.Sidecar(FakeCtx())

    def boom(**kwargs):
        raise RuntimeError("Electron 没下下来")

    monkeypatch.setattr(sidecar, "start", boom)

    with caplog.at_level("WARNING"):
        plugin._autostart(sidecar)  # 不抛

    assert any("启动失败" in record.message for record in caplog.records)