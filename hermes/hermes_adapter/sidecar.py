"""dsh-pet 独立运行时的进程管理。

Hermes 侧只做三件事：用独立的 ``DSH_HOME`` 拉起 dsh-pet 现成的独立模式进程（``lib/standalone.js``）、
按运行时状态文件确认它真的起来了、在插件被停用/卸载时把它关掉。桌宠内部逻辑一律不重写。

始终成立：
- 只认自己数据目录下的 ``standalone-runtime.json``，绝不按端口盲杀别的实例；
- 启动幂等：已经在跑就直接复用，不会再起第二只；
- 任何一步失败都抛 ``SidecarError`` 或返回带原因的状态，不 silence 掉“没起来”这件事。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import api

_log = logging.getLogger("dsh_pet_hermes")

PLUGIN_ID = "dsh-pet-hermes"
DEFAULT_PORT = 3080
#: 探测邻居实例的端口跨度（含起始端口本身）；独立模式被占用时逐端口顺延。
PROBE_SPAN = 10

#: 首次拉起时等待“服务监听 + 健康检查”的总时长；Electron 下载不在这条路径上等。
START_TIMEOUT = 45.0
#: 收尾等待进程退出的时长（正常是 POST /quit 后的优雅退出）。
STOP_TIMEOUT = 15.0


class SidecarError(RuntimeError):
    """拉起或收尾失败，消息可直接给用户看。"""


@dataclass(frozen=True)
class Runtime:
    """运行时状态文件的内容（只取这里用得上的字段）。"""

    pid: int
    port: int
    token: str
    settings_url: str
    plugin_root: str


def _pid_alive(pid: int) -> bool:
    """进程是否还活着；权限不足按“活着”处理，避免误判后重复拉起。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Windows 上直接问内核：OpenProcess + GetExitCodeProcess 比 os.kill(pid, 0) 靠谱
        # （后者在 CPython 里对某些状态会抛 SystemError）。
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_ACCESS_DENIED = 5：进程还在，只是不让我们查
            return kernel32.GetLastError() == 5
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, SystemError):
        return False
    return True


def _port_open(port: int, timeout: float = 0.2) -> bool:
    """回环端口上还有人在监听吗（只连不写，用来判断实例是否真的退干净）。"""
    if not 1 <= port <= 65535:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def _electron_rel_path() -> Path:
    """$DSH_HOME/electron 下可执行文件的相对位置（与 dsh-pet 的 electron.ts 一致）。"""
    if sys.platform == "win32":
        return Path("electron.exe")
    if sys.platform == "darwin":
        return Path("Electron.app") / "Contents" / "MacOS" / "Electron"
    return Path("electron")


def _shared_electron_hint() -> Optional[Path]:
    """本机 DSH 已经下过 Electron 时直接借用，别让插件数据目录再存一份 350MB。

    只给「提示」：dsh-pet 的解析器只认存在且非空的文件，指错了它会自己回落去下载，
    所以这里判断失败也不影响启动，只是多下一次。
    """
    base = os.environ.get("DSH_HOME", "").strip()
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    candidate = (Path(base) if base else home / ".dsh") / "electron" / _electron_rel_path()
    try:
        return candidate if candidate.is_file() and candidate.stat().st_size > 0 else None
    except OSError:
        return None


def _plugin_data_dir() -> Path:
    """Hermes 的 profile 专属插件数据根；拿不到时退回 HERMES_HOME 约定路径。"""
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore

        return plugin_data_dir(PLUGIN_ID)
    except Exception:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        path = Path(home) / "plugin-data" / PLUGIN_ID
        path.mkdir(parents=True, exist_ok=True)
        return path


