import sys
import os
import logging
from datetime import datetime

# Ensure the app directory is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.paths import STORAGE_DIR, STORAGE_DIR_SOURCE, STORAGE_DIR_WARNINGS, ensure_storage_dirs
from core.log_handler import log_manager

# Ensure storage dirs exist
migrated_storage_items = ensure_storage_dirs()

# Create logs directory
LOGS_DIR = STORAGE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Setup logging
log_file = LOGS_DIR / f"app_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Initialize GUI log handler
log_manager.initialize()

logger = logging.getLogger(__name__)
logger.info("Application starting...")
logger.info("Data directory: %s (%s)", STORAGE_DIR, STORAGE_DIR_SOURCE)
for warning in STORAGE_DIR_WARNINGS:
    logger.warning(warning)
if migrated_storage_items:
    logger.info("Migrated legacy storage items: %s", ", ".join(migrated_storage_items))


def main():
    import customtkinter as ctk
    ctk.set_default_color_theme("blue")
    ctk.set_appearance_mode("dark")

    from ui.app import App
    app = App()

    try:
        app.mainloop()
    except Exception as e:
        logger.error(f"Application error: {e}", exc_info=True)
    finally:
        logger.info("Application shutting down...")
        log_manager.shutdown()


if __name__ == "__main__":
    main()
