"""Bounded download concurrency and UI progress, with synthetic state only."""
from concurrent.futures import ThreadPoolExecutor as RealExecutor
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace
from urllib.request import Request

import pytest

from core import local_proxy, proxy_routing, remote_proxy, subscription_auto_refresh as worker
from core.proxy_update_result import update_result
from core.subscription_refresh_schedule import SubscriptionRefreshSchedule
from test_subscription_auto_refresh import timer as _timer_fixture
from ui import subscription_auto_refresh as runner

timer = _timer_fixture


@pytest.fixture
def batch(monkeypatch):
    profiles = {key: {"id": key, "url": f"https://{key}.example/sub?token=synthetic-secret",
                      "name": "https://name.example/synthetic-secret", "source_revision": "one"}
                for key in "abcd"}
    state = {"active_profile_id": "a", "profiles": profiles}
    routes = {"service_profile_bindings": dict(zip(("claude", "youtube", "google", "github"), profiles))}
    cache, applications, fetches = {}, [], []
    schedule = SubscriptionRefreshSchedule(clock=lambda: 0.0)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", lambda: routes)
    monkeypatch.setattr(proxy_routing, "load_ssh_routes", lambda _name: routes)
    monkeypatch.setattr(proxy_routing, "host_lock", lambda _name: nullcontext())
    monkeypatch.setattr(worker, "_cached_node_keys", lambda _profile: {"original"})
    monkeypatch.setattr(local_proxy, "local_proxy_subscription_direct_fallback_allowed", lambda: False)
    monkeypatch.setattr(remote_proxy, "proxy_subscription_node_key", lambda node: node)
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda profile: cache.get(profile["id"]))

    def fetch(_url, **kwargs):
        key = kwargs["profile_id"]
        fetches.append(key)
        assert kwargs["activate"] is False
        assert kwargs["allow_direct_fallback"] is False
        assert kwargs["recovery_proxy_provider"] is local_proxy.local_proxy_subscription_recovery_session
        cache[key] = SimpleNamespace(nodes=(key,))
        return cache[key]

    def apply(name, nodes, **kwargs):
        applications.append((name, threading.get_ident(), kwargs))
        return update_result("applied synthetic routes", "applied")

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fetch)
    monkeypatch.setattr(local_proxy, "refresh_running_local_service_routes_from_subscription",
                        lambda nodes, **kwargs: apply("local", nodes, **kwargs))
    monkeypatch.setattr(remote_proxy, "refresh_running_ai_proxy_from_subscription", apply)
    return SimpleNamespace(state=state, routes=routes, cache=cache, applications=applications,
                           fetches=fetches, fetch=fetch, schedule=schedule,
                           run=lambda scope="local", **kwargs: worker.refresh_saved_subscriptions(
                               scope, server_names=("server-a", "server-b"), interval_seconds=300,
                               schedule=schedule, **kwargs))


@pytest.mark.parametrize("scope", ["local", "ssh"])
def test_two_download_slots_apply_serially_once_after_all_finish(batch, monkeypatch, scope):
    barrier, lock = threading.Barrier(2), threading.Lock()
    active = peak = 0
    download_threads, progress_threads, events, apply_observations = set(), set(), [], []
    coordinator = threading.get_ident()

    def fetch(url, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            download_threads.add(threading.get_ident())
        try:
            barrier.wait(timeout=3)
            return batch.fetch(url, **kwargs)
        finally:
            with lock:
                active -= 1

    def progress(event):
        progress_threads.add(threading.get_ident())
        events.append(event)
        if event["stage"] == "apply":
            apply_observations.append((active, len(batch.fetches)))

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fetch)
    result = batch.run(scope, on_progress=progress)
    assert not result["errors"] and set(result["results"]) == set("abcd")
    assert peak == 2 and len(download_threads) == 2 and coordinator not in download_threads
    assert progress_threads == {coordinator}
    assert apply_observations and all(value == (0, 4) for value in apply_observations)
    assert [row[0] for row in batch.applications] == (["local"] if scope == "local" else ["server-a", "server-b"])
    assert all(row[1] == coordinator for row in batch.applications)
    assert [event["completed"] for event in events if event["stage"] == "download"] == [0, 1, 2, 3, 4]
    assert len(result["result_sources"]) == 4
    assert not any(secret in json.dumps(events) for secret in ("secret", "https://", "server-a", "token"))
    if scope == "ssh":
        assert all(row[2]["require_service_binding"] is True for row in batch.applications)
    assert all(source.reservation is None for source in batch.schedule._sources.values())


