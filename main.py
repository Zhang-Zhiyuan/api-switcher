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


def main(argv: list[str] | None = None):
    args = parse_args(argv)
    log_manager = configure_logging()

    import customtkinter as ctk
    ctk.set_default_color_theme("blue")
    ctk.set_appearance_mode("dark")

    from ui.app import App
    app = App(start_minimized=args.start_minimized)

    try:
        app.mainloop()
    except Exception as e:
        logger.error(f"Application error: {e}", exc_info=True)
    finally:
        logger.info("Application shutting down...")
        log_manager.shutdown()


if __name__ == "__main__":
    main()
