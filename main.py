import sys
import os
import logging
import argparse
import threading
from datetime import datetime
from argparse import Namespace

# Ensure the app directory is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)
SPLASH_WINDOW_POLL_MS = 16


def parse_args(argv: list[str] | None = None) -> Namespace:
    parser = argparse.ArgumentParser(description="API Switcher")
    parser.add_argument(
        "--minimized",
        "--start-minimized",
        "--tray",
        action="store_true",
        dest="start_minimized",
        help="Start hidden in the system tray when tray support is available.",
    )
    parser.add_argument(
        "--no-splash",
        action="store_true",
        help="Disable the startup splash window.",
    )
    parser.add_argument(
        "--splash-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args, _unknown = parser.parse_known_args(argv)
    return args


def configure_logging():
    from config.paths import STORAGE_DIR, STORAGE_DIR_SOURCE, STORAGE_DIR_WARNINGS, ensure_storage_dirs
    from core.log_handler import ExpectedLibraryNoiseFilter, log_manager

    migrated_storage_items = ensure_storage_dirs()
    logs_dir = STORAGE_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"app_{datetime.now().strftime('%Y%m%d')}.log"

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    stream_handler = logging.StreamHandler()
    # Attach the normalizer to both outputs. The first handler to receive a
    # Paramiko socket-close record downgrades it in-place, so every later
    # handler (including the GUI viewer) sees the same severity.
    file_handler.addFilter(ExpectedLibraryNoiseFilter())
    stream_handler.addFilter(ExpectedLibraryNoiseFilter())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            file_handler,
            stream_handler,
        ],
    )

    log_manager.initialize()
    logger.info("Application starting...")
    logger.info("Data directory: %s (%s)", STORAGE_DIR, STORAGE_DIR_SOURCE)
    for warning in STORAGE_DIR_WARNINGS:
        logger.warning(warning)
    if migrated_storage_items:
        logger.info("Migrated legacy storage items: %s", ", ".join(migrated_storage_items))
    return log_manager


def flush_usage_session() -> None:
    try:
        from core.usage_recorder import usage_recorder

        usage_recorder.end_session()
    except Exception:
        logger.exception("Failed to flush usage session")


def reconcile_auto_continue_installations(manager=None) -> dict[str, bool]:
    """Upgrade previously installed local hooks without blocking app startup on failure."""
    try:
        if manager is None:
            from core.auto_continue.manager import auto_continue_manager

            manager = auto_continue_manager
        results = manager.reconcile_all_installations()
    except Exception:
        logger.warning("Failed to reconcile local auto-continue hooks", exc_info=True)
        return {}

    failed = sorted(name for name, repaired in results.items() if not repaired)
    if failed:
        logger.warning("Some local auto-continue hooks could not be reconciled: %s", ", ".join(failed))
    return results


def _schedule_startup_splash_close(app, splash, min_visible_seconds: float = 0.45) -> None:
    """Close the splash after the main window maps without nesting Tk's event loop."""

    def close_when_mapped() -> None:
        try:
            if not app.winfo_ismapped():
                app.after(SPLASH_WINDOW_POLL_MS, close_when_mapped)
                return
        except Exception:
            pass
        threading.Thread(
            target=splash.close,
            name="startup-splash-close",
            daemon=True,
        ).start()

    delay_ms = splash.remaining_visible_ms(min_visible_seconds)
    try:
        app.after(delay_ms, close_when_mapped)
    except Exception:
        threading.Thread(target=splash.close, daemon=True).start()


def main(argv: list[str] | None = None):
    args = parse_args(argv)
    if args.splash_child:
        from ui.startup_splash import run_splash_process

        return run_splash_process()

    splash = None
    if not args.start_minimized and not args.no_splash:
        try:
            from ui.startup_splash import StartupSplash

            splash = StartupSplash()
        except Exception:
            splash = None

    def pulse(message: str) -> None:
        if splash:
            splash.pulse(message)

    log_manager = None
    try:
        pulse("正在准备配置...")
        log_manager = configure_logging()

        pulse("正在校验自动继续 Hook...")
        reconcile_auto_continue_installations()

        pulse("正在加载界面组件...")
        import customtkinter as ctk

        ctk.set_default_color_theme("blue")
        ctk.set_appearance_mode("dark")

        pulse("正在创建主窗口...")
        from ui.app import App

        app = App(start_minimized=args.start_minimized)
        pulse("即将完成...")
        if splash:
            _schedule_startup_splash_close(app, splash)
        app.mainloop()
    except Exception as e:
        logger.error(f"Application error: {e}", exc_info=True)
        return 1
    finally:
        if splash:
            splash.close()
        flush_usage_session()
        logger.info("Application shutting down...")
        if log_manager:
            log_manager.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
