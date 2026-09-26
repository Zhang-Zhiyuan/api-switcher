"""Native routing previews/recovery with synthetic, isolated authority."""
import copy
import os
from pathlib import Path
import time

import customtkinter as ctk
import pytest

from core import proxy_routing
from ui.dialogs import service_routes_dialog as routes_ui
from test_service_routes_dialog import _catalog, _wait


@pytest.fixture
def editor_factory(tk_root):
    dialogs = []
    def create(preferences=None, *, recoverer=None):
        records = copy.deepcopy(preferences or {"合成服务器": {}})
        applied = []
        dialog = routes_ui.ServiceRoutesDialog(
            tk_root, scopes=list(records), load_preferences=lambda scope: records[scope],
            catalog_loader=_catalog, recover_preferences=recoverer,
            apply_preferences=lambda scope, prefs, expected: applied.append((scope, prefs)) or "合成应用成功",
        )
        dialogs.append(dialog)
        _wait(tk_root, lambda: not dialog._busy)
        return dialog, applied
    yield create
    for dialog in reversed(dialogs):
        if dialog.winfo_exists():
            dialog.destroy()
        tk_root.update()


def test_missing_authority_warns_before_rebuilding_and_never_auto_applies(editor_factory, tk_root, monkeypatch):
    dialog, applied = editor_factory({"SSH 合成": {"_authority_missing": True}}, recoverer=lambda scope: {})
    assert dialog._recovery_button.cget("state") == "normal"
    assert not dialog._changes
    confirmations = []
    monkeypatch.setattr(routes_ui, "ConfirmDialog", lambda *args, **kwargs: confirmations.append(kwargs))
    dialog._apply()
    assert not applied and not dialog._busy
    assert "重建远端规则" in confirmations[0]["message"]
    confirmations[0]["on_confirm"]()
    _wait(tk_root, lambda: not dialog._busy)
    assert len(applied) == 1
    assert dialog._recovery_button.cget("state") == "disabled"


def test_recovery_updates_only_current_scope_without_applier(editor_factory, tk_root):
    recovered = proxy_routing.normalize_routes({"builtin_sites": {"youtube": True},
                                                "service_route_modes": {"youtube": "direct", "openai": "default"}})
    calls = []
    dialog, applied = editor_factory({"SSH A": {"_authority_missing": True}, "SSH B": {"_authority_missing": True}},
                                     recoverer=lambda scope: calls.append(scope) or recovered)
    untouched = copy.deepcopy(dialog._drafts["SSH B"])
    dialog._recover_routes()
    _wait(tk_root, lambda: not dialog._busy)
    assert calls == ["SSH A"] and not applied
    assert dialog._drafts["SSH A"] == dialog._originals["SSH A"] == recovered
    assert dialog._drafts["SSH B"] == untouched
    assert dialog._rows["youtube"]["profile"].get() == routes_ui.DIRECT_PROFILE
    assert not dialog._changes and "远端代理未改变" in dialog._status.cget("text")
    dialog._switch_scope("SSH B")
    assert dialog._recovery_button.cget("state") == "normal"


def test_recovery_failure_preserves_draft_and_allows_retry(editor_factory, tk_root, monkeypatch):
    def fail(scope):
        raise ValueError("合成损坏记录")
    dialog, applied = editor_factory({"SSH A": {"_authority_missing": True}}, recoverer=fail)
    dialog._select_profile("youtube", routes_ui.DIRECT_PROFILE)
    before = copy.deepcopy(dialog._drafts)
    confirmations = []
    monkeypatch.setattr(routes_ui, "ConfirmDialog", lambda *args, **kwargs: confirmations.append(kwargs))
    dialog._recover_routes()
    assert not dialog._busy and len(confirmations) == 1
    confirmations[0]["on_confirm"]()
    _wait(tk_root, lambda: not dialog._busy)
    assert dialog._drafts == before and not applied
    assert "合成损坏记录" in dialog._status.cget("text")
    assert dialog._recovery_button.cget("state") == "normal"


def test_known_strict_privacy_conflict_stops_all_pending_scopes_before_apply(editor_factory):
    dialog, applied = editor_factory({"Win 合成": {"strict_privacy": True}, "SSH 合成": {}})
    dialog._select_profile("youtube", routes_ui.DIRECT_PROFILE)
    dialog._switch_scope("SSH 合成")
    dialog._select_profile("google", routes_ui.DIRECT_PROFILE)
    dialog._apply()
    assert not applied and not dialog._busy
    assert "未应用" in dialog._status.cget("text") and "Win 合成" in dialog._status.cget("text")
    dialog._switch_scope("Win 合成")
    assert "无法应用：直连目标" in dialog._preview.get("1.0", "end")


@pytest.mark.parametrize("geometry,scale", [("1040x760", 1.0), ("720x680", 1.0), ("860x720", 1.25)])
def test_rule_preview_layout_and_capture(editor_factory, tk_root, geometry, scale):
    prefs = proxy_routing.normalize_routes({"builtin_sites": {"google": True, "youtube": True},
        "service_route_modes": {"google": "direct", "youtube": "direct", "custom:own": "default"},
        "custom_targets": [{"id": "own", "value": "youtube.com"}]})
    dialog, applied = editor_factory({"SSH 合成服务器": {**prefs, "_authority_missing": True}}, recoverer=lambda scope: prefs)
    try:
        ctk.set_widget_scaling(scale)
        settle = time.monotonic() + 1.1
        while time.monotonic() < settle:
            tk_root.update()
            time.sleep(0.02)
        dialog.geometry(geometry)
        dialog._set_category("网站")
        dialog._toggle_preview()
        dialog.lift()
        settle = time.monotonic() + 0.5
        while time.monotonic() < settle:
            tk_root.update()
            time.sleep(0.02)
        width, height = (int(value) for value in geometry.split("x"))
        assert abs(dialog.winfo_width() - width * dialog._get_window_scaling()) <= 2
        assert abs(dialog.winfo_height() - height * dialog._get_window_scaling()) <= 2
        assert dialog._table.winfo_height() >= 100 * dialog._get_widget_scaling()
        text = dialog._preview.get("1.0", "end")
        assert "youtube.com：覆盖" in text and "googleapis.com" in text
        assert "本机尚无此服务器" in text and "非运行态" in text
        for widget in (dialog._recovery_button, dialog._save_button, dialog._preview_toggle, dialog._preview):
            assert widget.winfo_ismapped()
            assert widget.winfo_rootx() >= dialog.winfo_rootx()
            assert widget.winfo_rootx() + widget.winfo_width() <= dialog.winfo_rootx() + dialog.winfo_width()
            assert widget.winfo_rooty() + widget.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
        if output := os.environ.get("API_SWITCHER_ROUTE_PREFLIGHT_CAPTURE_DIR"):
            from tools.ui_visual_audit import capture_window_image
            directory = Path(output).resolve()
            assert directory.is_relative_to(Path(__file__).resolve().parent / "dist")
            directory.mkdir(parents=True, exist_ok=True)
            capture_window_image(dialog).save(directory / f"preflight-{geometry}-{scale}.png")
        assert not applied
    finally:
        ctk.set_widget_scaling(1.0)
