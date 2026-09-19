import threading
import time

import pytest

from core import remote_proxy
from ui.dialogs.subscription_tags_dialog import SubscriptionTagsDialog


CATALOG = [
    {"id": "a", "name": "家宽 A", "network_type": "residential"},
    {"id": "b", "name": "机房 B", "network_type": "unknown"},
]


def wait(root, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not predicate():
        root.update()
        time.sleep(0.01)
    root.update()
    assert predicate()


@pytest.fixture
def dialog(tk_root, monkeypatch):
    saved = []
    callbacks = []

    def save(updates, *, expected):
        saved.append((updates, expected, threading.get_ident()))
        return {}

    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_network_types", save)
    view = SubscriptionTagsDialog(tk_root, CATALOG, lambda: callbacks.append(threading.get_ident()))
    try:
        tk_root.update()
        yield view, saved, callbacks
    finally:
        view.destroy()
        tk_root.update()


def test_only_changed_labels_are_atomically_saved_off_main_thread(dialog, tk_root):
    view, saved, callbacks = dialog
    assert view._combos["a"].get() == "家宽"
    assert view._combos["b"].get() == "未标记"
    view._combos["b"].set("非家宽")
    view._save()
    wait(tk_root, lambda: view._closed)
    assert len(saved) == 1
    assert saved[0][0] == {"b": "datacenter"}
    assert saved[0][1] == {"b": "unknown"}
    assert saved[0][2] != threading.get_ident()
    assert callbacks == [threading.get_ident()]


def test_unchanged_labels_do_not_write_or_notify(dialog):
    view, saved, callbacks = dialog
    view._save()
    assert not saved and not callbacks
    assert not view._busy and not view._closed
    assert "无需保存" in view._status.cget("text")


def test_empty_catalog_has_disabled_save(tk_root, monkeypatch):
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_network_types",
                        lambda *_args, **_kwargs: pytest.fail("empty catalog must not write"))
    view = SubscriptionTagsDialog(tk_root, [], None)
    try:
        assert view._save_button.cget("state") == "disabled"
        view._save()
        assert not view._busy
    finally:
        view.destroy()


def test_empty_error_stays_visible_and_retry_recovers(dialog, tk_root, monkeypatch):
    view, saved, callbacks = dialog
    original_save = remote_proxy.set_proxy_subscription_network_types

    def fail(*_args, **_kwargs):
        raise TimeoutError()

    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_network_types", fail)
    view._combos["b"].set("非家宽")
    view._save()
    wait(tk_root, lambda: not view._busy)
    assert "TimeoutError" in view._status.cget("text")
    assert not view._closed and not callbacks
    assert view._save_button.cget("state") == "normal"
    assert view._original["b"] == "unknown"
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_network_types", original_save)
    view._save()
    wait(tk_root, lambda: view._closed)
    assert len(saved) == 1 and len(callbacks) == 1


def test_closing_while_save_pending_does_not_access_destroyed_widgets(dialog, tk_root, monkeypatch):
    view, _, callbacks = dialog
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def save(*_args, **_kwargs):
        started.set()
        release.wait(3)
        finished.set()

    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_network_types", save)
    view._combos["b"].set("非家宽")
    view._save()
    assert started.wait(3)
    view.destroy()
    view.destroy()
    release.set()
    assert finished.wait(3)
    tk_root.update()
    assert not callbacks
    assert view._poll_id is None


def test_user_close_during_save_waits_and_preserves_parent_refresh(dialog, tk_root, monkeypatch):
    view, _, callbacks = dialog
    started, release = threading.Event(), threading.Event()
    def save(*_args, **_kwargs):
        started.set()
        assert release.wait(3)
    monkeypatch.setattr(remote_proxy, "set_proxy_subscription_network_types", save)
    view._combos["b"].set("非家宽")
    view._save()
    try:
        assert started.wait(3)
        view._close()
        assert not view._closed and view._busy
        assert "等待完成" in view._status.cget("text")
    finally:
        release.set()
    wait(tk_root, lambda: view._closed)
    assert callbacks == [threading.get_ident()]


def test_thread_start_failure_restores_controls(dialog, monkeypatch):
    view, saved, callbacks = dialog

    def fail(_thread):
        raise RuntimeError("no threads")

    monkeypatch.setattr(threading.Thread, "start", fail)
    view._combos["b"].set("非家宽")
    view._save()
    assert not view._busy and not saved and not callbacks
    assert view._save_button.cget("state") == "normal"
    assert "无法开始保存" in view._status.cget("text")


def test_parent_refresh_failure_reports_saved_state_without_resaving(dialog, tk_root):
    view, saved, _ = dialog

    def fail():
        raise RuntimeError("refresh failed")

    view._on_saved = fail
    view._combos["b"].set("非家宽")
    view._save()
    wait(tk_root, lambda: not view._busy)
    assert "标记已保存" in view._status.cget("text")
    assert not view._closed
    assert view._original["b"] == "datacenter"
    view._save()
    assert len(saved) == 1


def test_classification_controls_fit_narrow_window_and_scaled_dpi(dialog, tk_root):
    view, _, _ = dialog
    import customtkinter as ctk
    original_scaling = ctk.ScalingTracker.widget_scaling
    try:
        for scaling in (1.0, 1.5, 2.0):
            ctk.set_widget_scaling(scaling)
            view.geometry("500x420")
            tk_root.update()
            for widget in (view._save_button, *view._combos.values()):
                assert widget.winfo_rootx() >= view.winfo_rootx()
                assert widget.winfo_rootx() + widget.winfo_width() <= view.winfo_rootx() + view.winfo_width() + 2
    finally:
        ctk.set_widget_scaling(original_scaling)


def test_nested_dialog_restores_parent_grab_when_closed(tk_root):
    import customtkinter as ctk
    parent = ctk.CTkToplevel(tk_root)
    parent.grab_set()
    view = SubscriptionTagsDialog(parent, CATALOG, None)
    try:
        tk_root.update()
        assert view.grab_current() is view
        view.destroy()
        assert parent.grab_current() is parent
    finally:
        view.destroy()
        parent.destroy()
