import json
from pathlib import Path

import pytest

from core.auto_continue.base import AutoContinueProvider
from models.auto_continue import AutoContinueSettings


class _CleanupProvider(AutoContinueProvider):
    def __init__(self, config_dir: Path):
        super().__init__("codex")
        self._config_dir = config_dir

    def get_config_dir(self) -> Path:
        return self._config_dir

    def get_hook_script_path(self) -> Path:
        return self._config_dir / "auto_continue_stop.ps1"

    def get_settings_path(self) -> Path:
        return self._config_dir / "auto_continue_settings.json"

    def is_hook_registered(self) -> bool:
        return False

    def register_hook(self) -> None:
        return None

    def unregister_hook(self) -> None:
        return None

    def install_hook_script(self) -> None:
        return None

    def uninstall_hook_script(self) -> None:
        self.get_hook_script_path().unlink(missing_ok=True)


def test_max_stagnant_continuations_defaults_validates_and_round_trips():
    defaults = AutoContinueSettings()
    assert defaults.max_stagnant_continuations == 3
    assert defaults.to_dict()["max_stagnant_continuations"] == 3

    restored = AutoContinueSettings.from_dict({"max_stagnant_continuations": "7"})
    assert restored.max_stagnant_continuations == 7
    assert AutoContinueSettings.from_dict(restored.to_dict()).max_stagnant_continuations == 7

    fallback = AutoContinueSettings.from_dict({"max_stagnant_continuations": "not-a-number"})
    assert fallback.max_stagnant_continuations == 3

    for value in (0, 3, 20):
        ok, error = AutoContinueSettings(max_stagnant_continuations=value).validate()
        assert ok, error

    for value in (-1, 21, True, False, 1.5):
        ok, _error = AutoContinueSettings(max_stagnant_continuations=value).validate()
        assert not ok, value

    with pytest.raises(ValueError, match="max_stagnant_continuations"):
        AutoContinueSettings.from_dict({"max_stagnant_continuations": -1})


def test_local_uninstall_removes_only_valid_scope_reset_markers(tmp_path):
    config_dir = tmp_path / "config"
    state_dir = config_dir / "tmp"
    state_dir.mkdir(parents=True)
    provider = _CleanupProvider(config_dir)

    provider.get_settings_path().write_text(
        json.dumps(AutoContinueSettings().to_dict()),
        encoding="utf-8",
    )
    (state_dir / "auto_continue_stop_state.json").write_text("{}", encoding="utf-8")

    prefix = "auto_continue_stop_state.json.reset."
    valid_markers = [
        config_dir / f"{prefix}{'a' * 64}",
        state_dir / f"{prefix}{'A1' * 32}",
    ]
    invalid_neighbors = [
        state_dir / f"{prefix}{'b' * 63}",
        state_dir / f"{prefix}{'c' * 65}",
        state_dir / f"{prefix}{'g' * 64}",
        state_dir / f"{prefix}{'d' * 64}.tmp",
        state_dir / f"auto_continue_stop_state.json.resetx.{'e' * 64}",
    ]
    marker_named_directory = state_dir / f"{prefix}{'f' * 64}"

    for path in valid_markers + invalid_neighbors:
        path.write_text("marker", encoding="utf-8")
    marker_named_directory.mkdir()

    provider.uninstall()

    assert not provider.get_settings_path().exists()
    assert not (state_dir / "auto_continue_stop_state.json").exists()
    assert all(not path.exists() for path in valid_markers)
    assert all(path.exists() for path in invalid_neighbors)
    assert marker_named_directory.is_dir()
