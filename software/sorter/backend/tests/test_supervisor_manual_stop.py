from pathlib import Path

import supervisor as supervisor_module

from supervisor import BackendSupervisor


class _FakeProcess:
    pid = 12345
    returncode = -15

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode


class _RunningFakeProcess:
    pid = 12345

    def __init__(self) -> None:
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9


def _supervisor() -> BackendSupervisor:
    return BackendSupervisor(
        command=["backend"],
        cwd=Path("."),
        environment={},
        backend_health_url="http://127.0.0.1:1/health",
        health_interval_s=999.0,
        health_timeout_s=0.01,
        restart_backoff_s=0.0,
        stop_timeout_s=0.01,
    )


def test_manual_stop_exit_does_not_auto_restart(monkeypatch):
    supervisor = _supervisor()
    child = _FakeProcess()
    supervisor._process = child
    supervisor._process_group_pid = child.pid
    supervisor._manual_stop_requested = True
    supervisor._last_exit_reason = "manual stop requested"
    restarted = []
    monkeypatch.setattr(
        supervisor,
        "_start_backend",
        lambda *, reason: restarted.append(reason),
    )

    supervisor._watch_process(child)

    status = supervisor.status()
    assert status["supervisor_state"] == "stopped"
    assert status["backend_running"] is False
    assert status["last_exit_reason"] == "manual stop requested"
    assert restarted == []


def test_stop_without_running_process_stays_stopped():
    supervisor = _supervisor()
    supervisor._manual_stop_requested = True

    supervisor._stop_backend(reason="manual stop requested")

    status = supervisor.status()
    assert status["supervisor_state"] == "stopped"
    assert status["last_exit_reason"] == "manual stop requested"


def test_status_reports_manual_stop_safety_protocol():
    status = _supervisor().status()

    assert status["supervisor_protocol"] == 2
    assert status["manual_stop_safe"] is True


def test_stop_backend_uses_process_terminate_on_windows(monkeypatch):
    supervisor = _supervisor()
    child = _RunningFakeProcess()
    supervisor._process = child
    supervisor._process_group_pid = child.pid
    supervisor._manual_stop_requested = True

    monkeypatch.setattr(supervisor_module.os, "name", "nt", raising=False)

    supervisor._stop_backend(reason="manual stop requested")

    assert child.terminate_calls == 1
    assert child.kill_calls == 0
    status = supervisor.status()
    assert status["supervisor_state"] == "stopped"
    assert status["last_exit_reason"] == "manual stop requested"
