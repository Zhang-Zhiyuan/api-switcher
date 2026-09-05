import copy
from pathlib import Path
import time

import customtkinter as ctk
import pytest

from core import proxy_routing
from ui.dialogs.service_routes_dialog import DEFAULT_PROFILE, MISSING_NODE, ServiceRoutesDialog


def _catalog():
    return [
        {"id": "home", "name": "家宽订阅 A", "nodes": [
            {"key": "one", "label": "日本 · 家宽 01"}, {"key": "two", "label": "美国 · 家宽 02"},
        ]},
        {"id": "dc", "name": "机房订阅 B", "nodes": [{"key": "three", "label": "香港 · 流媒体 01"}]},
    ]


def _preferences():
    return proxy_routing.route_snapshot({
        "builtin_sites": {"youtube": True},
        "service_profile_bindings": {"openai": "home", "claude": "home", "youtube": "dc"},
        "service_node_bindings": {"openai": "one", "claude": "two", "youtube": "three"},
    })


def _wait(root, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    root.update()
    assert predicate(), "editor worker did not finish"


@pytest.fixture(scope="module")
def tk_root():
    appearance = ctk.get_appearance_mode()
    ctk.set_appearance_mode("dark")
    try:
        root = ctk.CTk()
    except Exception as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()
    ctk.set_appearance_mode(appearance)


@pytest.fixture
def editor(tk_root):
    root = tk_root
    saved = []
    dialog = ServiceRoutesDialog(
        root, scopes=["SSH 开发服务器", "SSH 推理服务器"],
        load_preferences=lambda _scope: _preferences(),
        catalog_loader=_catalog,
        apply_preferences=lambda scope, prefs, expected: saved.append((scope, copy.deepcopy(prefs), expected)) or "已应用",
    )
    try:
        _wait(root, lambda: not dialog._busy)
        yield root, dialog, saved
    finally:
        if dialog.winfo_exists():
            dialog.destroy()


def test_editor_changes_are_drafts_until_explicit_apply(editor):
    root, dialog, saved = editor
    assert dialog._scope_combo.get() == dialog._scope
    assert dialog._rows["google_ai"]["node"].get() == DEFAULT_PROFILE
    dialog._select_node("claude", "日本 · 家宽 01")
    assert saved == []
    assert dialog._originals[dialog._scope]["service_node_bindings"]["claude"] == "two"
    dialog._apply()
    _wait(root, lambda: not dialog._busy)
    assert len(saved) == 1
    assert saved[0][1]["service_node_bindings"]["claude"] == "one"
    assert dialog._originals[dialog._scope] == dialog._drafts[dialog._scope]


def test_editor_reselecting_profile_preserves_pinned_node(editor):
    _root, dialog, _saved = editor
    dialog._select_profile("claude", "家宽订阅 A")
    assert dialog._drafts[dialog._scope]["service_node_bindings"]["claude"] == "two"
    dialog._select_profile("claude", "机房订阅 B")
    assert "claude" not in dialog._drafts[dialog._scope]["service_node_bindings"]


def test_server_switch_preserves_drafts_and_copy_is_independent(editor):
    _root, dialog, _saved = editor
    first, second = dialog._scopes
    dialog._select_profile("claude", DEFAULT_PROFILE)
    dialog._switch_scope(second)
    assert dialog._drafts[second]["service_profile_bindings"]["claude"] == "home"
    dialog._switch_scope(first)
    dialog._copy_to_scopes()
    assert "claude" not in dialog._drafts[second]["service_profile_bindings"]
    dialog._drafts[first]["service_profile_bindings"]["claude"] = "dc"
    assert "claude" not in dialog._drafts[second]["service_profile_bindings"]


def test_missing_node_is_shown_and_not_replaced_by_default(editor):
    _root, dialog, _saved = editor
    dialog._drafts[dialog._scope]["service_node_bindings"]["claude"] = "missing"
    dialog._refresh_row("claude")
    assert dialog._rows["claude"]["node"].get() == MISSING_NODE
    assert dialog._drafts[dialog._scope]["service_node_bindings"]["claude"] == "missing"


def test_failed_apply_preserves_draft_and_successful_servers_are_not_reapplied(editor):
    root, dialog, saved = editor
    first, second = dialog._scopes
    dialog._select_profile("claude", "机房订阅 B")
    dialog._copy_to_scopes()
    def apply(scope, preferences, _expected):
        if scope == second:
            raise RuntimeError("服务器暂时无法连接")
        saved.append(scope)
        return "已应用"
    dialog._applier = apply
    dialog._apply()
    _wait(root, lambda: not dialog._busy)
    assert saved == [first]
    assert dialog._originals[first] == dialog._drafts[first]
    assert dialog._originals[second] != dialog._drafts[second]
    dialog._apply()
    _wait(root, lambda: not dialog._busy)
    assert saved == [first]
    assert "服务器暂时无法连接" in dialog._details.get("1.0", "end")


def test_custom_target_add_remove_and_invalid_input_feedback(editor):
    _root, dialog, saved = editor
    dialog._custom_entry.insert(0, "https://api.example.com/v1")
    dialog._add_custom()
    target = dialog._drafts[dialog._scope]["custom_targets"][0]
    service = f"custom:{target['id']}"
    assert target["value"] == "api.example.com"
    dialog._select_profile(service, "家宽订阅 A")
    dialog._select_node(service, "日本 · 家宽 01")
    dialog._remove_custom(service)
    assert service not in dialog._drafts[dialog._scope]["service_node_bindings"]
    assert service not in dialog._drafts[dialog._scope]["service_profile_bindings"]
    dialog._custom_entry.insert(0, "bad,domain")
    dialog._add_custom()
    assert "每次只能添加" in dialog._status.cget("text")
    assert not saved


def capture_preview(directory):
    """Isolated visual verification; no real preferences or SSH connections."""
    from PIL import ImageGrab

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.withdraw()
    dialog = ServiceRoutesDialog(
        root, scopes=["SSH 开发服务器", "SSH 推理服务器"],
        load_preferences=lambda _scope: _preferences(), catalog_loader=_catalog,
        apply_preferences=lambda *_args: "隔离演示：规则已应用",
    )
    try:
        _wait(root, lambda: not dialog._busy)
        for label, geometry in (("wide", "1040x760"), ("narrow", "720x680")):
            dialog.geometry(geometry)
            for _ in range(15):
                root.update()
                time.sleep(0.03)
            ImageGrab.grab(bbox=(dialog.winfo_rootx(), dialog.winfo_rooty(),
                                dialog.winfo_rootx() + dialog.winfo_width(),
                                dialog.winfo_rooty() + dialog.winfo_height())).save(directory / f"routes-{label}.png")
    finally:
        dialog.destroy()
        root.destroy()


if __name__ == "__main__":
    import sys
    capture_preview(sys.argv[1])
