import sys
import os
import logging

# Ensure the app directory is in the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.paths import STORAGE_DIR, BACKUPS_DIR

# Ensure storage dirs exist
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

# Create logs directory
LOGS_DIR = STORAGE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Setup logging
from datetime import datetime
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
from core.log_handler import log_manager
log_manager.initialize()

logger = logging.getLogger(__name__)
logger.info("Application starting...")


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