class Sidecar:
    """一只 Hermes 专属的 dsh-pet 进程。"""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._cleanup_on_exit = False

        self.root = Path(__file__).resolve().parents[1]  # = 插件根（hermes/）
        self.package_dir = self._resolve_package_dir()
        self.script = self.package_dir / "lib" / "standalone.js"
        self.home = _plugin_data_dir() / "runtime-home"
        self.state_dir = self.home / "dsh-pet"
        self.state_file = self.state_dir / "standalone-runtime.json"
        self.log_file = self.state_dir / "hermes-launch.log"
        self.dsh_log = self.state_dir / "logs.txt"

    def _resolve_package_dir(self) -> Path:
        """找到 dsh-pet 包根（含 lib/standalone.js）。

        本层不携带桌宠代码，只指路，按优先级：
        环境变量 DSH_PET_HOME / 设置 dsh_pet_path / 安装时写入的 package-path.json /
        仓库同级目录 / 插件目录下 node_modules。
        """
        env = os.environ.get("DSH_PET_HOME", "").strip()
        configured = str(self._setting("dsh_pet_path", "") or "").strip()
        candidates: list[Path] = []
        if env:
            candidates.append(Path(env))
        if configured:
            candidates.append(Path(configured))
        # 插件自带的一份运行时：安装脚本把 dsh-pet 的可分发部分复制到插件目录下的 dsh-pet/，
        # 装完的插件不依赖仓库还在不在原路径（复制安装、可整体卸载）。
        vendored = self.root / "dsh-pet"
        candidates.append(vendored)
        # 安装脚本把仓库里 dsh-pet 的真实位置写在这里：插件被复制到
        # <HERMES_HOME>/plugins 之后，自己已经不在仓库里了，靠相对路径找不到。
        pointer = self.root / "package-path.json"
        try:
            recorded = json.loads(pointer.read_text("utf-8-sig")).get("packageRoot", "")
            if recorded:
                candidates.append(Path(str(recorded)))
        except (OSError, ValueError):
            pass
        candidates.append(self.root.parent / "dsh-pet")
        candidates.append(self.root / "node_modules" / "dsh-pet")
        for candidate in candidates:
            if (candidate / "lib" / "standalone.js").exists():
                return candidate
        return vendored

    # ── 配置 ───────────────────────────────────────────────────────────
    def _setting(self, key: str, default: Any) -> Any:
        try:
            value = self._ctx.get_config(key, default)
        except Exception:
            return default
        return default if value is None else value

    @property
    def autostart(self) -> bool:
        return bool(self._setting("autostart", True))

    @property
    def port(self) -> int:
        try:
            value = int(self._setting("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            value = DEFAULT_PORT
        return value if 1 <= value <= 65535 else DEFAULT_PORT

    def node_path(self) -> Optional[str]:
        """优先用设置里的路径，其次 PATH 里的 node。"""
        configured = str(self._setting("node_path", "") or "").strip()
        if configured:
            return configured if Path(configured).exists() else None
        found = shutil.which("node")
        if found:
            return found
        for candidate in (
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Program Files (x86)\nodejs\node.exe",
            "/usr/local/bin/node",
            "/usr/bin/node",
        ):
            if Path(candidate).exists():
                return candidate
        return None

    # ── 运行时状态 ─────────────────────────────────────────────────────
    def read_runtime(self) -> Optional[Runtime]:
        """读运行时状态文件；进程已死或文件损坏都返回 None。"""
        try:
            raw = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        try:
            runtime = Runtime(
                pid=int(raw["pid"]),
                port=int(raw["port"]),
                token=str(raw["token"]),
                settings_url=str(raw.get("settingsUrl", "")),
                plugin_root=str(raw.get("pluginRoot", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not self._alive(runtime.pid):
            return None
        return runtime

    def _alive(self, pid: int) -> bool:
        """进程是否还活着。

        Windows 上 ``os.kill(pid, 0)`` 对“已退出但句柄还开着”的自己人仍报存在，
        所以自己拉起的那个必须走 ``Popen.poll()`` 回收，别的 pid 才用系统探测。
        """
        proc = self._proc
        if proc is not None and proc.pid == pid:
            return proc.poll() is None
        return _pid_alive(pid)

    def health(self, port: int) -> Optional[dict]:
        try:
            status, body = api.request(port, "GET", "/health", with_route=False, timeout=1.0)
        except Exception:
            return None
        return body if status == 200 and isinstance(body, dict) else None

    def current(self) -> Optional[Runtime]:
        """正在跑并且真的能应答的那只；不可应答的当没跑。"""
        runtime = self.read_runtime()
        if runtime is None:
            return None
        return runtime if self.health(runtime.port) else None

    def status(self) -> dict:
        runtime = self.current()
        return {
            "running": runtime is not None,
            "pid": runtime.pid if runtime else None,
            "port": runtime.port if runtime else None,
            "settingsUrl": runtime.settings_url if runtime else None,
            "home": str(self.home),
            "packageRoot": str(self.package_dir),
            "script": str(self.script),
            "scriptExists": self.script.exists(),
            "node": self.node_path(),
            "log": str(self.log_file),
        }

    # ── 邻居实例 ───────────────────────────────────────────────────────
    def probe_instances(self, span: int = PROBE_SPAN) -> list[dict]:
        """扫 port..port+span，找出还活着的 dsh-pet 独立实例。

        ``/health`` 是运维端点、不要令牌，回包里带 name 和 pid，正好能区分
        「桌宠服务」和「别的程序恰好占了这个端口」。DSH 自己那只也会被找出来。
        """
        found: list[dict] = []
        base = self.port
        for port in range(base, min(base + span, 65535) + 1):
            try:
                status, body = api.request(port, "GET", "/health", with_route=False, timeout=0.25)
            except Exception:
                continue
            if status != 200 or not isinstance(body, dict):
                continue
            if body.get("name") != "dsh-pet-standalone":
                continue
            try:
                pid = int(body.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            found.append({"port": port, "pid": pid, "root": str(body.get("pluginRoot") or "")})
        return found

    def leftover_instances(self) -> list[dict]:
        """除自己这只以外的实例（非自己 pid 的都算）。"""
        own = self.read_runtime()
        own_pid = own.pid if own else 0
        return [item for item in self.probe_instances() if item["pid"] != own_pid]

    def pid_alive(self, pid: int) -> bool:
        """对外暴露的存活判断（卸载后复查用）。"""
        return _pid_alive(pid)

    def port_open(self, port: int) -> bool:
        """对外暴露的端口判断（卸载后复查用）。"""
        return _port_open(port)

    def terminate_pid(self, pid: int) -> None:
        """按 pid 连子进程一起结束（Windows 走 taskkill /T）。"""
        self._force_kill(pid)

    # ── 启停 ───────────────────────────────────────────────────────────
    def start(self, *, cleanup_on_exit: bool = True, timeout: float = START_TIMEOUT) -> dict:
        """拉起桌宠；已经在跑就复用，不会起第二只。"""
        with self._lock:
            existing = self.current()
            if existing is not None:
                return {"ok": True, "reused": True, "others": self.leftover_instances(), **self.status()}
            # 拉起前先看邻居：端口段里已经有人（DSH 那只、或上次没退干净的）也要说出来，
            # 否则屏幕上多出一只窗口，用户会以为是同一只宠物不听话。
            others = self.leftover_instances()
            if others:
                _log.warning(
                    "dsh-pet 桌宠：端口 %s 上还有别的实例在跑（pid %s），会各弹一个窗口",
                    ", ".join(str(item["port"]) for item in others),
                    ", ".join(str(item["pid"]) for item in others),
                )
            if not self.script.exists():
                raise SidecarError(
                    f"找不到独立模式入口 {self.script}；先跑一次 npm install && npm run build，"
                    "或重新执行 scripts/hermes-plugin-install.ps1（会写入 package-path.json 指路），"
                    "也可以在插件设置 dsh_pet_path 里填 dsh-pet 包目录。"
                )
            node = self.node_path()
            if not node:
                raise SidecarError("找不到 node 可执行文件；请在插件设置里填 node_path，或把 node 放进 PATH。")

            self.state_dir.mkdir(parents=True, exist_ok=True)
            # 借本机 DSH 已下好的 Electron（若存在），避免每次重装都把 350MB 重新拉一遍。
            shared_electron = _shared_electron_hint()
            env = os.environ.copy()
            env["DSH_HOME"] = str(self.home)
            if shared_electron is not None and not env.get("DSH_PET_ELECTRON_PATH"):
                env["DSH_PET_ELECTRON_PATH"] = str(shared_electron)
            # Hermes 进程自身的环境一律不动，只改传给子进程的这一份副本。

            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            log = open(self.log_file, "ab")
            try:
                proc = subprocess.Popen(  # noqa: S603 - 固定 argv，无 shell
                    [node, str(self.script), "--port", str(self.port)],
                    cwd=str(self.package_dir),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                    close_fds=True,
                )
            finally:
                log.close()

            self._proc = proc
            self._cleanup_on_exit = cleanup_on_exit

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                code = proc.poll()
                if code is not None:
                    self._proc = None
                    raise SidecarError(
                        f"桌宠进程启动即退出（退出码 {code}）；日志尾巴：{self._log_tail()}"
                    )
                runtime = self.read_runtime()
                if runtime is not None and runtime.pid == proc.pid and self.health(runtime.port):
                    return {
                        "ok": True,
                        "reused": False,
                        "others": others,
                        "electron": str(shared_electron) if shared_electron is not None else None,
                        **self.status(),
                    }
                time.sleep(0.25)

            # 超时不能把半启动的进程丢在后台：先收掉这次拉起的这只，再报错。
            self._force_kill(proc.pid)
            self._proc = None
            self._cleanup_on_exit = False
            raise SidecarError(
                f"等了 {int(timeout)} 秒桌宠仍未就绪，已结束这次启动的进程；日志尾巴：{self._log_tail()}"
            )

    def stop(self, timeout: float = STOP_TIMEOUT) -> dict:
        """优雅收尾自己这一份；没有在跑就返回 ``stopped: False``。"""
        with self._lock:
            runtime = self.read_runtime()
            if runtime is None:
                return self._stop_owned_proc()

            # 动手之前先确认这个 pid 真是桌宠：状态文件可能来自上次崩溃前的运行，
            # pid 早被系统分配给了别的进程，按它强杀会误伤。
            verified = self._proc is not None and self._proc.pid == runtime.pid
            if not verified:
                verified = self._owned_by_runtime(runtime)

            # 严格按状态文件里的凭据请求退出，不按端口猜、不按名字杀。
            try:
                api.request(
                    runtime.port,
                    "POST",
                    "/quit",
                    token=runtime.token,
                    payload={},
                    timeout=2.0,
                )
            except Exception:
                pass  # 响应不保证送达（发起者通常就是即将被停掉的 Helper），以进程是否消失为准

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not self._alive(runtime.pid):
                    self._proc = None
                    self._cleanup_on_exit = False
                    return {"ok": True, "stopped": True, "pid": runtime.pid}
                time.sleep(0.2)

            if not verified:
                # 对不上号：只清掉过期的状态文件，绝不对别人的 pid 动手。
                self._proc = None
                self._cleanup_on_exit = False
                try:
                    self.state_file.unlink()
                except OSError:
                    pass
                return {"ok": True, "stopped": False, "reason": "unverified-pid", "pid": runtime.pid}

            self._force_kill(runtime.pid)
            self._proc = None
            self._cleanup_on_exit = False
            return {"ok": True, "stopped": True, "pid": runtime.pid, "forced": True}

    def _stop_owned_proc(self) -> dict:
        """状态文件没了，但本次进程确实拉起过一只：直接收自己这只，别留孤儿窗口。"""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._proc = None
            return {"ok": True, "stopped": False, "reason": "not-running"}
        pid = proc.pid
        self._force_kill(pid)
        self._proc = None
        self._cleanup_on_exit = False
        return {"ok": True, "stopped": True, "pid": pid, "forced": True}

    def _owned_by_runtime(self, runtime: Runtime) -> bool:
        """健康检查回的 pid 与状态文件一致，才算“这只进程是我们的”。"""
        body = self.health(runtime.port)
        try:
            return bool(body) and int(body.get("pid", -1)) == runtime.pid
        except (TypeError, ValueError):
            return False

    def _force_kill(self, pid: int) -> None:
        """兜底：优雅退出超时才动手，且只动状态文件里那个 pid。"""
        if self._proc is not None and self._proc.pid == pid:
            self._proc.terminate()
            try:
                self._proc.wait(3)
                return
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(3)
                except subprocess.TimeoutExpired:
                    pass
                return
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
            else:
                os.kill(pid, 15)
        except Exception:
            pass
        try:
            self.state_file.unlink()
        except OSError:
            pass

    def _log_tail(self, lines: int = 6) -> str:
        for candidate in (self.log_file, self.dsh_log):
            try:
                text = candidate.read_text("utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                tail = [line for line in text.splitlines() if line.strip()][-lines:]
                return f"{candidate.name}: " + " | ".join(tail)
        return "（没有日志）"

    def stop_if_owned(self) -> None:
        """只在“这个进程自己拉起的”情况下收尾，避免任意 CLI 进程误停网关那只。"""
        if not self._cleanup_on_exit:
            return
        try:
            self.stop()
        except Exception:
            pass