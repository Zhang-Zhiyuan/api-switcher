"""System tray icon manager for quick profile switching."""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Callable

from config.paths import APP_DIR

logger = logging.getLogger(__name__)

pystray = None
Item = None
profile_manager = None
startup_manager = None
switcher = None
_pystray_imported = False
_app_managers_imported = False


def _load_pystray():
    global Item, _pystray_imported, pystray

    if _pystray_imported:
        return pystray, Item

    try:
        import pystray as pystray_module
        from pystray import MenuItem
    except ImportError:
        pystray = None
        Item = None
    else:
        pystray = pystray_module
        Item = MenuItem
    _pystray_imported = True
    return pystray, Item


def _load_app_managers() -> None:
    global _app_managers_imported, profile_manager, startup_manager, switcher

    if _app_managers_imported:
        return

    from core import profile_manager as profile_manager_module
    from core import startup_manager as startup_manager_module
    from core import switcher as switcher_module

    profile_manager = profile_manager or profile_manager_module
    startup_manager = startup_manager or startup_manager_module
    switcher = switcher or switcher_module
    _app_managers_imported = True


def _profile_checked(name: str, active_name: str | None):
    def checked(_item):
        return name == active_name

    return checked


class TrayManager:
    """Manages the system tray icon and menu."""

    def __init__(
        self,
        on_show_window: Callable,
        on_exit: Callable,
        on_startup_changed: Callable | None = None,
        on_hide_window: Callable | None = None,
    ):
        self.on_show_window = on_show_window
        self.on_exit = on_exit
        self.on_startup_changed = on_startup_changed
        self.on_hide_window = on_hide_window
        self.icon: object | None = None
        self._thread: threading.Thread | None = None

    def create_icon_image(self):
        """Create the tray icon image."""
        from PIL import Image, ImageDraw, ImageFont

        for base in _resource_roots():
            for name in ("icon.png", "icon.ico"):
                icon_path = base / name
                if not icon_path.exists():
                    continue
                try:
                    return Image.open(icon_path).convert("RGBA").resize((64, 64), Image.LANCZOS)
                except Exception as e:
                    logger.debug("Failed to load tray icon %s: %s", icon_path, e)

        size = 64
        image = Image.new("RGB", (size, size), color=(18, 27, 41))
        draw = ImageDraw.Draw(image)

        for i in range(size):
            color_value = int(28 + (80 - 28) * (i / size))
            draw.rectangle([(0, i), (size, i + 1)], fill=(color_value, color_value + 16, color_value + 44))

        try:
            font = ImageFont.truetype("arial.ttf", 20)
        except Exception:
            font = ImageFont.load_default()

        text = "API"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (size - text_width) // 2
        y = (size - text_height) // 2

        draw.text((x, y), text, fill=(255, 255, 255), font=font)
        return image

    def get_active_profiles_text(self) -> str:
        """Get text showing currently active API configurations."""
        _load_app_managers()

        claude_names = {p.name for p in profile_manager.list_switchable_claude_profiles()}
        codex_names = {p.name for p in profile_manager.list_switchable_codex_profiles()}
        active_claude = profile_manager.get_current_claude_name() or profile_manager.get_active_claude_name()
        active_codex = profile_manager.get_current_codex_name() or profile_manager.get_active_codex_name()

        parts = []
        if active_claude in claude_names:
            parts.append(f"Claude API: {active_claude}")
        if active_codex in codex_names:
            parts.append(f"Codex API: {active_codex}")

        return " | ".join(parts) if parts else "无活动 API 配置"

    def create_menu(self) -> tuple:
        """Create the tray menu."""
        pystray_module, menu_item = _load_pystray()
        if pystray_module is None or menu_item is None:
            return tuple()
        _load_app_managers()

        active_claude = profile_manager.get_current_claude_name() or profile_manager.get_active_claude_name()
        active_codex = profile_manager.get_current_codex_name() or profile_manager.get_active_codex_name()

        menu_items = [
            menu_item("显示主窗口", self.on_show_window, default=True),
        ]
        if self.on_hide_window is not None:
            menu_items.append(menu_item("隐藏到托盘", self.on_hide_window))
        menu_items.append(pystray_module.Menu.SEPARATOR)

        menu_items.append(menu_item(f"当前 API: {self.get_active_profiles_text()}", None, enabled=False))
        menu_items.append(pystray_module.Menu.SEPARATOR)

        claude_profiles = profile_manager.list_switchable_claude_profiles()
        if claude_profiles:
            claude_items = [
                menu_item(
                    profile.name,
                    self._switch_claude_action(profile.name),
                    checked=_profile_checked(profile.name, active_claude),
                )
                for profile in claude_profiles[:10]
            ]
            if len(claude_profiles) > 10:
                claude_items.append(menu_item(f"仅显示前 10 个，共 {len(claude_profiles)} 个", None, enabled=False))
            menu_items.append(menu_item("Claude API 配置", pystray_module.Menu(*claude_items)))

        codex_profiles = profile_manager.list_switchable_codex_profiles()
        if codex_profiles:
            codex_items = [
                menu_item(
                    profile.name,
                    self._switch_codex_action(profile.name),
                    checked=_profile_checked(profile.name, active_codex),
                )
                for profile in codex_profiles[:10]
            ]
            if len(codex_profiles) > 10:
                codex_items.append(menu_item(f"仅显示前 10 个，共 {len(codex_profiles)} 个", None, enabled=False))
            menu_items.append(menu_item("Codex API 配置", pystray_module.Menu(*codex_items)))

        menu_items.append(pystray_module.Menu.SEPARATOR)

        startup_status = startup_manager.get_startup_status()
        if startup_status.supported:
            menu_items.append(
                menu_item(
                    "开机自启动",
                    self._toggle_startup,
                    checked=lambda _item: startup_manager.get_startup_status().enabled,
                )
            )
        menu_items.append(menu_item("刷新菜单", lambda _icon=None, _item=None: self.update_menu()))

        menu_items.append(pystray_module.Menu.SEPARATOR)
        menu_items.append(menu_item("退出", self._on_exit_clicked))
        return tuple(menu_items)

    def _switch_claude_action(self, name: str):
        def action(_icon=None, _item=None):
            self._switch_claude(name)

        return action

    def _switch_codex_action(self, name: str):
        def action(_icon=None, _item=None):
            self._switch_codex(name)

        return action

    def _switch_claude(self, name: str):
        """Switch Claude profile from tray menu."""
        _load_app_managers()
        try:
            switcher.switch_claude_profile(name)
            logger.info("Switched Claude profile to: %s (from tray)", name)
            self.update_menu()
        except Exception as e:
            logger.error("Failed to switch Claude profile from tray: %s", e, exc_info=True)

    def _switch_codex(self, name: str):
        """Switch Codex profile from tray menu."""
        _load_app_managers()
        try:
            switcher.switch_codex_profile(name)
            logger.info("Switched Codex profile to: %s (from tray)", name)
            self.update_menu()
        except Exception as e:
            logger.error("Failed to switch Codex profile from tray: %s", e, exc_info=True)

    def _on_exit_clicked(self, icon=None, item=None):
        """Handle exit menu item click."""
        self.stop()
        self.on_exit()

    def _toggle_startup(self, icon=None, item=None):
        """Toggle Windows startup from the tray menu."""
        _load_app_managers()
        try:
            status = startup_manager.get_startup_status()
            startup_manager.set_startup_enabled(not status.enabled)
            self.update_menu()
            if self.on_startup_changed:
                self.on_startup_changed()
        except Exception as e:
            logger.error("Failed to toggle startup from tray: %s", e, exc_info=True)

    def update_menu(self):
        """Update the tray menu with current profiles."""
        pystray_module, _menu_item = _load_pystray()
        if not self.icon or pystray_module is None:
            return
        try:
            self.icon.menu = pystray_module.Menu(*self.create_menu())
            self.icon.title = _tooltip_text(self.get_active_profiles_text())
        except Exception as e:
            logger.error("Failed to update tray menu: %s", e, exc_info=True)

    def start(self):
        """Start the tray icon in a separate thread."""
        pystray_module, _menu_item = _load_pystray()
        if pystray_module is None:
            logger.warning("pystray is not installed; tray icon is disabled")
            return

        if self.icon is not None:
            logger.warning("Tray icon already running")
            return

        try:
            icon = pystray_module.Icon(
                "api_switcher",
                self.create_icon_image(),
                _tooltip_text(self.get_active_profiles_text()),
                pystray_module.Menu(*self.create_menu()),
            )
            self.icon = icon
        except Exception as e:
            logger.error("Failed to initialize tray icon: %s", e, exc_info=True)
            self.icon = None
            return

        def run_icon():
            icon = self.icon
            if icon is None:
                return
            try:
                logger.info("Starting tray icon")
                icon.run()
            except Exception as e:
                logger.error("Tray icon error: %s", e, exc_info=True)
            finally:
                if self.icon is icon:
                    self.icon = None

        self._thread = threading.Thread(target=run_icon, daemon=True)
        self._thread.start()
        logger.info("Tray icon thread started")

    def stop(self):
        """Stop the tray icon."""
        if not self.icon:
            return
        logger.info("Stopping tray icon")
        icon = self.icon
        self.icon = None
        icon.stop()

    def is_running(self) -> bool:
        """Check if tray icon is running."""
        return self.icon is not None

    def is_available(self) -> bool:
        """Check if tray support is available in this environment."""
        pystray_module, _menu_item = _load_pystray()
        return pystray_module is not None

    def notify(self, message: str, title: str = "API切换器") -> None:
        """Show a best-effort tray notification."""
        if not self.icon or not hasattr(self.icon, "notify"):
            return
        try:
            self.icon.notify(message, title)
        except Exception as e:
            logger.debug("Tray notification failed: %s", e)


def _resource_roots() -> list[Path]:
    roots = [Path(getattr(sys, "_MEIPASS", APP_DIR)), APP_DIR]
    unique: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved not in seen:
            unique.append(root)
            seen.add(resolved)
    return unique


def _tooltip_text(active_text: str) -> str:
    text = f"API切换器 - {active_text}"
    return text if len(text) <= 120 else text[:117] + "..."
