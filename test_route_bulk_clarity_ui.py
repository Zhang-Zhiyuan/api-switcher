"""Compact batch choices retain safety feedback and real selection controls."""
from pathlib import Path
import socket
import time

import customtkinter as ctk
import pytest

from core import proxy_routing
from ui.dialogs.service_route_bulk_dialog import DIRECT, DISABLE, FOLLOW, RouteBulkDialog
from ui.theme import COLORS


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("batch layout tests must not access the network")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


def _settle(root, seconds=0.45):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        root.update()
        time.sleep(0.01)


@pytest.fixture
def batch(tk_root):
    views = []

    def create(*, initial=(), long_name=False):
        rows = proxy_routing.route_rows(proxy_routing.normalize_routes({
            "builtin_sites": {"youtube": True},
            "custom_targets": [{"id": "demo", "kind": "domain", "value": "example.test", "enabled": False}],
        }))
        if long_name:
            next(row for row in rows if row["id"] == "custom:demo")["label"] = "合成自定义目标名称需要完整可读并可点击选择-" * 8
        accepted = []
        view = RouteBulkDialog(
            tk_root, rows=rows, catalog=[{
                "id": "synthetic", "name": "合成订阅", "nodes": [{"key": "one", "label": "合成节点"}],
            }], initial_services=initial, scope_label="合成 Win11 设备",
            on_apply=lambda *args, **kwargs: accepted.append((args, kwargs)),
        )
        views.append(view)
        _settle(tk_root)
        return view, accepted

    yield create
    for view in views:
        if view.winfo_exists():
            view.destroy()
    tk_root.update()


def test_next_step_matches_missing_choice_and_only_one_save_instruction(batch, tk_root):
    view, accepted = batch()
    assert "请勾选目标并选择访问线路" in view._status.cget("text")
    view._select_group("ai")
    assert "已选 3 项，请选择访问线路" in view._status.cget("text")
    assert view._apply_button.cget("state") == "disabled"
    view._select_route(FOLLOW)
    assert "已选 3 项，确认后返回编辑" in view._status.cget("text")
    assert view._apply_button.cget("state") == "normal"
    view._select_group("none")
    assert "请勾选要修改的目标" in view._status.cget("text")
    labels, pending = [], [view]
    while pending:
        widget = pending.pop()
        pending.extend(widget.winfo_children())
        if isinstance(widget, ctk.CTkLabel):
            labels.append(widget.cget("text"))
    assert sum(text.count("保存并应用") for text in labels) == 1
    assert not accepted


@pytest.mark.parametrize("operation,detail", [(DIRECT, "不自动回退代理"), (DISABLE, "不等于直连")])
def test_route_safety_hint_stays_visible_with_no_targets(batch, tk_root, operation, detail):
    view, accepted = batch()
    view._select_route(operation)
    view._select_group("none")
    assert detail in view._status.cget("text")
    assert view._status.winfo_manager() == "pack"
    assert view._apply_button.cget("state") == "disabled" and not accepted


def test_validation_error_survives_target_adjustment_and_layout(batch, tk_root):
    view, accepted = batch(initial=("youtube",))
    view._select_route(DIRECT)

    def reject(*_args, **_kwargs):
        raise ValueError("合成校验失败：严格隐私与直连冲突")

    view._on_apply = reject
    view._commit()
    view._select_group("ai")
    view._layout_targets()
    assert "合成校验失败" in view._status.cget("text")
    assert "不自动回退代理" in view._status.cget("text")
    assert view._status.cget("text_color") == COLORS["warning"]
    assert view.winfo_exists() and not accepted
    view._select_route(FOLLOW)
    assert "合成校验失败" not in view._status.cget("text")


def test_stale_candidate_warning_is_not_erased_by_group_selection(batch, tk_root):
    view, accepted = batch()
    view._select_route("合成订阅")
    view._choose_node("deleted", profile_id="synthetic")
    view._select_group("sites")
    assert "已变化" in view._status.cget("text")
    assert view._node_key == "" and not accepted


@pytest.mark.parametrize("choice", ["node", "pool"])
def test_valid_node_choice_clears_only_the_old_candidate_warning(batch, tk_root, choice):
    view, accepted = batch(initial=("youtube",))
    view._select_route("合成订阅")
    view._choose_node("deleted", profile_id="synthetic")
    assert "已变化" in view._status.cget("text")
    if choice == "node":
        view._choose_node("one", profile_id="synthetic")
        assert view._node_key == "one"
    else:
        view._choose_pool(["one"], profile_id="synthetic")
        assert view._node_keys == ["one"]
    assert "已变化" not in view._status.cget("text")
    assert view._status.cget("text_color") == COLORS["muted"]
    assert not view._status_error and not view._status_error_source and not accepted


