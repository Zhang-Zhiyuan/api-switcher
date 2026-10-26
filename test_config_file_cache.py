import json
import os

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


def test_codex_auth_cache_reuses_parsing_and_detects_external_write(monkeypatch, tmp_path):
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", auth_path)
    auth_path.write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    auth_parser.clear_codex_auth_cache()

    original_loads = json.loads
    parse_count = {"value": 0}

    def counting_loads(*args, **kwargs):
        parse_count["value"] += 1
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(json, "loads", counting_loads)

    assert auth_parser.read_codex_auth()["auth_mode"] == "chatgpt"
    assert auth_parser.read_codex_auth()["auth_mode"] == "chatgpt"
    assert parse_count["value"] == 1

    auth_path.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk"}), encoding="utf-8")
    assert auth_parser.read_codex_auth()["auth_mode"] == "apikey"
    assert parse_count["value"] == 2


def test_codex_auth_cache_detects_same_size_write_with_same_timestamp(monkeypatch, tmp_path):
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", auth_path)
    auth_parser.clear_codex_auth_cache()
    auth_parser.write_codex_auth({"auth_mode": "apikey", "OPENAI_API_KEY": "synthetic-a"})
    stamp = auth_path.stat()
    assert auth_parser.read_codex_auth()["OPENAI_API_KEY"] == "synthetic-a"
    content = auth_path.read_text(encoding="utf-8")
    auth_path.write_text(content.replace("synthetic-a", "synthetic-b"), encoding="utf-8")
    os.utime(auth_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert auth_path.stat().st_size == stamp.st_size
    assert auth_parser.read_codex_auth()["OPENAI_API_KEY"] == "synthetic-b"


def test_codex_auth_cache_does_not_label_old_data_as_a_new_concurrent_write(monkeypatch, tmp_path):
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", auth_path)
    auth_parser.clear_codex_auth_cache()
    old = json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "synthetic-a"})
    new = old.replace("synthetic-a", "synthetic-b")
    auth_path.write_text(old, encoding="utf-8")
    original_loads = json.loads

    def replace_during_parse(content):
        auth_path.write_text(new, encoding="utf-8")
        return original_loads(content)

    monkeypatch.setattr(json, "loads", replace_during_parse)
    assert auth_parser.read_codex_auth()["OPENAI_API_KEY"] == "synthetic-a"
    assert auth_parser.read_codex_auth()["OPENAI_API_KEY"] == "synthetic-b"
