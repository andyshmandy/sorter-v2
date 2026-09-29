from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from server.security import (
    is_loopback_client_address,
    is_ui_origin_allowed,
    normalize_origin,
)

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DEFAULT_CONTROL_HOST = os.getenv("BACKEND_SUPERVISOR_HOST", "127.0.0.1")
DEFAULT_CONTROL_PORT = int(os.getenv("BACKEND_SUPERVISOR_PORT", "8001"))
DEFAULT_RESTART_BACKOFF_S = float(os.getenv("BACKEND_SUPERVISOR_RESTART_BACKOFF_S", "0.2"))
DEFAULT_STOP_TIMEOUT_S = float(os.getenv("BACKEND_SUPERVISOR_STOP_TIMEOUT_S", "5.0"))
DEFAULT_FAST_CRASH_WINDOW_S = float(os.getenv("BACKEND_SUPERVISOR_FAST_CRASH_WINDOW_S", "30.0"))
CACHE_CLEAR_CRASH_THRESHOLD = 3


class BackendSupervisor:
    """Runs the backend, restarts it when it exits, and restarts it on request.
    The backend inherits the supervisor's stdout and stderr (the journal)."""

    def __init__(
        self,
        *,
        command: list[str],
        cwd: Path,
        environment: dict[str, str],
        restart_backoff_s: float,
        stop_timeout_s: float,
        fast_crash_window_s: float = DEFAULT_FAST_CRASH_WINDOW_S,
    ) -> None:
        self._command = list(command)
        self._cwd = cwd
        self._environment = dict(environment)
        self._restart_backoff_s = restart_backoff_s
        self._stop_timeout_s = stop_timeout_s
        self._fast_crash_window_s = fast_crash_window_s
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._restart_requested = False
        # Started with start_new_session, so its pid is also its process group.
        self._process: subprocess.Popen[bytes] | None = None
        self._process_started_at: float | None = None
        self._consecutive_fast_crashes = 0
        self._cache_cleared_for_streak = False

    def start(self) -> None:
        self._start_backend()

    def shutdown(self) -> None:
        self._shutdown.set()
        self._stop_backend()

    def request_restart(self) -> bool:
        with self._lock:
            if self._restart_requested:
                return False
            self._restart_requested = True
        threading.Thread(target=self._restart_worker, daemon=True).start()
        return True

    def _restart_worker(self) -> None:
        try:
            self._stop_backend()
            if self._shutdown.is_set():
                return
            time.sleep(self._restart_backoff_s)
            self._start_backend()
        finally:
            with self._lock:
                self._restart_requested = False

    def _start_backend(self) -> None:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                return
            child = subprocess.Popen(
                self._command,
                cwd=str(self._cwd),
                env=self._environment,
                start_new_session=True,
            )
            self._process = child
            self._process_started_at = time.time()
        threading.Thread(target=self._watch_process, args=(child,), daemon=True).start()

    def _clear_bytecode_caches(self) -> int:
        cleared = 0
        cache_prefix = self._environment.get("PYTHONPYCACHEPREFIX")
        if cache_prefix and Path(cache_prefix).is_dir():
            try:
                shutil.rmtree(cache_prefix)
                cleared += 1
            except OSError:
                pass
        for root, dirs, _files in os.walk(self._cwd):
            dirs[:] = [d for d in dirs if d not in (".venv", "node_modules", ".git")]
            if "__pycache__" in dirs:
                dirs.remove("__pycache__")
                try:
                    shutil.rmtree(Path(root) / "__pycache__")
                    cleared += 1
                except OSError:
                    pass
        return cleared

    def _watch_process(self, child: subprocess.Popen[bytes]) -> None:
        child.wait()
        with self._lock:
            if self._process is not child:
                return
            started_at = self._process_started_at
            self._process = None
            if self._shutdown.is_set() or self._restart_requested:
                return
            if started_at is not None and time.time() - started_at < self._fast_crash_window_s:
                self._consecutive_fast_crashes += 1
            else:
                self._consecutive_fast_crashes = 0
                self._cache_cleared_for_streak = False
            clear_cache = (
                self._consecutive_fast_crashes >= CACHE_CLEAR_CRASH_THRESHOLD
                and not self._cache_cleared_for_streak
            )
            if clear_cache:
                self._cache_cleared_for_streak = True

        if clear_cache:
            # A corrupt .pyc never self-heals: Python trusts the cache header while the
            # body is garbage, so the backend crash-loops forever. Clearing the caches
            # once per streak is free and lets the next start recompile from source.
            cleared = self._clear_bytecode_caches()
            print(
                f"[supervisor] crash loop detected ({self._consecutive_fast_crashes} fast exits); "
                f"cleared {cleared} __pycache__ dirs before restarting",
                flush=True,
            )

        time.sleep(self._restart_backoff_s)
        if not self._shutdown.is_set():
            self._start_backend()

    def _stop_backend(self) -> None:
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=self._stop_timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass


