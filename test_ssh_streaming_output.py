"""Streaming/cancellation without any SSH connection or user configuration."""
from collections import deque
import threading
from types import SimpleNamespace

import pytest

from core.ssh_manager import SSHManager


class Channel:
    def __init__(self, stdout=(), stderr=(), *, finished=True):
        self.stdout, self.stderr = deque(stdout), deque(stderr)
        self.finished = finished
        self.closed = False
        self.reads = []

    def recv_ready(self):
        return bool(self.stdout)

    def recv(self, size):
        self.reads.append("out")
        return self.stdout.popleft()

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def recv_stderr(self, size):
        self.reads.append("err")
        return self.stderr.popleft()

    def exit_status_ready(self):
        return self.finished and not self.stdout and not self.stderr

    def recv_exit_status(self):
        return 0

    def close(self):
        self.closed = True


def collect(channel, **kwargs):
    stream = SimpleNamespace(channel=channel)
    return SSHManager._collect_command_output(stream, stream, timeout=1, max_output_bytes=1024, **kwargs)


def test_streaming_utf8_cross_chunk_and_trailing_partial_character():
    raw = "节点✓\n".encode() + b"\xe4"
    channel = Channel([raw[:1], raw[1:4], raw[4:]])
    chunks = []
    status, stdout, stderr = collect(channel, stdout_callback=chunks.append)
    assert status == 0 and stderr == ""
    assert stdout == "节点✓\n�" == "".join(chunks)


def test_stdout_and_stderr_are_fairly_drained():
    channel = Channel([b"one", b"two", b"three"], [b"warn"])
    collect(channel)
    assert channel.reads[:2] == ["out", "err"]


def test_cancel_keeps_delivered_progress_and_closes_only_command_channel():
    cancel = threading.Event()
    channel = Channel([b"finished-node\n", b"not-read\n"], finished=False)
    chunks = []

    def receive(text):
        chunks.append(text)
        cancel.set()

    with pytest.raises(InterruptedError):
        collect(channel, stdout_callback=receive, cancel_event=cancel)
    assert chunks == ["finished-node\n"]
    assert channel.closed and channel.stdout


def test_precancel_does_not_execute_a_remote_command():
    cancel = threading.Event()
    cancel.set()
    client = SimpleNamespace(exec_command=lambda *_args, **_kwargs: pytest.fail("cancelled before exec"))
    with pytest.raises(InterruptedError):
        SSHManager().execute_command_with_status(client, "synthetic", cancel_event=cancel)


def test_callback_failure_closes_its_channel():
    channel = Channel([b"one"])

    def receive(_text):
        raise ValueError("synthetic callback rejection")

    with pytest.raises(ValueError, match="synthetic"):
        collect(channel, stdout_callback=receive)
    assert channel.closed


def test_continuous_output_cannot_bypass_total_deadline(monkeypatch):
    channel = Channel([b"x"] * 50, finished=False)
    now = [0.0]

    def tick():
        now[0] += 0.2
        return now[0]

    monkeypatch.setattr("core.ssh_manager.time.monotonic", tick)
    with pytest.raises(TimeoutError):
        collect(channel)
    assert channel.closed and channel.stdout


def test_output_limit_applies_before_callback():
    channel = Channel([b"x" * 1025])
    chunks = []
    with pytest.raises(ValueError, match="上限"):
        collect(channel, stdout_callback=chunks.append)
    assert channel.closed and chunks == []


def test_execute_passes_streaming_and_cancel_options():
    cancel = threading.Event()
    channel = Channel([b"node\n"])
    channel.shutdown_write = lambda: None
    written = []
    stdin = SimpleNamespace(channel=channel, write=written.append, flush=lambda: None)
    stream = SimpleNamespace(channel=channel)
    client = SimpleNamespace(exec_command=lambda *_args, **_kwargs: (stdin, stream, stream))
    chunks = []
    assert SSHManager().execute_command_with_status(
        client, "synthetic", input_data="[]", stdout_callback=chunks.append, cancel_event=cancel,
    ) == (0, "node\n", "")
    assert chunks == ["node\n"] and written == ["[]"]
