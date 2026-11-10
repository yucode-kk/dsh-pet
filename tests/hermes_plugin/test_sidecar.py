"""sidecar：定位 dsh-pet 包、拉起/复用/收尾、邻居实例探测。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes.hermes_adapter import sidecar as S
from hermes.hermes_adapter.sidecar import Sidecar, SidecarError

from conftest import FakeCtx, FakePopen, make_runtime


def seq_alive(*values):
    """按顺序返回的存活判断替身；用光后一直返回最后一个。"""
    remaining = list(values)

    def _alive(self, pid: int) -> bool:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return _alive


# ── 包定位 ─────────────────────────────────────────────────────────────
def test_resolve_prefers_env_var(sidecar_env, package_tree):
    assert sidecar_env.package_dir == package_tree
    assert sidecar_env.script.exists()
    assert sidecar_env.home.name == "runtime-home"


def test_resolve_prefers_configured_setting(tmp_path, monkeypatch, fake_ctx, package_tree):
    data = tmp_path / "plugin-data"
    monkeypatch.setattr(S, "_plugin_data_dir", lambda: data)
    monkeypatch.delenv("DSH_PET_HOME", raising=False)
    fake_ctx.settings["dsh_pet_path"] = str(package_tree)

    sc = Sidecar(fake_ctx)

    assert sc.package_dir == package_tree


def test_resolve_uses_package_path_pointer(sidecar_env, tmp_path, monkeypatch, package_tree):
    monkeypatch.delenv("DSH_PET_HOME", raising=False)
    plugin_root = tmp_path / "plugins" / "dsh-pet"
    plugin_root.mkdir(parents=True)
    (plugin_root / "package-path.json").write_text(
        json.dumps({"packageRoot": str(package_tree)}), encoding="utf-8-sig"
    )
    sidecar_env.root = plugin_root

    assert sidecar_env._resolve_package_dir() == package_tree


def test_resolve_ignores_broken_pointer(sidecar_env, tmp_path, monkeypatch, package_tree):
    monkeypatch.delenv("DSH_PET_HOME", raising=False)
    plugin_root = tmp_path / "hermes"
    plugin_root.mkdir(parents=True)
    (plugin_root / "package-path.json").write_text("{ 写坏了", encoding="utf-8")
    sidecar_env.root = plugin_root

    # 指针读不出来就继续往下找：同级目录 dsh-pet
    assert sidecar_env._resolve_package_dir() == package_tree


def test_resolve_skips_empty_pointer_record(sidecar_env, tmp_path, monkeypatch, package_tree):
    monkeypatch.delenv("DSH_PET_HOME", raising=False)
    plugin_root = tmp_path / "hermes"
    plugin_root.mkdir(parents=True)
    (plugin_root / "package-path.json").write_text(json.dumps({"packageRoot": ""}), encoding="utf-8")
    sidecar_env.root = plugin_root

    assert sidecar_env._resolve_package_dir() == package_tree


def test_resolve_uses_sibling_dsh_pet(sidecar_env, tmp_path, monkeypatch):
    monkeypatch.delenv("DSH_PET_HOME", raising=False)
    plugin_root = tmp_path / "repo" / "hermes"
    plugin_root.mkdir(parents=True)
    sibling = tmp_path / "repo" / "dsh-pet"
    (sibling / "lib").mkdir(parents=True)
    (sibling / "lib" / "standalone.js").write_text("//", encoding="utf-8")
    sidecar_env.root = plugin_root

    assert sidecar_env._resolve_package_dir() == sibling


def test_resolve_uses_plugin_node_modules(sidecar_env, tmp_path, monkeypatch):
    monkeypatch.delenv("DSH_PET_HOME", raising=False)
    plugin_root = tmp_path / "nest" / "deep" / "plugin"
    plugin_root.mkdir(parents=True)
    bundled = plugin_root / "node_modules" / "dsh-pet"
    (bundled / "lib").mkdir(parents=True)
    (bundled / "lib" / "standalone.js").write_text("//", encoding="utf-8")
    sidecar_env.root = plugin_root

    assert sidecar_env._resolve_package_dir() == bundled


def test_resolve_falls_back_to_vendored_when_nothing_exists(sidecar_env, tmp_path, monkeypatch):
    monkeypatch.delenv("DSH_PET_HOME", raising=False)
    plugin_root = tmp_path / "no-such" / "hermes"
    plugin_root.mkdir(parents=True)
    sidecar_env.root = plugin_root

    assert sidecar_env._resolve_package_dir() == plugin_root / "dsh-pet"


def test_resolve_uses_vendored_runtime(sidecar_env, tmp_path, monkeypatch):
    monkeypatch.delenv("DSH_PET_HOME", raising=False)
    plugin_root = tmp_path / "plugins" / "dsh-pet"
    vendored = plugin_root / "dsh-pet"
    (vendored / "lib").mkdir(parents=True)
    (vendored / "lib" / "standalone.js").write_text("//", encoding="utf-8")
    sidecar_env.root = plugin_root

    assert sidecar_env._resolve_package_dir() == vendored


def test_vendored_runtime_beats_repo_sibling(sidecar_env, tmp_path, monkeypatch):
    monkeypatch.delenv("DSH_PET_HOME", raising=False)
    repo = tmp_path / "repo"
    plugin_root = repo / "hermes"
    plugin_root.mkdir(parents=True)
    sibling = repo / "dsh-pet"
    (sibling / "lib").mkdir(parents=True)
    (sibling / "lib" / "standalone.js").write_text("// 仓库那份", encoding="utf-8")
    vendored = plugin_root / "dsh-pet"
    (vendored / "lib").mkdir(parents=True)
    (vendored / "lib" / "standalone.js").write_text("// 插件自带", encoding="utf-8")
    sidecar_env.root = plugin_root

    assert sidecar_env._resolve_package_dir() == vendored


def test_plugin_data_dir_prefers_hermes_storage(tmp_path, monkeypatch):
    target = tmp_path / "profile-plugin-data"
    fake = type(sys)("plugins.plugin_storage")
    fake.plugin_data_dir = lambda plugin_id: target / plugin_id
    monkeypatch.setitem(sys.modules, "plugins", type(sys)("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", fake)

    assert S._plugin_data_dir() == target / S.PLUGIN_ID


def test_plugin_data_dir_falls_back_to_hermes_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delitem(sys.modules, "plugins.plugin_storage", raising=False)

    path = S._plugin_data_dir()

    assert path == tmp_path / "plugin-data" / S.PLUGIN_ID
    assert path.exists()


# ── 配置 ───────────────────────────────────────────────────────────────
def test_setting_swallows_context_errors(tmp_path, monkeypatch):
    class Boom:
        def get_config(self, key, default=None):
            raise RuntimeError("上下文还没就绪")

    monkeypatch.setattr(S, "_plugin_data_dir", lambda: tmp_path / "data")
    sc = Sidecar(Boom())

    assert sc._setting("port", 3080) == 3080
    assert sc.autostart is True


def test_setting_treats_none_as_default(sidecar_env, fake_ctx):
    fake_ctx.settings["port"] = None
    assert sidecar_env.port == S.DEFAULT_PORT


def test_port_accepts_valid_values(sidecar_env, fake_ctx):
    fake_ctx.settings["port"] = 4096
    assert sidecar_env.port == 4096

    fake_ctx.settings["port"] = "abc"
    assert sidecar_env.port == S.DEFAULT_PORT

    fake_ctx.settings["port"] = 70000
    assert sidecar_env.port == S.DEFAULT_PORT

    fake_ctx.settings["port"] = 0
    assert sidecar_env.port == S.DEFAULT_PORT


def test_autostart_can_be_disabled(sidecar_env, fake_ctx):
    fake_ctx.settings["autostart"] = False
    assert sidecar_env.autostart is False


def test_node_path_prefers_configured_existing(sidecar_env, fake_ctx, tmp_path):
    node = tmp_path / "node.exe"
    node.write_text("", encoding="utf-8")
    fake_ctx.settings["node_path"] = str(node)

    assert sidecar_env.node_path() == str(node)


def test_node_path_rejects_missing_configured(sidecar_env, fake_ctx, tmp_path):
    fake_ctx.settings["node_path"] = str(tmp_path / "missing.exe")
    assert sidecar_env.node_path() is None


def test_node_path_uses_path_lookup(sidecar_env, fake_ctx, monkeypatch):
    fake_ctx.settings.pop("node_path", None)
    monkeypatch.setattr(S.shutil, "which", lambda name: "/usr/bin/node")
    assert sidecar_env.node_path() == "/usr/bin/node"


def test_node_path_falls_back_to_well_known_location(sidecar_env, fake_ctx, monkeypatch):
    fake_ctx.settings.pop("node_path", None)
    monkeypatch.setattr(S.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: str(self).endswith("nodejs\\node.exe"))

    found = sidecar_env.node_path()

    assert found is not None and found.endswith("nodejs\\node.exe")


def test_node_path_returns_none_when_absent(sidecar_env, fake_ctx, monkeypatch):
    fake_ctx.settings.pop("node_path", None)
    monkeypatch.setattr(S.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert sidecar_env.node_path() is None


# ── 运行时状态 ─────────────────────────────────────────────────────────
def test_read_runtime_missing_file(sidecar_env):
    assert sidecar_env.read_runtime() is None


def test_read_runtime_corrupt_json(sidecar_env):
    sidecar_env.state_dir.mkdir(parents=True, exist_ok=True)
    sidecar_env.state_file.write_text("{ nope", encoding="utf-8")
    assert sidecar_env.read_runtime() is None


def test_read_runtime_wrong_shape(sidecar_env):
    sidecar_env.state_dir.mkdir(parents=True, exist_ok=True)
    sidecar_env.state_file.write_text("[1, 2, 3]", encoding="utf-8")
    assert sidecar_env.read_runtime() is None


def test_read_runtime_missing_fields(sidecar_env):
    sidecar_env.state_dir.mkdir(parents=True, exist_ok=True)
    sidecar_env.state_file.write_text('{"pid": 1}', encoding="utf-8")
    assert sidecar_env.read_runtime() is None


def test_read_runtime_dead_pid(sidecar_env, monkeypatch):
    sidecar_env.state_dir.mkdir(parents=True, exist_ok=True)
    sidecar_env.state_file.write_text('{"pid": 1, "port": 2, "token": "t"}', encoding="utf-8")
    monkeypatch.setattr(Sidecar, "_alive", lambda self, pid: False)

    assert sidecar_env.read_runtime() is None


def test_read_runtime_ok(runtime_state, monkeypatch):
    sc, rt = runtime_state
    monkeypatch.setattr(Sidecar, "_alive", lambda self, pid: True)
    loaded = sc.read_runtime()

    assert loaded == rt


def test_alive_prefers_popen_poll(runtime_state, monkeypatch):
    sc, _ = runtime_state
    sc._proc = FakePopen(pid=4242)
    assert sc._alive(4242) is True

    sc._proc._poll_result = 3
    assert sc._alive(4242) is False

    monkeypatch.setattr(S, "_pid_alive", lambda pid: pid == 7)
    assert sc._alive(7) is True
    assert sc._alive(8) is False


def test_health_variants(sidecar_env, monkeypatch):
    monkeypatch.setattr(S.api, "request", lambda *a, **k: (200, {"pid": 1}))
    assert sidecar_env.health(3081) == {"pid": 1}

    monkeypatch.setattr(S.api, "request", lambda *a, **k: (500, {"err": "x"}))
    assert sidecar_env.health(3081) is None

    monkeypatch.setattr(S.api, "request", lambda *a, **k: (200, ["not", "dict"]))
    assert sidecar_env.health(3081) is None

    def boom(*a, **k):
        raise OSError("连不上")

    monkeypatch.setattr(S.api, "request", boom)
    assert sidecar_env.health(3081) is None


def test_current_requires_healthy_answer(sidecar_env, monkeypatch):
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: None)
    assert sidecar_env.current() is None

    rt = make_runtime()
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: rt)
    monkeypatch.setattr(Sidecar, "health", lambda self, port: None)
    assert sidecar_env.current() is None

    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": rt.pid})
    assert sidecar_env.current() == rt


def test_status_reports_idle_and_running(sidecar_env, monkeypatch):
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    idle = sidecar_env.status()
    assert idle["running"] is False and idle["pid"] is None
    assert idle["scriptExists"] is True

    rt = make_runtime()
    monkeypatch.setattr(Sidecar, "current", lambda self: rt)
    running = sidecar_env.status()
    assert running["running"] is True
    assert running["pid"] == 4242
    assert running["port"] == 3081
    assert running["settingsUrl"] == rt.settings_url


# ── 邻居实例 ───────────────────────────────────────────────────────────
def test_probe_instances_filters_foreign_services(sidecar_env, monkeypatch):
    table = {
        3080: (200, {"name": "dsh-pet-standalone", "pid": "11", "pluginRoot": "E:\\a"}),
        3081: (200, {"name": "dsh-pet-standalone", "pid": "abc", "pluginRoot": ""}),
        3082: (200, {"name": "别的东西", "pid": 1}),
        3083: (200, ["not", "a", "dict"]),
        3084: (500, None),
    }

    def fake_request(port, method, path, **kwargs):
        if port == 3085:
            raise OSError("端口上没人")
        return table.get(port, (404, None))

    monkeypatch.setattr(S.api, "request", fake_request)

    found = sidecar_env.probe_instances(span=5)

    assert found == [
        {"port": 3080, "pid": 11, "root": "E:\\a"},
        {"port": 3081, "pid": 0, "root": ""},
    ]


def test_leftover_instances_excludes_own_pid(sidecar_env, monkeypatch):
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: make_runtime(pid=7))
    monkeypatch.setattr(
        Sidecar,
        "probe_instances",
        lambda self, span=S.PROBE_SPAN: [{"port": 3080, "pid": 7, "root": ""}, {"port": 3081, "pid": 8, "root": ""}],
    )

    assert sidecar_env.leftover_instances() == [{"port": 3081, "pid": 8, "root": ""}]

    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: None)
    assert len(sidecar_env.leftover_instances()) == 2


def test_pid_alive_and_port_open_delegate(sidecar_env, monkeypatch):
    monkeypatch.setattr(S, "_pid_alive", lambda pid: pid == 42)
    monkeypatch.setattr(S, "_port_open", lambda port: port == 3081)

    assert sidecar_env.pid_alive(42) is True
    assert sidecar_env.pid_alive(43) is False
    assert sidecar_env.port_open(3081) is True
    assert sidecar_env.port_open(3082) is False


def test_terminate_pid_uses_force_kill(sidecar_env, monkeypatch):
    killed = []
    monkeypatch.setattr(Sidecar, "_force_kill", lambda self, pid: killed.append(pid))

    sidecar_env.terminate_pid(999)

    assert killed == [999]


def test_real_pid_and_port_probes():
    assert S._pid_alive(-1) is False
    assert S._pid_alive(os.getpid()) is True
    assert S._port_open(0) is False
    assert S._port_open(70000) is False


def test_port_probe_sees_listening_socket():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        port = sock.getsockname()[1]
        assert S._port_open(port) is True
    finally:
        sock.close()
    assert S._port_open(port) is False


# ── Electron 复用 ──────────────────────────────────────────────────────
def test_shared_electron_hint_finds_dsh_binary(tmp_path, monkeypatch):
    home = tmp_path / "user"
    exe = home / ".dsh" / "electron" / "electron.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.setattr(S.sys, "platform", "win32")

    assert S._shared_electron_hint() == exe


def test_shared_electron_hint_honours_dsh_home(tmp_path, monkeypatch):
    custom = tmp_path / "custom-dsh"
    exe = custom / "electron" / "electron.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("DSH_HOME", str(custom))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "someone-else"))
    monkeypatch.setattr(S.sys, "platform", "win32")

    assert S._shared_electron_hint() == exe


def test_shared_electron_hint_platform_layouts(tmp_path, monkeypatch):
    home = tmp_path / "mac-user"
    mac = home / ".dsh" / "electron" / "Electron.app" / "Contents" / "MacOS" / "Electron"
    mac.parent.mkdir(parents=True)
    mac.write_bytes(b"#!/bin/sh")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("DSH_HOME", raising=False)

    monkeypatch.setattr(S.sys, "platform", "darwin")
    assert S._shared_electron_hint() == mac

    linux = home / ".dsh" / "electron" / "electron"
    linux.write_bytes(b"#!/bin/sh")
    monkeypatch.setattr(S.sys, "platform", "linux")
    assert S._shared_electron_hint() == linux


def test_shared_electron_hint_skips_empty_and_errors(tmp_path, monkeypatch):
    home = tmp_path / "user"
    exe = home / ".dsh" / "electron" / "electron.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.setattr(S.sys, "platform", "win32")

    assert S._shared_electron_hint() is None  # 空文件不算

    exe.write_bytes(b"MZ")

    def broken_stat(self):
        raise OSError("权限不足")

    monkeypatch.setattr(Path, "stat", broken_stat)
    assert S._shared_electron_hint() is None


def test_start_passes_shared_electron_to_child(sidecar_env, monkeypatch, tmp_path):
    home = tmp_path / "user"
    exe = home / ".dsh" / "electron" / "electron.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.delenv("DSH_PET_ELECTRON_PATH", raising=False)
    monkeypatch.setattr(S.sys, "platform", "win32")

    proc = FakePopen(pid=4242)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(S.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [])
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: make_runtime(pid=4242))
    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": 4242})

    result = sidecar_env.start()

    assert result["electron"] == str(exe)
    assert captured["env"]["DSH_PET_ELECTRON_PATH"] == str(exe)
    assert captured["env"]["DSH_HOME"] == str(sidecar_env.home)


def test_start_keeps_explicit_electron_env(sidecar_env, monkeypatch, tmp_path):
    home = tmp_path / "user"
    exe = home / ".dsh" / "electron" / "electron.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DSH_PET_ELECTRON_PATH", "C:/custom/electron.exe")

    proc = FakePopen(pid=4242)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(S.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [])
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: make_runtime(pid=4242))
    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": 4242})

    sidecar_env.start()

    assert captured["env"]["DSH_PET_ELECTRON_PATH"] == "C:/custom/electron.exe"


# ── 启动 ───────────────────────────────────────────────────────────────
def test_start_reuses_running_instance(sidecar_env, monkeypatch):
    rt = make_runtime()
    monkeypatch.setattr(Sidecar, "current", lambda self: rt)
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [{"port": 3080, "pid": 8, "root": "x"}])

    result = sidecar_env.start()

    assert result["ok"] is True and result["reused"] is True
    assert result["others"] == [{"port": 3080, "pid": 8, "root": "x"}]


def test_start_errors_when_script_missing(sidecar_env, monkeypatch, tmp_path):
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [])
    sidecar_env.root = tmp_path / "plugins" / "dsh-pet"
    sidecar_env.script = tmp_path / "nope" / "standalone.js"
    monkeypatch.setattr(Sidecar, "npm_path", lambda self: None)

    with pytest.raises(SidecarError, match="没有找到 dsh-pet 运行时"):
        sidecar_env.start()


def test_start_errors_when_node_missing(sidecar_env, monkeypatch):
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [])
    monkeypatch.setattr(Sidecar, "node_path", lambda self: None)

    with pytest.raises(SidecarError, match="找不到 node"):
        sidecar_env.start()


def test_start_launches_process_and_waits_for_health(sidecar_env, monkeypatch, caplog):
    proc = FakePopen(pid=4242)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(S.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(
        Sidecar, "leftover_instances", lambda self: [{"port": 3080, "pid": 8, "root": "E:\\DSH"}]
    )
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: make_runtime(pid=4242))
    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": 4242})

    with caplog.at_level("WARNING"):
        result = sidecar_env.start(cleanup_on_exit=False)

    assert result["ok"] is True and result["reused"] is False
    assert captured["argv"][1:] == [str(sidecar_env.script), "--port", str(sidecar_env.port)]
    assert captured["kwargs"]["cwd"] == str(sidecar_env.package_dir)
    assert captured["kwargs"]["env"]["DSH_HOME"] == str(sidecar_env.home)
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert sidecar_env._proc is proc
    assert sidecar_env._cleanup_on_exit is False
    assert sidecar_env.log_file.exists()
    assert any("别的实例" in record.message for record in caplog.records)


@pytest.mark.skipif(sys.platform != "win32", reason="CREATE_NO_WINDOW 只在 Windows 上有意义")
def test_start_passes_create_no_window_on_windows(sidecar_env, monkeypatch):
    proc = FakePopen(pid=4242)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(S.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [])
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: make_runtime(pid=4242))
    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": 4242})

    sidecar_env.start()

    assert captured["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_start_skips_create_no_window_off_windows(sidecar_env, monkeypatch):
    proc = FakePopen(pid=4242)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(S.sys, "platform", "linux")
    monkeypatch.setattr(S.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(Sidecar, "node_path", lambda self: "node")
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [])
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: make_runtime(pid=4242))
    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": 4242})

    sidecar_env.start()

    assert captured["creationflags"] == 0


def test_start_reports_immediate_exit_with_log_tail(sidecar_env, monkeypatch):
    proc = FakePopen(pid=4242, poll_result=7)
    sidecar_env.state_dir.mkdir(parents=True, exist_ok=True)
    sidecar_env.log_file.write_text("第一行\n第二行\n", encoding="utf-8")
    monkeypatch.setattr(S.subprocess, "Popen", lambda argv, **kwargs: proc)
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [])

    with pytest.raises(SidecarError) as info:
        sidecar_env.start()

    assert "退出码 7" in str(info.value)
    assert "第二行" in str(info.value)
    assert sidecar_env._proc is None


def test_start_kills_process_that_never_becomes_healthy(sidecar_env, monkeypatch):
    proc = FakePopen(pid=4242)
    monkeypatch.setattr(S.subprocess, "Popen", lambda argv, **kwargs: proc)
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [])
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: None)

    with pytest.raises(SidecarError) as info:
        sidecar_env.start(timeout=0.01)

    assert "仍未就绪" in str(info.value)
    assert proc.terminated == 1
    assert sidecar_env._proc is None
    assert sidecar_env._cleanup_on_exit is False


def test_start_waits_through_mismatched_runtime(sidecar_env, monkeypatch):
    """状态文件写的是上一只的 pid 时不能算就绪。"""
    proc = FakePopen(pid=4242)
    monkeypatch.setattr(S.subprocess, "Popen", lambda argv, **kwargs: proc)
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [])
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: make_runtime(pid=999))
    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": 999})
    monkeypatch.setattr(S.time, "sleep", lambda seconds: None)

    with pytest.raises(SidecarError, match="仍未就绪"):
        sidecar_env.start(timeout=0.05)

    assert proc.terminated == 1


# ── 收尾 ───────────────────────────────────────────────────────────────
def test_stop_without_runtime_and_without_proc(sidecar_env):
    result = sidecar_env.stop()

    assert result == {"ok": True, "stopped": False, "reason": "not-running"}


def test_stop_kills_owned_proc_when_state_file_missing(sidecar_env):
    proc = FakePopen(pid=555)
    sidecar_env._proc = proc

    result = sidecar_env.stop()

    assert result == {"ok": True, "stopped": True, "pid": 555, "forced": True}
    assert proc.terminated == 1
    assert sidecar_env._proc is None


def test_stop_requests_graceful_quit(runtime_state, monkeypatch):
    sc, rt = runtime_state
    sc._proc = FakePopen(pid=rt.pid)
    monkeypatch.setattr(Sidecar, "_alive", seq_alive(True, False))
    calls = []
    monkeypatch.setattr(S.api, "request", lambda *a, **k: calls.append(k) or (200, {"ok": True}))

    result = sc.stop()

    assert result == {"ok": True, "stopped": True, "pid": rt.pid}
    assert calls and calls[0]["token"] == rt.token
    assert sc._proc is None and sc._cleanup_on_exit is False


def test_stop_ignores_quit_transport_error(runtime_state, monkeypatch):
    sc, rt = runtime_state
    sc._proc = FakePopen(pid=rt.pid)
    monkeypatch.setattr(Sidecar, "_alive", seq_alive(True, False))

    def boom(*a, **k):
        raise OSError("响应没送达")

    monkeypatch.setattr(S.api, "request", boom)

    assert sc.stop()["stopped"] is True


def test_stop_refuses_unverified_pid(runtime_state, monkeypatch):
    sc, rt = runtime_state
    monkeypatch.setattr(Sidecar, "_owned_by_runtime", lambda self, runtime: False)
    monkeypatch.setattr(Sidecar, "_alive", lambda self, pid: True)
    monkeypatch.setattr(S.api, "request", lambda *a, **k: (200, None))

    result = sc.stop(timeout=0)

    assert result["reason"] == "unverified-pid" and result["stopped"] is False
    assert not sc.state_file.exists()


def test_stop_unverified_pid_survives_unlink_error(runtime_state, monkeypatch):
    sc, _ = runtime_state
    monkeypatch.setattr(Sidecar, "_owned_by_runtime", lambda self, runtime: False)
    monkeypatch.setattr(Sidecar, "_alive", lambda self, pid: True)
    monkeypatch.setattr(S.api, "request", lambda *a, **k: (200, None))
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("占用中")))

    assert sc.stop(timeout=0)["reason"] == "unverified-pid"


def test_stop_force_kills_when_graceful_exit_times_out(runtime_state, monkeypatch):
    sc, rt = runtime_state
    sc._proc = FakePopen(pid=rt.pid)
    monkeypatch.setattr(Sidecar, "_alive", lambda self, pid: True)
    monkeypatch.setattr(S.api, "request", lambda *a, **k: (200, None))
    killed = []
    monkeypatch.setattr(Sidecar, "_force_kill", lambda self, pid: killed.append(pid))

    result = sc.stop(timeout=0)

    assert result == {"ok": True, "stopped": True, "pid": rt.pid, "forced": True}
    assert killed == [rt.pid]


def test_owned_by_runtime_compares_pid(sidecar_env, monkeypatch):
    rt = make_runtime()
    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": rt.pid})
    assert sidecar_env._owned_by_runtime(rt) is True

    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": str(rt.pid)})
    assert sidecar_env._owned_by_runtime(rt) is True

    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": rt.pid + 1})
    assert sidecar_env._owned_by_runtime(rt) is False

    monkeypatch.setattr(Sidecar, "health", lambda self, port: None)
    assert sidecar_env._owned_by_runtime(rt) is False

    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": "不是数字"})
    assert sidecar_env._owned_by_runtime(rt) is False


# ── 强杀 ───────────────────────────────────────────────────────────────
def test_pid_alive_windows_syscalls(monkeypatch):
    """Windows 分支靠内核问答，权限不足也要当“活着”，免得重复拉起。"""
    import types

    def make_ctypes(handle: int, last_error: int, exit_ok: bool = True, code: int = 259):
        kernel32 = types.SimpleNamespace(
            OpenProcess=lambda access, inherit, pid: handle,
            GetLastError=lambda: last_error,
            GetExitCodeProcess=lambda handle, out: exit_ok,
            CloseHandle=lambda handle: None,
        )
        return types.SimpleNamespace(
            windll=types.SimpleNamespace(kernel32=kernel32),
            c_ulong=lambda value=0: types.SimpleNamespace(value=code),
            byref=lambda obj: obj,
        )

    monkeypatch.setattr(S.sys, "platform", "win32")

    monkeypatch.setitem(sys.modules, "ctypes", make_ctypes(handle=0, last_error=5))
    assert S._pid_alive(123) is True  # 拒绝访问 → 视为活着

    monkeypatch.setitem(sys.modules, "ctypes", make_ctypes(handle=0, last_error=87))
    assert S._pid_alive(123) is False  # 参数错误 → 进程不存在

    monkeypatch.setitem(sys.modules, "ctypes", make_ctypes(handle=9, last_error=0, exit_ok=False))
    assert S._pid_alive(123) is True

    monkeypatch.setitem(sys.modules, "ctypes", make_ctypes(handle=9, last_error=0, exit_ok=True, code=259))
    assert S._pid_alive(123) is True

    monkeypatch.setitem(sys.modules, "ctypes", make_ctypes(handle=9, last_error=0, exit_ok=True, code=1))
    assert S._pid_alive(123) is False


def test_pid_alive_posix_branch(monkeypatch):
    """非 Windows 走 os.kill：找不到 / 没权限 / 其它错误各有各的结论。"""
    monkeypatch.setattr(S.sys, "platform", "linux")

    monkeypatch.setattr(S.os, "kill", lambda pid, sig: None)
    assert S._pid_alive(321) is True

    def missing(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(S.os, "kill", missing)
    assert S._pid_alive(321) is False

    def denied(pid, sig):
        raise PermissionError

    monkeypatch.setattr(S.os, "kill", denied)
    assert S._pid_alive(321) is True

    def generic(pid, sig):
        raise OSError("别的问题")

    monkeypatch.setattr(S.os, "kill", generic)
    assert S._pid_alive(321) is False

    def weird(pid, sig):
        raise SystemError("CPython 内部错误")

    monkeypatch.setattr(S.os, "kill", weird)
    assert S._pid_alive(321) is False


def test_port_probe_swallows_socket_errors(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("socket 子系统不可用")

    monkeypatch.setattr(S.socket, "socket", boom)

    assert S._port_open(3081) is False


def test_stop_retries_before_giving_up(runtime_state, monkeypatch):
    sc, rt = runtime_state
    sc._proc = FakePopen(pid=rt.pid)
    monkeypatch.setattr(Sidecar, "_alive", seq_alive(True, True, False))
    monkeypatch.setattr(S.api, "request", lambda *a, **k: (200, None))
    slept = []
    monkeypatch.setattr(S.time, "sleep", lambda seconds: slept.append(seconds))

    result = sc.stop(timeout=5)

    assert result["stopped"] is True
    assert slept == [0.2]


def test_force_kill_terminates_own_proc_gracefully(sidecar_env):
    proc = FakePopen(pid=555)
    sidecar_env._proc = proc

    sidecar_env._force_kill(555)

    assert proc.terminated == 1
    assert proc.killed == 0
    assert proc.waited == [3]


def test_force_kill_escalates_to_kill(sidecar_env):
    proc = FakePopen(pid=555)
    proc.wait_error = subprocess.TimeoutExpired(cmd="x", timeout=3)
    proc.wait_error_times = 2
    sidecar_env._proc = proc

    sidecar_env._force_kill(555)

    assert proc.terminated == 1
    assert proc.killed == 1


def test_force_kill_uses_taskkill_on_windows(sidecar_env, monkeypatch, tmp_path):
    sidecar_env.state_dir.mkdir(parents=True, exist_ok=True)
    sidecar_env.state_file.write_text("{}", encoding="utf-8")
    recorded = []
    monkeypatch.setattr(S.sys, "platform", "win32")
    monkeypatch.setattr(S.subprocess, "run", lambda argv, **kwargs: recorded.append(argv))

    sidecar_env._force_kill(777)

    assert recorded == [["taskkill", "/PID", "777", "/T", "/F"]]
    assert not sidecar_env.state_file.exists()


def test_force_kill_uses_sigterm_elsewhere(sidecar_env, monkeypatch):
    monkeypatch.setattr(S.sys, "platform", "linux")
    killed = []
    monkeypatch.setattr(S.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    sidecar_env._force_kill(888)

    assert killed == [(888, 15)]


def test_force_kill_swallows_os_errors(sidecar_env, monkeypatch):
    monkeypatch.setattr(S.sys, "platform", "linux")

    def boom(pid, sig):
        raise ProcessLookupError("已经没了")

    monkeypatch.setattr(S.os, "kill", boom)

    sidecar_env._force_kill(999)  # 不抛


def test_force_kill_swallows_unlink_error(sidecar_env):
    class StubPath:
        def unlink(self):
            raise OSError("占用中")

    sidecar_env.state_file = StubPath()
    sidecar_env.force_used = True

    sidecar_env._force_kill(1000)  # 不抛


# ── 日志尾巴 / 退出钩子 ────────────────────────────────────────────────
def test_log_tail_reads_launch_log(sidecar_env):
    sidecar_env.state_dir.mkdir(parents=True, exist_ok=True)
    sidecar_env.log_file.write_text("\n".join(f"line{i}" for i in range(10)), encoding="utf-8")

    tail = sidecar_env._log_tail(lines=3)

    assert tail.startswith("hermes-launch.log:")
    assert "line9" in tail and "line0" not in tail


def test_log_tail_falls_back_to_dsh_log(sidecar_env):
    sidecar_env.state_dir.mkdir(parents=True, exist_ok=True)
    sidecar_env.log_file.write_text("   \n", encoding="utf-8")
    sidecar_env.dsh_log.write_text("dsl 的日志", encoding="utf-8")

    assert sidecar_env._log_tail().startswith("logs.txt:")


def test_log_tail_without_logs(sidecar_env):
    assert sidecar_env._log_tail() == "（没有日志）"


def test_stop_if_owned_skips_when_not_owner(sidecar_env, monkeypatch):
    called = []
    monkeypatch.setattr(Sidecar, "stop", lambda self, timeout=S.STOP_TIMEOUT: called.append(1))

    sidecar_env._cleanup_on_exit = False
    sidecar_env.stop_if_owned()
    assert called == []

    sidecar_env._cleanup_on_exit = True
    sidecar_env.stop_if_owned()
    assert called == [1]


def test_stop_if_owned_swallows_errors(sidecar_env, monkeypatch):
    def boom(self, timeout=S.STOP_TIMEOUT):
        raise RuntimeError("收尾失败也不能拖垮退出")

    monkeypatch.setattr(Sidecar, "stop", boom)
    sidecar_env._cleanup_on_exit = True

    sidecar_env.stop_if_owned()  # 不抛


# ── 市场安装：插件目录里没有自带运行时，首次启动按固定版本自举 ───────────────
def _isolated(sidecar_env, tmp_path, monkeypatch):
    """摘掉仓库同级目录/环境变量，只留下插件目录与自举目录。

    conftest 的 package_tree 就放在 tmp_path/dsh-pet，直接拿 tmp_path 做父目录会被
    “同级目录”候选命中；这里套一层 plugins/ 让候选全落空。
    """
    monkeypatch.delenv("DSH_PET_HOME", raising=False)
    sidecar_env.root = tmp_path / "plugins" / "dsh-pet"
    sidecar_env.runtime_dir = tmp_path / "plugin-data" / "runtime"
    sidecar_env.package_dir = sidecar_env._resolve_package_dir()
    sidecar_env.script = sidecar_env.package_dir / "lib" / "standalone.js"
    return sidecar_env


def test_npm_path_prefers_npm_then_cmd(sidecar_env, monkeypatch):
    calls = []

    def which(name):
        calls.append(name)
        return None

    monkeypatch.setattr(S.shutil, "which", which)
    assert sidecar_env.npm_path() is None
    assert calls == ["npm", "npm.cmd"]

    monkeypatch.setattr(S.shutil, "which", lambda name: "/usr/bin/npm.cmd" if name == "npm.cmd" else None)
    assert sidecar_env.npm_path() == "/usr/bin/npm.cmd"


def test_ensure_runtime_returns_existing_package(sidecar_env):
    assert sidecar_env.ensure_runtime() == sidecar_env.package_dir


def test_ensure_runtime_installs_pinned_version(sidecar_env, tmp_path, monkeypatch):
    sc = _isolated(sidecar_env, tmp_path, monkeypatch)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        # npm ci 只认 cwd 里的 package.json + package-lock.json，两份都得先铺好。
        captured["manifest"] = (sc.runtime_dir / "package.json").is_file()
        captured["lockfile"] = (sc.runtime_dir / "package-lock.json").is_file()
        pkg = sc.runtime_dir / "node_modules" / "dsh-pet"
        (pkg / "lib").mkdir(parents=True)
        (pkg / "lib" / "standalone.js").write_text("// stub\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(S.subprocess, "run", fake_run)
    monkeypatch.setattr(Sidecar, "npm_path", lambda self: "C:/node/npm.cmd")

    path = sc.ensure_runtime()

    assert path == sc.runtime_dir / "node_modules" / "dsh-pet"
    assert sc.script.exists()  # 装完重新解析了 script
    assert captured["manifest"] and captured["lockfile"]
    argv = captured["argv"]
    assert argv[0] == "C:/node/npm.cmd"
    # 装的是锁文件（npm ci），而不是按 ^ 范围现解析（npm install <包>）。
    assert argv[1] == "ci"
    assert "--prefix" not in argv
    assert not any(arg.startswith(f"{S.RUNTIME_PACKAGE}@") for arg in argv)
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == str(sc.runtime_dir)
    assert kwargs["timeout"] == S.RUNTIME_NPM_TIMEOUT
    # 子进程环境是白名单，不是整份继承。
    assert "env" in kwargs and kwargs["env"].get("PATH") == os.environ.get("PATH")


def test_ensure_runtime_requires_lockfile(sidecar_env, tmp_path, monkeypatch):
    sc = _isolated(sidecar_env, tmp_path, monkeypatch)
    monkeypatch.setattr(Sidecar, "npm_path", lambda self: "npm")
    monkeypatch.setattr(S, "RUNTIME_LOCK_DIR", tmp_path / "missing-lock")
    ran = []
    monkeypatch.setattr(S.subprocess, "run", lambda argv, **kwargs: ran.append(argv))

    with pytest.raises(SidecarError, match="缺运行时锁文件"):
        sc.ensure_runtime()
    assert ran == []  # 没有锁文件就不该去跑 npm


def test_lockfile_pins_every_dependency_exactly():
    """锁文件必须全树精确版本 + integrity：维护者要求的正是“依赖树不再每次首启重新解析”。"""
    lock = json.loads((S.RUNTIME_LOCK_DIR / "package-lock.json").read_text("utf-8"))
    assert lock["lockfileVersion"] >= 3
    root = lock["packages"][""]
    assert root["dependencies"][S.RUNTIME_PACKAGE] == S.RUNTIME_VERSION
    installed = {k: v for k, v in lock["packages"].items() if k}
    assert installed, "锁文件里没有装出来的包"
    for name, info in installed.items():
        assert info.get("version"), f"{name} 缺版本"
        assert not any(c in info["version"] for c in "^~<>=*|"), f"{name} 不是精确版本：{info['version']}"
        assert info.get("integrity", "").startswith("sha512-"), f"{name} 缺 integrity"
        assert info.get("resolved", "").startswith("https://registry.npmjs.org/"), f"{name} 指向第三方源"


def test_ensure_runtime_without_npm(sidecar_env, tmp_path, monkeypatch):
    sc = _isolated(sidecar_env, tmp_path, monkeypatch)
    monkeypatch.setattr(Sidecar, "npm_path", lambda self: None)

    with pytest.raises(SidecarError, match="也找不到 npm"):
        sc.ensure_runtime()


def test_ensure_runtime_reports_npm_exit_code(sidecar_env, tmp_path, monkeypatch):
    sc = _isolated(sidecar_env, tmp_path, monkeypatch)
    monkeypatch.setattr(Sidecar, "npm_path", lambda self: "npm")
    monkeypatch.setattr(
        S.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="EACCES 权限不足"),
    )

    with pytest.raises(SidecarError, match="npm 退出码 1") as exc:
        sc.ensure_runtime()
    assert "EACCES" in str(exc.value)


def test_ensure_runtime_falls_back_to_stdout_on_failure(sidecar_env, tmp_path, monkeypatch):
    sc = _isolated(sidecar_env, tmp_path, monkeypatch)
    monkeypatch.setattr(Sidecar, "npm_path", lambda self: "npm")
    monkeypatch.setattr(
        S.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=2, stdout="registry 不可达", stderr=""),
    )

    with pytest.raises(SidecarError, match="npm 退出码 2") as exc:
        sc.ensure_runtime()
    assert "registry 不可达" in str(exc.value)


def test_ensure_runtime_rejects_empty_install(sidecar_env, tmp_path, monkeypatch):
    sc = _isolated(sidecar_env, tmp_path, monkeypatch)
    monkeypatch.setattr(Sidecar, "npm_path", lambda self: "npm")
    monkeypatch.setattr(
        S.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with pytest.raises(SidecarError, match="npm 装完仍然找不到"):
        sc.ensure_runtime()


def test_ensure_runtime_wraps_oserror(sidecar_env, tmp_path, monkeypatch):
    sc = _isolated(sidecar_env, tmp_path, monkeypatch)
    monkeypatch.setattr(Sidecar, "npm_path", lambda self: "npm")

    def boom(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(S.subprocess, "run", boom)

    with pytest.raises(SidecarError, match="安装 dsh-pet 运行时失败：no such file"):
        sc.ensure_runtime()


def test_ensure_runtime_wraps_timeout(sidecar_env, tmp_path, monkeypatch):
    sc = _isolated(sidecar_env, tmp_path, monkeypatch)
    monkeypatch.setattr(Sidecar, "npm_path", lambda self: "npm")

    def slow(argv, **kwargs):
        raise S.subprocess.TimeoutExpired(cmd="npm", timeout=1)

    monkeypatch.setattr(S.subprocess, "run", slow)

    with pytest.raises(SidecarError, match="安装 dsh-pet 运行时失败"):
        sc.ensure_runtime()


# ── 子进程环境白名单 ────────────────────────────────────────────────────


def test_child_env_drops_host_secrets(monkeypatch):
    """宿主环境里的各家 key 一个都不能传给 node/Electron。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("HERMES_TELEGRAM_TOKEN", "bot-secret")
    monkeypatch.setenv("DSH_PET_ELECTRON_MIRROR", "https://mirror.example/electron")

    env = S._child_env()

    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "HERMES_TELEGRAM_TOKEN" not in env
    # 路径这类“能跑起来”的变量要留下，否则 node 都起不来。
    assert env.get("PATH") == os.environ.get("PATH")
    # 显式 opt-in 的运行时定位开关照传。
    assert env["DSH_PET_ELECTRON_MIRROR"] == "https://mirror.example/electron"


