from types import SimpleNamespace

import pytest

from core import tray_manager


def test_tray_menu_accepts_profile_actions(monkeypatch):
    pystray_module, _menu_item = tray_manager._load_pystray()
    if pystray_module is None:
        pytest.skip("pystray is not installed")

    fake_profile_manager = SimpleNamespace(
        list_switchable_claude_profiles=lambda: [SimpleNamespace(name="claude-relay")],
        list_switchable_codex_profiles=lambda: [SimpleNamespace(name="codex-relay")],
        get_current_claude_name=lambda: "claude-relay",
        get_active_claude_name=lambda: "",
        get_current_codex_name=lambda: "codex-relay",
        get_active_codex_name=lambda: "",
    )
    fake_startup_manager = SimpleNamespace(
        get_startup_status=lambda: SimpleNamespace(supported=True, enabled=False),
    )
    monkeypatch.setattr(tray_manager, "profile_manager", fake_profile_manager)
    monkeypatch.setattr(tray_manager, "startup_manager", fake_startup_manager)
    monkeypatch.setattr(tray_manager, "_app_managers_imported", True)

    manager = tray_manager.TrayManager(lambda *_: None, lambda *_: None)
    menu = manager.create_menu()

    assert menu
    assert any(item is pystray_module.Menu.SEPARATOR for item in menu)


def test_tray_stop_stops_icon_and_joins_thread():
    stopped = []
    joined = []

    class FakeIcon:
        def stop(self):
            stopped.append(True)

    class FakeThread:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            joined.append(timeout)

    manager = tray_manager.TrayManager(lambda *_: None, lambda *_: None)
    manager.icon = FakeIcon()
    manager._thread = FakeThread()

    manager.stop(timeout=0.25)

    assert stopped == [True]
    assert joined == [0.25]
    assert manager.icon is None
    assert manager._thread is None


@pytest.mark.parametrize(
    ("profile_type", "method_name", "switch_method"),
    [
        ("claude", "_switch_claude", "switch_claude_profile"),
        ("codex", "_switch_codex", "switch_codex_profile"),
    ],
)
def test_tray_profile_switch_notifies_main_app(monkeypatch, profile_type, method_name, switch_method):
    switched = []
    changed = []
    fake_switcher = SimpleNamespace(**{switch_method: lambda name: switched.append(name)})
    monkeypatch.setattr(tray_manager, "switcher", fake_switcher)
    monkeypatch.setattr(tray_manager, "_app_managers_imported", True)

    manager = tray_manager.TrayManager(
        lambda *_: None,
        lambda *_: None,
        on_profile_changed=lambda kind, name: changed.append((kind, name)),
    )
    manager.update_menu = lambda: None

    getattr(manager, method_name)("relay")

    assert switched == ["relay"]
    assert changed == [(profile_type, "relay")]
