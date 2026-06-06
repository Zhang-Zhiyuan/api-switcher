import json

from core import auth_parser, parser


def test_claude_settings_cache_reuses_reads_updates_on_write_and_detects_external_write(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(parser, "CLAUDE_SETTINGS", settings_path)
    settings_path.write_text(json.dumps({"model": "first"}), encoding="utf-8")
    parser.clear_claude_file_cache()

    original_read_text = type(settings_path).read_text
    read_count = {"value": 0}

    def counting_read_text(self, *args, **kwargs):
        if self == settings_path:
            read_count["value"] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(settings_path), "read_text", counting_read_text)

    assert parser.read_claude_settings()["model"] == "first"
    assert parser.read_claude_settings()["model"] == "first"
    assert read_count["value"] == 1

    parser.write_claude_settings({"model": "written"})
    assert parser.read_claude_settings()["model"] == "written"
    assert read_count["value"] == 1

    settings_path.write_text(json.dumps({"model": "external-change"}), encoding="utf-8")
    assert parser.read_claude_settings()["model"] == "external-change"
    assert read_count["value"] == 2


def test_codex_auth_cache_reuses_reads_and_detects_external_write(monkeypatch, tmp_path):
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", auth_path)
    auth_path.write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    auth_parser.clear_codex_auth_cache()

    original_read_text = type(auth_path).read_text
    read_count = {"value": 0}

    def counting_read_text(self, *args, **kwargs):
        if self == auth_path:
            read_count["value"] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(auth_path), "read_text", counting_read_text)

    assert auth_parser.read_codex_auth()["auth_mode"] == "chatgpt"
    assert auth_parser.read_codex_auth()["auth_mode"] == "chatgpt"
    assert read_count["value"] == 1

    auth_path.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk"}), encoding="utf-8")
    assert auth_parser.read_codex_auth()["auth_mode"] == "apikey"
    assert read_count["value"] == 2