def test_real_loopback_transport_overlaps_two_requests_without_external_proxy(batch, monkeypatch):
    barrier, lock = threading.Barrier(2), threading.Lock()
    active = peak = 0
    paths, handler_errors = [], []
    body = b"proxies:\n - {name: fixture, type: http, server: fixture.example, port: 8080}\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                paths.append(self.path)
            try:
                barrier.wait(timeout=3)  # Serial I/O cannot pass this barrier.
                self.send_response(200)
                self.send_header("Content-Type", "application/yaml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                handler_errors.append(type(exc).__name__)
            finally:
                with lock:
                    active -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    for key, profile in batch.state["profiles"].items():
        profile["url"] = f"http://127.0.0.1:{server.server_port}/{key}"

    def fetch(url, **kwargs):
        payload, content_type, _charset = remote_proxy._open_validated_proxy_subscription_request(
            Request(url), timeout=3, max_bytes=4096, direct=True,
        )
        assert payload == body and content_type == "application/yaml"
        return batch.fetch(url, **kwargs)

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fetch)
    started = time.perf_counter()
    try:
        result = batch.run()
        elapsed = time.perf_counter() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert not result["errors"] and result["downloaded_count"] == 4
    assert sorted(paths) == ["/a", "/b", "/c", "/d"]
    assert peak == 2 and not handler_errors and not thread.is_alive()
    print(f"loopback: 4 subscriptions, peak {peak} HTTP requests, {elapsed:.3f}s")


def test_fast_sibling_progress_is_emitted_before_slow_first_download_finishes(batch, monkeypatch):
    released, first_started = threading.Event(), threading.Event()
    events = []

    def fetch(url, **kwargs):
        if kwargs["profile_id"] == "a":
            first_started.set()
            assert released.wait(3), "fast sibling progress was blocked by slow first future"
        else:
            assert first_started.wait(3)
        return batch.fetch(url, **kwargs)

    def progress(event):
        events.append(event)
        if event["stage"] == "download" and event["index"] == 2:
            assert "a" not in batch.cache
            released.set()

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fetch)
    result = batch.run(on_progress=progress)
    assert released.is_set() and not result["errors"]
    completed = [event for event in events if event["stage"] == "download" and event["index"]]
    assert completed[0]["index"] == 2


@pytest.mark.parametrize("field,value", [("url", "https://changed.example/sub"), ("source_revision", "two")])
def test_parallel_source_change_rejects_stale_sibling_but_keeps_healthy_results(batch, monkeypatch, field, value):
    released = threading.Event()

    def fetch(url, **kwargs):
        if kwargs["profile_id"] == "a":
            assert released.wait(3)
        return batch.fetch(url, **kwargs)

    def progress(event):
        if event["stage"] == "download" and event["index"] == 2:
            batch.state["profiles"]["a"][field] = value
            released.set()

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fetch)
    result = batch.run(on_progress=progress)
    assert result["errors"] and set(result["results"]) == set("bcd")
    assert set(result["result_sources"]) == set("bcd")
    assert len(batch.applications) == 1 and batch.applications[0][2]["profile_id"] == "b"
    assert all(source.reservation is None for source in batch.schedule._sources.values())


