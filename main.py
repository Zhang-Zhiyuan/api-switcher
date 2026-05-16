import sys
import os
import logging
import argparse
from datetime import datetime
from argparse import Namespace

# Ensure the app directory is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)


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
    from core.log_handler import log_manager

    migrated_storage_items = ensure_storage_dirs()
    logs_dir = STORAGE_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"app_{datetime.now().strftime('%Y%m%d')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
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

        pulse("正在加载界面组件...")
        import customtkinter as ctk

        ctk.set_default_color_theme("blue")
        ctk.set_appearance_mode("dark")

        pulse("正在创建主窗口...")
        from ui.app import App

        app = App(start_minimized=args.start_minimized)
        pulse("即将完成...")
        if splash:
            if not args.start_minimized:
                try:
                    app.update_idletasks()
                    app.update()
                except Exception:
                    logger.exception("Failed to draw main window before closing splash")
            splash.keep_visible_for(0.45)
            splash.close()
            splash = None
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
