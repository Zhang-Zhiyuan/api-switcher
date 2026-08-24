import threading

from core import usage_recorder as usage_recorder_module


class _StatsRecorder:
    def __init__(self):
        self.events = []

    def record_switch(self, name, kind):
        self.events.append(("switch", name, kind))

    def record_usage(self, name, kind, duration):
        self.events.append(("usage", name, kind, duration))

    def record_tokens(self, name, kind, input_tokens, output_tokens):
        self.events.append(("tokens", name, kind, input_tokens, output_tokens))

    def record_error(self, name, kind):
        self.events.append(("error", name, kind))

    def record_success(self, name, kind):
        self.events.append(("success", name, kind))


def test_usage_recorder_uses_monotonic_elapsed_time(monkeypatch):
    stats = _StatsRecorder()
    clock = iter((100.0, 103.5))
    monkeypatch.setattr(usage_recorder_module, "usage_stats", stats)
    monkeypatch.setattr(usage_recorder_module.time, "monotonic", lambda: next(clock))
    recorder = usage_recorder_module.UsageRecorder()

    recorder.start_session("relay", "codex")
    recorder.end_session()

    assert stats.events[:1] == [("switch", "relay", "codex")]
    assert stats.events[1] == ("usage", "relay", "codex", 3.5)


def test_usage_recorder_serializes_concurrent_profile_switches(monkeypatch):
    stats = _StatsRecorder()
    monkeypatch.setattr(usage_recorder_module, "usage_stats", stats)
    recorder = usage_recorder_module.UsageRecorder()
    barrier = threading.Barrier(3)

    def switch(name):
        barrier.wait()
        recorder.start_session(name, "claude")

    threads = [threading.Thread(target=switch, args=(name,)) for name in ("one", "two")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert recorder.current_profile in {"one", "two"}
    assert [event[0] for event in stats.events].count("switch") == 2
    assert [event[0] for event in stats.events].count("usage") == 1
