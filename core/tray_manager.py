"""System tray icon manager for quick profile switching."""
import logging
import threading
from typing import Callable, Optional
from PIL import Image, ImageDraw, ImageFont

try:
    import pystray
    from pystray import MenuItem as Item
except ImportError:
    pystray = None
    Item = None

from core import profile_manager, switcher

logger = logging.getLogger(__name__)


class TrayManager:
    """Manages system tray icon and menu."""

    def __init__(self, on_show_window: Callable, on_exit: Callable):
        self.on_show_window = on_show_window
        self.on_exit = on_exit
        self.icon: Optional[object] = None
        self._thread: Optional[threading.Thread] = None

    def create_icon_image(self) -> Image.Image:
        """Create a simple icon image for the tray."""
        # Create a 64x64 image with gradient blue background
        size = 64
        image = Image.new('RGB', (size, size), color=(45, 55, 72))
        draw = ImageDraw.Draw(image)

        # Draw gradient background
        for i in range(size):
            color_value = int(45 + (100 - 45) * (i / size))
            draw.rectangle([(0, i), (size, i + 1)], fill=(color_value, color_value + 20, color_value + 50))

        # Draw "API" text
        try:
            # Try to use a nice font, fallback to default if not available
            font = ImageFont.truetype("arial.ttf", 20)
        except Exception:
            font = ImageFont.load_default()

        text = "API"
        # Get text bounding box
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        # Center the text
        x = (size - text_width) // 2
        y = (size - text_height) // 2

        draw.text((x, y), text, fill=(255, 255, 255), font=font)

        return image

    def get_active_profiles_text(self) -> str:
        """Get text showing currently active API configurations."""
        claude_names = {p.name for p in profile_manager.list_switchable_claude_profiles()}
        codex_names = {p.name for p in profile_manager.list_switchable_codex_profiles()}
        active_claude = profile_manager.get_current_claude_name() or profile_manager.get_active_claude_name()
        active_codex = profile_manager.get_current_codex_name() or profile_manager.get_active_codex_name()

        parts = []
        if active_claude in claude_names:
            parts.append(f"Claude API: {active_claude}")
        if active_codex in codex_names:
            parts.append(f"Codex API: {active_codex}")

        return " | ".join(parts) if parts else "无激活 API 配置"

    def create_menu(self) -> tuple:
        """Create the tray menu."""
        if pystray is None or Item is None:
            return tuple()

        # Get active profiles
        active_claude = profile_manager.get_current_claude_name() or profile_manager.get_active_claude_name()
        active_codex = profile_manager.get_current_codex_name() or profile_manager.get_active_codex_name()

        menu_items = []

        # Show window item
        menu_items.append(Item('显示主窗口', self.on_show_window, default=True))
        menu_items.append(Item.SEPARATOR)

        # Current active API config section
        status_text = self.get_active_profiles_text()
        menu_items.append(Item(f'当前 API: {status_text}', None, enabled=False))
        menu_items.append(Item.SEPARATOR)

        # Claude API configs submenu
        claude_profiles = profile_manager.list_switchable_claude_profiles()
        if claude_profiles:
            claude_items = []
            for profile in claude_profiles[:10]:  # Limit to 10 profiles
                is_active = profile.name == active_claude
                label = f"{'✓ ' if is_active else ''}{profile.name}"
                claude_items.append(
                    Item(
                        label,
                        lambda _, name=profile.name: self._switch_claude(name),
                        checked=lambda item, name=profile.name: name == active_claude
                    )
                )
            menu_items.append(Item('Claude API 配置', pystray.Menu(*claude_items)))

        # Codex API configs submenu
        codex_profiles = profile_manager.list_switchable_codex_profiles()
        if codex_profiles:
            codex_items = []
            for profile in codex_profiles[:10]:  # Limit to 10 profiles
                is_active = profile.name == active_codex
                label = f"{'✓ ' if is_active else ''}{profile.name}"
                codex_items.append(
                    Item(
                        label,
                        lambda _, name=profile.name: self._switch_codex(name),
                        checked=lambda item, name=profile.name: name == active_codex
                    )
                )
            menu_items.append(Item('Codex API 配置', pystray.Menu(*codex_items)))

        menu_items.append(Item.SEPARATOR)

        # Refresh menu item
        menu_items.append(Item('刷新菜单', lambda: self.update_menu()))

        menu_items.append(Item.SEPARATOR)

        # Exit item
        menu_items.append(Item('退出', self._on_exit_clicked))

        return tuple(menu_items)

    def _switch_claude(self, name: str):
        """Switch Claude profile from tray menu."""
        try:
            switcher.switch_claude_profile(name)
            logger.info(f"Switched Claude profile to: {name} (from tray)")
            self.update_menu()
        except Exception as e:
            logger.error(f"Failed to switch Claude profile from tray: {e}")

    def _switch_codex(self, name: str):
        """Switch Codex profile from tray menu."""
        try:
            switcher.switch_codex_profile(name)
            logger.info(f"Switched Codex profile to: {name} (from tray)")
            self.update_menu()
        except Exception as e:
            logger.error(f"Failed to switch Codex profile from tray: {e}")

    def _on_exit_clicked(self, icon, item):
        """Handle exit menu item click."""
        self.stop()
        self.on_exit()

    def update_menu(self):
        """Update the tray menu with current profiles."""
        if self.icon and pystray is not None:
            self.icon.menu = pystray.Menu(*self.create_menu())
            # Update tooltip
            self.icon.title = f"API切换器 - {self.get_active_profiles_text()}"

    def start(self):
        """Start the tray icon in a separate thread."""
        if pystray is None:
            logger.warning("pystray is not installed; tray icon is disabled")
            return

        if self.icon is not None:
            logger.warning("Tray icon already running")
            return

        def run_icon():
            try:
                image = self.create_icon_image()
                menu = pystray.Menu(*self.create_menu())

                self.icon = pystray.Icon(
                    "api_switcher",
                    image,
                    f"API切换器 - {self.get_active_profiles_text()}",
                    menu
                )

                logger.info("Starting tray icon")
                self.icon.run()
            except Exception as e:
                logger.error(f"Tray icon error: {e}", exc_info=True)

        self._thread = threading.Thread(target=run_icon, daemon=True)
        self._thread.start()
        logger.info("Tray icon thread started")

    def stop(self):
        """Stop the tray icon."""
        if self.icon:
            logger.info("Stopping tray icon")
            self.icon.stop()
            self.icon = None

    def is_running(self) -> bool:
        """Check if tray icon is running."""
        return self.icon is not None

    def is_available(self) -> bool:
        """Check if tray support is available in this environment."""
        return pystray is not None
