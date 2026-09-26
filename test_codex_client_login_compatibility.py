"""Optional real-client checks with synthetic credentials and isolated homes only."""

import base64
import json
import os
from pathlib import Path
import subprocess

import pytest

from core import account_transfer


def _jwt(claims):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return encode({"alg": "RS256", "typ": "JWT"}) + "." + encode(claims) + ".synthetic-signature"


def _auth():
    return {
        "auth_mode": "chatgpt",
        "last_refresh": "2026-09-21T00:00:00Z",
        "tokens": {
            "id_token": _jwt({"sub": "synthetic-user", "email": "test@example.test",
                              "https://api.openai.com/auth": {"chatgpt_account_id": "synthetic-account"}}),
            "access_token": _jwt({"sub": "synthetic-user", "exp": 4102444800}),
            "refresh_token": "synthetic-refresh-never-use-online",
            "account_id": "synthetic-account",
        },
    }


def _login_status(tmp_path, credentials):
    executable = os.environ.get("API_SWITCHER_TEST_CODEX_CLI")
    if not executable:
        pytest.skip("Set API_SWITCHER_TEST_CODEX_CLI for an isolated native client check")
    assert Path(executable).is_file()
    # Never inherit a live home/keyring selection or a project-level config.
    folder = tmp_path / "synthetic-codex"
    folder.mkdir()
    (folder / "config.toml").write_text('cli_auth_credentials_store = "file"\nmodel_provider = "openai"\n', encoding="utf-8")
    (folder / "auth.json").write_text(json.dumps(credentials), encoding="utf-8")
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(folder)
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"):
        environment.pop(name, None)
    return subprocess.run(
        [executable, "login", "status"], env=environment, cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.parametrize("camel_case", [False, True])
def test_transferred_auth_is_recognized_by_real_client(tmp_path, camel_case):
    credentials = _auth()
    if camel_case:
        aliases = {"id_token": "idToken", "access_token": "accessToken",
                   "refresh_token": "refreshToken", "account_id": "accountId"}
        credentials["tokens"] = {aliases[key]: value for key, value in credentials["tokens"].items()}
    normalized = account_transfer._clean_credentials("codex", credentials)
    result = _login_status(tmp_path, normalized)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "chatgpt" in (result.stderr + result.stdout).lower()


@pytest.mark.parametrize("refresh_time", [
    None, "2026-09-21T00:00:00Z", "2026-09-21T08:00:00+08:00",
    "2026-09-21T00:00:00.123456789Z", "2026-09-21t00:00:00z",
    "2026-09-21 00:00:00+00:00",
])
def test_transfer_preserves_client_readable_refresh_dates(tmp_path, refresh_time):
    credentials = _auth()
    credentials["last_refresh"] = refresh_time
    normalized = account_transfer._clean_credentials("codex", credentials)
    assert normalized.get("last_refresh") == refresh_time
    result = _login_status(tmp_path, normalized)
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize("field,value", [
    ("last_refresh", "20260921T000000Z"),
    ("last_refresh", "2026-W39-1T00:00:00Z"),
    ("last_refresh", "2026-09-21T00Z"),
    ("last_refresh", "2026-09-21T00:00:00+00:00:30"),
    ("id_token", _jwt({"email": []})),
    ("id_token", _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": 123}})),
    ("id_token", _jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": []}})),
    ("id_token", "synthetic." + base64.urlsafe_b64encode(
        b'{"email":"first@example.test","email":"second@example.test"}'
    ).decode().rstrip("=") + ".signature"),
    ("id_token", _jwt({"sub": "synthetic", "extra": float("nan")})),
    ("id_token", _jwt({"sub": "synthetic"}).rsplit(".", 1)[0] + "!!!!.signature"),
], ids=["compact-date", "week-date", "hour-only", "offset-seconds", "email-type", "account-type", "plan-type",
        "duplicate-claim", "non-json-number", "invalid-base64"])
def test_transfer_blocks_known_native_client_parse_failures(tmp_path, field, value):
    credentials = _auth()
    (credentials["tokens"] if field == "id_token" else credentials)[field] = value
    with pytest.raises(ValueError):
        account_transfer._clean_credentials("codex", credentials)
    result = _login_status(tmp_path, credentials)
    assert result.returncode != 0
    assert "Error logging in" in result.stderr or "Error" in result.stderr


@pytest.mark.parametrize("claims", [
    {"sub": "synthetic", "email": None, "https://api.openai.com/auth": None},
    {"sub": "synthetic", "custom": {"arbitrary": [1, 2]}},
    {"email": "synthetic@example.test", "https://api.openai.com/auth": {
        "chatgpt_account_id": "synthetic", "chatgpt_plan_type": "future-plan",
    }},
])
def test_optional_and_unknown_id_claims_remain_compatible(tmp_path, claims):
    credentials = _auth()
    credentials["tokens"]["id_token"] = _jwt(claims)
    normalized = account_transfer._clean_credentials("codex", credentials)
    assert normalized["tokens"]["id_token"] == credentials["tokens"]["id_token"]
    result = _login_status(tmp_path, normalized)
    assert result.returncode == 0, result.stderr + result.stdout
