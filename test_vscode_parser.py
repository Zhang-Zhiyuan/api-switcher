import json

from core import vscode_parser


def test_read_vscode_settings_returns_empty_for_non_object_json(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(vscode_parser, "VSCODE_SETTINGS", settings_path)

    assert vscode_parser.read_vscode_settings() == {}


def test_vscode_settings_cache_reuses_reads_updates_on_write_and_detects_external_write(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(vscode_parser, "VSCODE_SETTINGS", settings_path)
    settings_path.write_text(json.dumps({"claudeCode.selectedModel": "first"}), encoding="utf-8")
    vscode_parser.clear_vscode_settings_cache()

    original_read_text = type(settings_path).read_text
    read_count = {"value": 0}

    def counting_read_text(self, *args, **kwargs):
        if self == settings_path:
            read_count["value"] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(settings_path), "read_text", counting_read_text)

    first = vscode_parser.read_vscode_settings()
    first["claudeCode.selectedModel"] = "mutated"
    second = vscode_parser.read_vscode_settings()

    assert second["claudeCode.selectedModel"] == "first"
    assert read_count["value"] == 1

    vscode_parser.write_vscode_settings({"claudeCode.selectedModel": "written"})
    assert vscode_parser.read_vscode_settings()["claudeCode.selectedModel"] == "written"
    assert read_count["value"] == 1

    settings_path.write_text(
        json.dumps({"claudeCode.selectedModel": "external-change", "marker": True}),
        encoding="utf-8",
    )
    assert vscode_parser.read_vscode_settings()["claudeCode.selectedModel"] == "external-change"
    assert read_count["value"] == 2


def test_apply_permissions_keeps_mode_and_prompt_skip_separate():
    settings = vscode_parser.apply_permissions({}, bypass=False, skip_dangerous=True)

    assert settings["claudeCode.initialPermissionMode"] == "default"
    assert settings["claudeCode.allowDangerouslySkipPermissions"] is True

    settings = vscode_parser.apply_permissions(settings, bypass=True, skip_dangerous=False)

    assert settings["claudeCode.initialPermissionMode"] == "bypassPermissions"
    assert settings["claudeCode.allowDangerouslySkipPermissions"] is False


def test_apply_permission_mode_supports_edit_automatically_and_auto():
    settings = vscode_parser.apply_permission_mode({}, "acceptEdits", skip_dangerous=False)

    assert settings["claudeCode.initialPermissionMode"] == "acceptEdits"
    assert settings["claudeCode.allowDangerouslySkipPermissions"] is False

    settings = vscode_parser.apply_permission_mode(settings, "dontAsk", skip_dangerous=False)

    assert settings["claudeCode.initialPermissionMode"] == "dontAsk"
    assert settings["claudeCode.allowDangerouslySkipPermissions"] is False

    settings = vscode_parser.apply_permission_mode(
        {"claudeCode.initialPermissionMode": "acceptEdits"},
        "auto",
        skip_dangerous=False,
    )

    assert "claudeCode.initialPermissionMode" not in settings
