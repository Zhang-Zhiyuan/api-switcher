from core import vscode_parser


def test_read_vscode_settings_returns_empty_for_non_object_json(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(vscode_parser, "VSCODE_SETTINGS", settings_path)

    assert vscode_parser.read_vscode_settings() == {}


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
