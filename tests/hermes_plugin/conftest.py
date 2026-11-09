"""dsh-pet Hermes 插件的测试夹具。

插件在仓库里是 ``hermes/`` 这个包，所以把仓库根塞进 ``sys.path`` 后按
``hermes.hermes_adapter.*`` 导入，跟运行时 ``hermes plugins`` 的加载结果一致。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes.hermes_adapter import sidecar as sidecar_mod  # noqa: E402


class FakeCtx:
    """够用的插件上下文：配置、钩子、CLI 命令、卸载回调都能记下来。"""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = dict(settings or {})
        self.hooks: list[tuple[str, Any]] = []
        self.cli_commands: list[dict[str, Any]] = []
        self.unload_callbacks: list[Any] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def register_hook(self, name: str, handler: Any) -> None:
        self.hooks.append((name, handler))

    def register_cli_command(self, **kwargs: Any) -> None:
        self.cli_commands.append(kwargs)

    def on_unload(self, callback: Any) -> None:
        self.unload_callbacks.append(callback)


@pytest.fixture
def fake_ctx() -> FakeCtx:
    return FakeCtx()


@pytest.fixture
def package_tree(tmp_path: Path) -> Path:
    """一个最小可用的 dsh-pet 包目录（只要求 lib/standalone.js 存在）。"""
    pkg = tmp_path / "dsh-pet"
    (pkg / "lib").mkdir(parents=True)
    (pkg / "lib" / "standalone.js").write_text("// stub\n", encoding="utf-8")
    return pkg


@pytest.fixture
def sidecar_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package_tree: Path, fake_ctx: FakeCtx):
    """把插件数据目录和 dsh-pet 包位置都指到 tmp，构造一个可用的 Sidecar。"""
    data = tmp_path / "plugin-data" / "dsh-pet-hermes"
    monkeypatch.setattr(sidecar_mod, "_plugin_data_dir", lambda: data)
    monkeypatch.setenv("DSH_PET_HOME", str(package_tree))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    sc = sidecar_mod.Sidecar(fake_ctx)
    return sc


def make_runtime(pid: int = 4242, port: int = 3081) -> Any:
    return sidecar_mod.Runtime(
        pid=pid,
        port=port,
        token="tok-secret",
        settings_url=f"http://127.0.0.1:{port}/dsh-pet-7340/settings?token=tok-secret",
        plugin_root="E:\\dsh-pr\\dsh-pet",
    )


class FakePopen:
    """替身进程：只实现 Sidecar 用到的那几个方法。"""

    def __init__(self, pid: int = 4242, poll_result: int | None = None) -> None:
        self.pid = pid
        self._poll_result = poll_result
        self.terminated = 0
        self.killed = 0
        self.waited: list[float | None] = []
        self.wait_error: BaseException | None = None
        self.wait_error_times = 0

    def poll(self) -> int | None:
        return self._poll_result

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1

    def wait(self, timeout: float | None = None) -> int:
        self.waited.append(timeout)
        if self.wait_error is not None and self.wait_error_times != 0:
            self.wait_error_times -= 1
            raise self.wait_error
        return 0


@pytest.fixture
def fake_popen():
    return FakePopen


@pytest.fixture
def runtime_state(sidecar_env, tmp_path: Path):
    """写一份状态文件，返回 (sidecar, runtime)。"""
    rt = make_runtime()
    sidecar_env.state_dir.mkdir(parents=True, exist_ok=True)
    sidecar_env.state_file.write_text(
        json.dumps(
            {
                "pid": rt.pid,
                "port": rt.port,
                "token": rt.token,
                "settingsUrl": rt.settings_url,
                "pluginRoot": rt.plugin_root,
            }
        ),
        encoding="utf-8",
    )
    return sidecar_env, rt


def ns(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)