from pathlib import Path

# Target config files
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CLAUDE_CONFIG = Path.home() / ".claude" / "config.json"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
VSCODE_SETTINGS = Path.home() / "AppData" / "Roaming" / "Code" / "User" / "settings.json"

# Local storage
APP_DIR = Path(__file__).parent.parent
STORAGE_DIR = APP_DIR / "storage"
PROFILES_FILE = STORAGE_DIR / "profiles.json"
BACKUPS_DIR = STORAGE_DIR / "backups"
SECRETS_DIR = STORAGE_DIR / "secrets"

# Keyring service name
KEYRING_SERVICE = "api-switcher"