def _handler_factory(supervisor: BackendSupervisor):
    class SupervisorHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/api/supervisor/restart":
                self._send_json(404, {"ok": False, "message": "Not found"})
                return
            client_host = self.client_address[0] if self.client_address else None
            if not is_loopback_client_address(client_host):
                self._send_json(403, {"ok": False, "message": "Supervisor control is restricted to loopback clients."})
                return
            origin = normalize_origin(self.headers.get("Origin"))
            if origin is None or not is_ui_origin_allowed(origin):
                self._send_json(403, {"ok": False, "message": "Supervisor control requests must include an allowed Origin header."})
                return
            accepted = supervisor.request_restart()
            self._send_json(202, {"ok": True, "accepted": accepted, "message": "Hard restart requested."})

        def log_message(self, format: str, *args: Any) -> None:
            message = format % args
            print(f"[supervisor] {self.address_string()} {message}", flush=True)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return SupervisorHandler


def _default_backend_command(script_dir: Path) -> list[str]:
    return [sys.executable, str(script_dir / "main.py")]


def _parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Supervisor for the sorter backend.")
    parser.add_argument("--host", default=DEFAULT_CONTROL_HOST)
    parser.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT)
    parser.add_argument(
        "--restart-backoff",
        type=float,
        default=DEFAULT_RESTART_BACKOFF_S,
    )
    parser.add_argument(
        "--stop-timeout",
        type=float,
        default=DEFAULT_STOP_TIMEOUT_S,
    )
    parser.add_argument(
        "backend_command",
        nargs=argparse.REMAINDER,
        help="Optional backend command after '--'. Defaults to running main.py with the current Python.",
    )
    args = parser.parse_args()

    default_command = _default_backend_command(script_dir)
    command = list(args.backend_command)
    if command and command[0] == "--":
        command = command[1:]
    args.backend_command = command or default_command
    return args


def main() -> None:
    args = _parse_args()
    script_dir = Path(__file__).resolve().parent
    supervisor = BackendSupervisor(
        command=list(args.backend_command),
        cwd=script_dir,
        environment=os.environ.copy(),
        restart_backoff_s=float(args.restart_backoff),
        stop_timeout_s=float(args.stop_timeout),
    )
    supervisor.start()

    server = ThreadingHTTPServer((str(args.host), int(args.control_port)), _handler_factory(supervisor))

    def _shutdown(*_args: Any) -> None:
        # server.shutdown() waits for serve_forever() to return, and that runs
        # on this thread, so called here it deadlocks until systemd's SIGKILL.
        # Mark the supervisor stopping first so it doesn't restart the backend
        # that systemd's SIGTERM just stopped.
        def _stop() -> None:
            supervisor.shutdown()
            server.shutdown()

        threading.Thread(target=_stop, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(
        f"[supervisor] control=http://{args.host}:{args.control_port} "
        f"command={' '.join(args.backend_command)}",
        flush=True,
    )

    try:
        server.serve_forever()
    finally:
        supervisor.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