def test_source_changed_before_waiting_slot_starts_never_fetches_old_url(batch, monkeypatch):
    def fetch(url, **kwargs):
        if kwargs["profile_id"] in "ab":
            batch.state["profiles"]["c"]["source_revision"] = "changed-before-start"
        return batch.fetch(url, **kwargs)

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fetch)
    result = batch.run()
    assert "c" not in batch.fetches and "c" not in result["results"]
    assert set(result["results"]) == set("abd")


def test_failed_submit_releases_only_its_reservation_and_healthy_siblings_continue(batch, monkeypatch):
    class OneFailedSubmit(RealExecutor):
        def submit(self, *args, **kwargs):
            if not getattr(self, "failed_once", False):
                self.failed_once = True
                raise RuntimeError("synthetic thread start failure")
            return super().submit(*args, **kwargs)

    monkeypatch.setattr(worker, "ThreadPoolExecutor", OneFailedSubmit)
    result = batch.run()
    assert result["errors"] and set(result["results"]) == set("bcd")
    assert all(source.reservation is None for source in batch.schedule._sources.values())
    assert len(batch.applications) == 1


def test_advisory_callback_failure_cannot_cancel_download_or_leak_reservation(batch):
    def broken_progress(_event):
        raise RuntimeError("synthetic destroyed progress consumer")

    result = batch.run(on_progress=broken_progress)
    assert not result["errors"] and result["downloaded_count"] == 4
    batch.fetches.clear()
    second = batch.run()
    assert not batch.fetches and second["waiting_count"] == 4
    assert len(batch.applications) == 1


def test_ui_progress_is_main_thread_only_and_never_commits_nodes_early(timer, monkeypatch):
    posted, release = threading.Event(), threading.Event()
    threads, ui_threads = [], []
    main_thread = threading.get_ident()
    status_name = "_set_status" if timer.kind == "local" else "_set_proxy_status"
    old_status = getattr(timer.tab, status_name)

    def status(*args):
        ui_threads.append(threading.get_ident())
        old_status(*args)

    setattr(timer.tab, status_name, status)

    def refresh(_scope, *, on_progress, **_kwargs):
        on_progress(dict(stage="download", completed=1, total=2, index=2, outcome="downloaded",
                         message="https://secret.example/token", name="secret"))
        posted.set()
        assert release.wait(3)
        return {"results": {}, "errors": [], "steps": [], "downloaded_count": 2}

    class BackgroundThread:
        def __init__(self, **kwargs):
            self.thread = threading.Thread(**kwargs)
            threads.append(self.thread)

        def start(self):
            self.thread.start()
            assert posted.wait(3)

    monkeypatch.setattr(worker, "refresh_saved_subscriptions", refresh)
    try:
        timer.start(BackgroundThread)
        assert any("1/2" in message[0] for message in timer.calls.status)
        assert not any("secret" in message[0] for message in timer.calls.status)
        assert not timer.calls.nodes and not timer.calls.refresh and not timer.calls.release
        assert ui_threads == [main_thread] * len(ui_threads)
        assert timer.calls.busy == [True]
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()
    timer.calls.after.pop(next(iter(timer.calls.after)))()
    assert timer.calls.busy == [True, False] and timer.calls.release == [True]
    assert ui_threads == [main_thread] * len(ui_threads)


@pytest.mark.parametrize("event", [None, {}, {"stage": "https://secret.example"},
                                   dict(stage="download", completed=True, total=3, index=1),
                                   dict(stage="download", completed=4, total=3, index=1),
                                   dict(stage="download", completed=1, total=3, index=-1)])
def test_invalid_progress_is_ignored_without_worker_text(event):
    assert runner._progress_message(event, "Win11") == ""


def test_unknown_progress_outcome_is_not_rendered_or_used_as_mapping_key():
    text = runner._progress_message(dict(stage="download", completed=1, total=2, index=1,
                                         outcome={"secret": "https://secret.example"}), "Win11")
    assert "1/2" in text and "secret" not in text
