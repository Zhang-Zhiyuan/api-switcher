"""Token metadata comparisons never refresh credentials or contact a service."""

import base64
import copy
import json

import pytest

from core import auth_parser


def _auth(refreshed=None, *, issued=None, expires=None):
    claims = {key: value for key, value in (("iat", issued), ("exp", expires)) if value is not None}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"last_refresh": refreshed, "tokens": {"access_token": "test." + payload + ".signature"}}


def test_refresh_time_outranks_different_token_lifetimes():
    fresh = _auth("2026-09-21T10:00:00Z", expires=2000000000)
    old = _auth("2026-09-20T10:00:00Z", expires=2100000000)
    assert auth_parser.codex_auth_is_newer(fresh, old)
    assert not auth_parser.codex_auth_is_newer(old, fresh)


@pytest.mark.parametrize("field", ["issued", "expires"])
def test_token_metadata_can_compare_snapshots_without_last_refresh(field):
    fresh, old = _auth(**{field: 2000000100}), _auth(**{field: 2000000000})
    assert auth_parser.codex_auth_is_newer(fresh, old)
    assert not auth_parser.codex_auth_is_newer(old, fresh)


@pytest.mark.parametrize("missing", [None, "", "invalid", True, float("nan"), float("inf")])
def test_unknown_timestamp_never_proves_snapshot_is_older(missing):
    one, two = _auth("2026-09-21T10:00:00Z"), _auth(missing)
    assert not auth_parser.codex_auth_is_newer(one, two)
    assert not auth_parser.codex_auth_is_newer(two, one)


def test_equivalent_timezone_offsets_are_equal():
    one, two = _auth("2026-09-21T10:00:00+08:00"), _auth("2026-09-21T02:00:00Z")
    assert not auth_parser.codex_auth_is_newer(one, two)
    assert not auth_parser.codex_auth_is_newer(two, one)


def test_alias_normalization_preserves_source_and_whole_token_bundle():
    source = {"last_refresh": "2026-09-21T00:00:00Z", "tokens": {
        "idToken": "synthetic-id", "accessToken": "synthetic-access",
        "refreshToken": "synthetic-refresh", "accountId": "synthetic-account",
    }}
    before = copy.deepcopy(source)
    normalized = auth_parser.normalize_codex_token_fields(source)
    assert source == before
    assert normalized["tokens"] == {
        "id_token": "synthetic-id", "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh", "account_id": "synthetic-account",
    }
    assert normalized["last_refresh"] == source["last_refresh"]


@pytest.mark.parametrize("field,alias", [("id_token", "idToken"), ("access_token", "accessToken"),
                                       ("refresh_token", "refreshToken"), ("account_id", "accountId")])
def test_conflicting_aliases_are_rejected_without_echoing_credentials(field, alias):
    source = {"tokens": {field: "synthetic-secret-left", alias: "synthetic-secret-right"}}
    with pytest.raises(ValueError, match="冲突") as exc:
        auth_parser.normalize_codex_token_fields(source)
    assert "synthetic-secret" not in str(exc.value)


@pytest.mark.parametrize("token", [None, {}, [], 3, "opaque", "header.!.sig", "header.bnVsbA.sig"])
def test_malformed_claims_are_unknown_not_a_validated_login(token):
    assert auth_parser.codex_token_claims(token) == {}


@pytest.mark.parametrize("payload,junk", [
    (b'{"email":"first","email":"second"}', ""),
    (b'{"sub":"synthetic","extra":NaN}', ""),
    (b'{"sub":"synthetic","extra":Infinity}', ""),
    (b'{"sub":"synthetic"}', "!!!!"),
])
def test_invalid_jwt_json_and_base64_are_not_silently_repaired(payload, junk):
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=") + junk
    assert auth_parser.codex_token_claims("synthetic." + encoded + ".signature") == {}