@pytest.mark.parametrize("choice", ["node", "pool"])
def test_valid_node_choice_keeps_unrelated_validation_error(batch, tk_root, choice):
    view, accepted = batch(initial=("youtube",))
    view._select_route("合成订阅")

    def reject(*_args, **_kwargs):
        raise ValueError("合成校验失败：目标位置正在应用其他修改")

    view._on_apply = reject
    view._commit()
    if choice == "node":
        view._choose_node("one", profile_id="synthetic")
    else:
        view._choose_pool(["one"], profile_id="synthetic")
    assert "合成校验失败" in view._status.cget("text")
    assert view._status.cget("text_color") == COLORS["warning"]
    assert view._status_error_source == "validation" and not accepted


def test_target_labels_do_not_present_previous_state_as_new_choice(batch, tk_root):
    view, accepted = batch(initial=("youtube",))
    view._select_route(DIRECT)
    assert view._target_labels["youtube"].cget("text") == "YouTube"
    assert view._target_labels["openai"].cget("text") == "OpenAI / Codex"
    assert all("专用线路" not in label.cget("text") and "沿用原规则" not in label.cget("text")
               for label in view._target_labels.values())
    assert not accepted


def test_responsive_layout_keeps_widgets_choices_and_caches_relayout(batch, tk_root, monkeypatch):
    view, accepted = batch(initial=("youtube", "claude"))
    previous_scale = ctk.ScalingTracker.widget_scaling
    original_widgets = dict(view._target_checks)
    try:
        ctk.set_widget_scaling(1.0)
        # CTk temporarily locks min/max dimensions for one second after a
        # scaling change. Resize only after that real toolkit timer completes.
        _settle(tk_root, 1.1)
        view.geometry("920x780")
        _settle(tk_root)
        assert view._target_columns == 2
        view.geometry("560x700")
        _settle(tk_root)
        assert view._target_columns == 1, {
            "window": view.winfo_width(), "viewport": view._body._parent_canvas.winfo_width(),
            "body": view._body.winfo_width(), "targets": view._targets.winfo_width(),
            "scaling": view._targets._get_widget_scaling(),
        }
        assert view._target_checks == original_widgets
        assert {key for key, var in view._vars.items() if var.get()} == {"youtube", "claude"}
        calls = []
        for check in view._target_checks.values():
            monkeypatch.setattr(check, "grid", lambda **kwargs: calls.append(kwargs))
        for _ in range(5):
            view._layout_targets()
        assert not calls and not accepted
    finally:
        ctk.set_widget_scaling(previous_scale)
        tk_root.update()


def test_wrapped_label_and_checkbox_both_toggle_the_same_selection(batch, tk_root):
    view, accepted = batch(long_name=True)
    view._body._parent_canvas.yview_moveto(1)
    _settle(tk_root)
    label, check = view._target_labels["custom:demo"], view._target_checks["custom:demo"]
    label._label.event_generate("<Button-1>", x=5, y=5)
    tk_root.update()
    assert view._vars["custom:demo"].get()
    check._canvas.event_generate("<Button-1>", x=8, y=8)
    tk_root.update()
    assert not view._vars["custom:demo"].get() and not accepted


@pytest.mark.parametrize("scale", [1.0, 2.5])
def test_batch_clarity_synthetic_screenshots(batch, tk_root, scale):
    from tools.ui_visual_audit import capture_window_image
    view, accepted = batch(initial=("openai", "claude", "google_ai"), long_name=True)
    previous_scale = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(scale)
        _settle(tk_root, 1.1)
        view.geometry("920x780")
        _settle(tk_root)
        assert view._target_columns == (2 if scale == 1.0 else 1)
        assert view._apply_button.winfo_rooty() + view._apply_button.winfo_height() <= view.winfo_rooty() + view.winfo_height()
        assert view._body._parent_canvas.winfo_height() >= 180
        folder = Path(__file__).resolve().parent / "dist" / "proxy-simple-workflow-v2471" / "bulk"
        folder.mkdir(parents=True, exist_ok=True)
        label = view._target_labels["custom:demo"]
        long_text = label.cget("text")
        label.configure(text="演示自定义网站")
        _settle(tk_root)
        capture_window_image(view, onscreen=True).save(folder / f"choices-{int(scale * 100)}.png")
        label.configure(text=long_text)
        _settle(tk_root)
        view._body._parent_canvas.yview_moveto(1)
        _settle(tk_root)
        assert label.winfo_height() > view._target_labels["youtube"].winfo_height()
        assert label.winfo_rootx() + label.winfo_width() <= view._targets.winfo_rootx() + view._targets.winfo_width()
        assert view._targets.winfo_width() <= view._body._parent_canvas.winfo_width()
        capture_window_image(view, onscreen=True).save(folder / f"targets-{int(scale * 100)}.png")
        assert not accepted
    finally:
        ctk.set_widget_scaling(previous_scale)
        tk_root.update()
