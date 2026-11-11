from __future__ import annotations

import argparse
from contextlib import nullcontext
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from process_manager import reuse_or_stop_backend, startup_lock


ROOT = Path(__file__).resolve().parents[1]
# A service-managed backend retries the camera every 2 s; give a replugged camera time to come back.
MANAGED_BACKEND_WAIT = float(os.getenv("DOORLOCK_MANAGED_BACKEND_WAIT", "30"))

def ui_python() -> str:
    configured = os.getenv("DOORLOCK_UI_PYTHON")
    if configured:
        return configured
    isolated = ROOT / ".venv-ui" / "bin" / "python"
    return str(isolated) if isolated.is_file() else sys.executable


def wait_ready(process: subprocess.Popen, base: str, timeout: float, require_npu: bool,
               package_model: str = "") -> None:
    deadline = time.monotonic() + timeout
    last_status = "waiting for HTTP"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Vision backend exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1) as response:
                status = json.load(response)
            last_status = str(status)
            if status.get("detector_error"):
                raise RuntimeError(f"Person detector failed: {status['detector_error']}")
            if package_model and status.get("package_detector_error"):
                raise RuntimeError(f"Package detector failed: {status['package_detector_error']}")
            if status.get("ok") and status.get("ring", 0) > 0 and status.get("detector"):
                if require_npu and status["detector"] != "rknn":
                    raise RuntimeError("NPU backend did not load")
                if package_model and not status.get("package_detector"):
                    time.sleep(0.2)
                    continue
                if package_model.endswith(".rknn") and status["package_detector"] != "rknn":
                    raise RuntimeError("Package NPU backend did not load")
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Vision backend not ready after {timeout}s: {last_status}")


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _wake_backend(base: str) -> None:
    """尽早续一次租约，让暂停中的后台在界面启动期间就开始打开相机。"""
    request = urllib.request.Request(
        f"{base}/ui/heartbeat",
        data=json.dumps({"client": f"launcher-{os.getpid()}"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    token = os.getenv("VISION_TOKEN", "")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        urllib.request.urlopen(request, timeout=1).close()
    except (OSError, ValueError, urllib.error.URLError):
        pass


def _can_reuse_existing_backend(base: str, package_model: str = "") -> bool:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=1) as response:
            status = json.load(response)
        paused = bool(status.get("paused"))
        if paused:
            _wake_backend(base)
        if (
            not status.get("ok")
            or status.get("detector_error")
            or not status.get("detector")
            # 暂停时相机已释放、缓冲已清空，界面心跳会把它唤醒
            or (not paused and (not status.get("camera") or status.get("ring", 0) <= 0))
        ):
            return False
        if package_model:
            if status.get("package_detector_error"):
                return False
            backend = status.get("package_detector")
            return bool(backend and (not package_model.endswith(".rknn") or backend == "rknn"))
        return True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _fingerprint_service_running() -> bool:
    port = int(os.getenv("FINGERPRINT_HTTP_PORT", "8085"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
            status = json.load(response)
            return isinstance(status, dict) and status.get("service") == "fingerprintd"
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return False


def _lock_service_running() -> bool:
    port = int(os.getenv("LOCK_HTTP_PORT", "8086"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
            status = json.load(response)
            if not isinstance(status, dict) or status.get("service") != "lockd":
                return False
            expected = os.getenv("LOCK_DRIVER", "servo").strip().lower()
            actual = (status.get("driver") or {}).get("driver")
            if expected and actual != expected:
                raise RuntimeError(
                    f"Existing lockd uses {actual}, requested {expected}; "
                    "stop the old lockd before switching hardware drivers"
                )
            return True
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return False


def _start_lock_service(disabled: bool) -> subprocess.Popen | None:
    """开锁服务必须比指纹服务先就绪，否则第一次按压会被判成"未接入"。"""
    if disabled or _lock_service_running():
        return None
    process = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--lock-only"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _lock_service_running():
            return process
        if process.poll() is not None:
            print("Warning: lock service exited during startup", file=sys.stderr)
            return None
        time.sleep(0.2)
    print("Warning: lock service did not become ready in 10s", file=sys.stderr)
    return process


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Doorlock desktop and device services")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--backend-only", action="store_true")
    modes.add_argument("--ui-only", action="store_true")
    modes.add_argument("--audio-only", action="store_true")
    modes.add_argument("--fingerprint-only", action="store_true")
    modes.add_argument("--lock-only", action="store_true")
    parser.add_argument("--no-audio", action="store_true", help="desktop mode 下不拉起音频服务")
    parser.add_argument("--no-fingerprint", action="store_true", help="desktop mode 下不拉起指纹服务")
    parser.add_argument("--no-lock", action="store_true", help="desktop mode 下不拉起开锁服务")
    parser.add_argument("--source", default=os.getenv("VISION_SOURCE") or "auto")
    parser.add_argument("--model", default=os.getenv("VISION_MODEL", "models/yolov5s-rk3576.rknn"))
    parser.add_argument("--package-model", default=os.getenv(
        "VISION_PACKAGE_MODEL", "models/parcel-yolov5n-rk3576-fp.rknn"
    ), help="包裹检测模型；传空字符串禁用")
    parser.add_argument("--port", type=int, default=int(os.getenv("VISION_HTTP_PORT", "8083")))
    parser.add_argument("--base", default="")
    parser.add_argument("--kiosk", action="store_true")
    parser.add_argument("--startup-timeout", type=float, default=20)
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    os.environ["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
    os.environ["VISION_SOURCE"] = args.source
    os.environ["VISION_MODEL"] = str((ROOT / args.model).resolve())
    os.environ["VISION_PACKAGE_MODEL"] = str((ROOT / args.package_model).resolve()) if args.package_model else ""
    os.environ["VISION_DATA_DIR"] = str((ROOT / os.getenv("VISION_DATA_DIR", "data")).resolve())
    os.environ["VISION_HTTP_PORT"] = str(args.port)
    os.environ.setdefault("FINGERPRINT_DATA_DIR", os.environ["VISION_DATA_DIR"])
    if args.backend_only:
        import asyncio
        from vision.app import main as vision_main
        asyncio.run(vision_main())
        return 0
    if args.audio_only:
        import asyncio
        from audio.app import main as audio_main
        asyncio.run(audio_main())
        return 0
    if args.fingerprint_only:
        import asyncio
        from fingerprint.service import main as fingerprint_main
        asyncio.run(fingerprint_main())
        return 0
    if args.lock_only:
        import asyncio
        from lock.service import main as lock_main
        asyncio.run(lock_main())
        return 0
    base = args.base.rstrip("/") or f"http://127.0.0.1:{args.port}"
    ui_command = [ui_python(), "-m", "ui.app", "--base", base]
    if args.kiosk:
        ui_command.append("--kiosk")
    backend = None
    audio = None
    fingerprint = None
    lock = None
    ui = None
    reuse_backend = False
    previous_handlers = {}
    shutting_down = False

    def interrupted(signum, frame):
        if not shutting_down:
            raise KeyboardInterrupt

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        with (nullcontext() if args.ui_only else startup_lock(args.port)):
            if not args.ui_only:
                if args.base:
                    raise RuntimeError("Use --ui-only with --base to connect to an external backend")
                import socket
                try:
                    with socket.socket() as probe:
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        probe.bind(("0.0.0.0", args.port))
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise
                    reuse_backend = reuse_or_stop_backend(
                        args.port,
                        _can_reuse_existing_backend(base, args.package_model),
                        recheck=lambda: _can_reuse_existing_backend(base, args.package_model),
                        wait=MANAGED_BACKEND_WAIT,
                    )
                    if reuse_backend:
                        print(f"Info: reuse existing backend at {base}", file=sys.stderr)

                if not reuse_backend:
                    backend = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--backend-only"])
                    wait_ready(backend, base, args.startup_timeout, args.model.endswith(".rknn"), args.package_model)
                    if not args.no_audio:
                        audio = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--audio-only"])
                lock = _start_lock_service(args.no_lock)
                if not args.no_fingerprint and not _fingerprint_service_running():
                    fingerprint = subprocess.Popen(
                        [sys.executable, str(ROOT / "main.py"), "--fingerprint-only"]
                    )
            ui = subprocess.Popen(ui_command)
        audio_warned = False
        while ui.poll() is None:
            if backend is not None and backend.poll() is not None:
                raise RuntimeError("Vision backend stopped while UI was running")
            if audio is not None and audio.poll() is not None and not audio_warned:
                print("Warning: audio service stopped, talk API unavailable", file=sys.stderr)
                audio_warned = True
            if fingerprint is not None and fingerprint.poll() is not None:
                print("Warning: fingerprint service stopped; fingerprint UI unavailable", file=sys.stderr)
                fingerprint = None
            if lock is not None and lock.poll() is not None:
                print("Warning: lock service stopped; unlocks will fail", file=sys.stderr)
                lock = None
            time.sleep(0.2)
        return ui.returncode
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError) as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        shutting_down = True
        stop_process(ui)
        stop_process(fingerprint)
        stop_process(lock)
        stop_process(audio)
        stop_process(backend)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)