def test_child_env_passes_npm_config_only_when_asked(monkeypatch):
    monkeypatch.setenv("npm_config_registry", "https://registry.npmmirror.com")
    monkeypatch.setenv("NPM_CONFIG_PROXY", "http://127.0.0.1:7897")

    def env_get(env: dict[str, str], name: str) -> str | None:
        return next((value for key, value in env.items() if key.upper() == name.upper()), None)

    node_env = S._child_env()
    assert not any(key.upper().startswith("NPM_CONFIG_") for key in node_env)

    npm_env = S._child_env(npm=True)
    assert env_get(npm_env, "npm_config_registry") == "https://registry.npmmirror.com"
    assert env_get(npm_env, "NPM_CONFIG_PROXY") == "http://127.0.0.1:7897"


def test_child_env_extra_overrides_and_skips_empty(monkeypatch):
    monkeypatch.setenv("DSH_HOME", "C:/inherited")
    env = S._child_env({"DSH_HOME": "D:/own", "DSH_PET_ELECTRON_PATH": ""})

    assert env["DSH_HOME"] == "D:/own"  # 显式给的赢过继承的
    assert "DSH_PET_ELECTRON_PATH" not in env  # 空值不落进环境


def test_start_child_env_excludes_host_secrets(sidecar_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")

    proc = FakePopen(pid=4242)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(S.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(Sidecar, "current", lambda self: None)
    monkeypatch.setattr(Sidecar, "leftover_instances", lambda self: [])
    monkeypatch.setattr(Sidecar, "read_runtime", lambda self: make_runtime(pid=4242))
    monkeypatch.setattr(Sidecar, "health", lambda self, port: {"pid": 4242})

    sidecar_env.start()

    env = captured["env"]
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["DSH_HOME"] == str(sidecar_env.home)
    assert env.get("PATH") == os.environ.get("PATH")