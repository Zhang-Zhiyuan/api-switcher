from types import SimpleNamespace

import pytest

from core import tray_manager


def test_tray_menu_accepts_profile_actions(monkeypatch):
    if tray_manager.pystray is None:
        pytest.skip("pystray is not installed")

    monkeypatch.setattr(
        tray_manager.profile_manager,
        "list_switchable_claude_profiles",
        lambda: [SimpleNamespace(name="claude-relay")],
    )
    monkeypatch.setattr(
        tray_manager.profile_manager,
        "list_switchable_codex_profiles",
        lambda: [SimpleNamespace(name="codex-relay")],
    )
    monkeypatch.setattr(tray_manager.profile_manager, "get_current_claude_name", lambda: "claude-relay")
    monkeypatch.setattr(tray_manager.profile_manager, "get_active_claude_name", lambda: "")
    monkeypatch.setattr(tray_manager.profile_manager, "get_current_codex_name", lambda: "codex-relay")
    monkeypatch.setattr(tray_manager.profile_manager, "get_active_codex_name", lambda: "")
    monkeypatch.setattr(
        tray_manager.startup_manager,
        "get_startup_status",
        lambda: SimpleNamespace(supported=True, enabled=False),
    )

    manager = tray_manager.TrayManager(lambda *_: None, lambda *_: None)
    menu = manager.create_menu()

    assert menu
    assert any(item is tray_manager.pystray.Menu.SEPARATOR for item in menu)
