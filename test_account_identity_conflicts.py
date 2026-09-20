"""Account identity display labels must not collapse distinct workspaces."""
import base64
import json

import pytest

from core import profile_manager


def _jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "synthetic." + encoded + ".signature"


@pytest.mark.parametrize("field", ["account_id", "accountUuid", "userId", "user_id", "sub", "organizationUuid"])
def test_shared_email_never_overrides_explicit_identity_conflict(field):
    saved = {"email": "same@example.test", "account": {field: "original"}}
    current = {"email": "same@example.test", "account": {field: "different"}}
    assert profile_manager._account_snapshot_identity_conflict(saved, current)
    assert not profile_manager._account_snapshots_match(saved, current)


def test_nested_jwt_workspace_conflict_is_detected():
    def auth(workspace):
        return {"tokens": {"id_token": _jwt({
            "email": "same@example.test", "sub": "same-user",
            "https://api.openai.com/auth": {"chatgpt_account_id": workspace},
        })}}

    assert profile_manager._account_snapshot_identity_conflict(auth("first"), auth("second"))
    assert not profile_manager._account_snapshots_match(auth("first"), auth("second"))


def test_same_stable_account_id_allows_email_change_and_token_rotation():
    saved = {"email": "old@example.test", "tokens": {"account_id": "same-account", "access_token": "old"}}
    current = {"email": "new@example.test", "tokens": {"account_id": "same-account", "access_token": "new"}}
    assert not profile_manager._account_snapshot_identity_conflict(saved, current)
    assert profile_manager._account_snapshots_match(saved, current)


def test_matching_subject_does_not_hide_conflicting_user_ids():
    saved = {"email": "same@example.test", "sub": "same-subject", "userId": "user-a"}
    current = {"email": "same@example.test", "sub": "same-subject", "userId": "user-b"}
    assert not profile_manager._account_snapshots_match(saved, current)


def test_missing_new_identity_fields_is_compatible_with_existing_email_snapshot():
    saved = {"email": "same@example.test", "access_token": "old"}
    current = {"email": "same@example.test", "account_id": "added-id", "access_token": "new"}
    assert profile_manager._account_snapshots_match(saved, current)


@pytest.mark.parametrize("emails", [False, True])
def test_organization_membership_alone_does_not_prove_same_account(emails):
    saved = {"oauthAccount": {"organizationUuid": "shared-org"}, "accessToken": "first-user-token"}
    current = {"oauthAccount": {"organizationUuid": "shared-org"}, "accessToken": "other-user-token"}
    if emails:
        saved["email"] = "first@example.test"
        current["email"] = "other@example.test"
    assert not profile_manager._account_snapshot_identity_conflict(saved, current)
    assert not profile_manager._account_snapshots_match(saved, current)


def test_namespaced_stable_account_claim_survives_email_and_token_rotation():
    def auth(email, token):
        return {"tokens": {"access_token": token, "id_token": _jwt({
            "email": email,
            "https://api.openai.com/auth": {"chatgpt_account_id": "same-account"},
        })}}

    assert profile_manager._account_snapshots_match(
        auth("old@example.test", "old-token"), auth("new@example.test", "new-token"),
    )
