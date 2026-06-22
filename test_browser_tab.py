from types import SimpleNamespace

from ui.tabs.browser_tab import (
    BrowserTab,
    _browser_diagnosis_matches_filter,
    _browser_profiles_summary,
    _diagnosis_failure,
    _visible_profile_names,
)


def _profile(name: str):
    return SimpleNamespace(name=name)


def test_browser_diagnosis_filter_handles_missing_keys_as_issue():
    assert _browser_diagnosis_matches_filter({}, "issues") is True
    assert _browser_diagnosis_matches_filter({}, "launchable") is False
    assert _browser_diagnosis_matches_filter({}, "resettable") is False
    assert _browser_diagnosis_matches_filter({}, "all") is True


def test_browser_profile_summary_counts_cached_diagnostics():
    profiles = [_profile("ok"), _profile("busy"), _profile("bad")]
    diagnoses = {
        "ok": {
            "valid": True,
            "executable_found": True,
            "profile_path_exists": True,
            "browser_running": False,
            "can_full_reset": True,
        },
        "busy": {
            "valid": True,
            "executable_found": True,
            "profile_path_exists": True,
            "browser_running": True,
            "can_full_reset": False,
        },
        "bad": {
            "valid": False,
            "executable_found": False,
            "profile_path_exists": False,
            "browser_running": False,
            "can_full_reset": False,
        },
    }

    summary = _browser_profiles_summary(profiles, diagnoses, {"ok", "missing"})

    assert summary["total_count"] == 3
    assert summary["issues_count"] == 2
    assert summary["launchable_count"] == 2
    assert summary["resettable_count"] == 1
    assert summary["selected_count"] == 1


def test_visible_profile_names_reuses_filter_without_rediagnosing():
    profiles = [_profile("ok"), _profile("bad")]
    diagnoses = {
        "ok": {
            "valid": True,
            "executable_found": True,
            "profile_path_exists": True,
            "browser_running": False,
        },
        "bad": {
            "valid": False,
            "executable_found": True,
            "profile_path_exists": True,
            "browser_running": False,
        },
    }

    assert _visible_profile_names(profiles, diagnoses, "launchable") == ["ok"]
    assert _visible_profile_names(profiles, diagnoses, "issues") == ["bad"]


def test_diagnosis_failure_keeps_failed_profile_visible_as_issue():
    diagnosis = _diagnosis_failure(RuntimeError("boom"))

    assert diagnosis["valid"] is False
    assert "boom" in diagnosis["validation_error"]
    assert _browser_diagnosis_matches_filter(diagnosis, "issues") is True


def test_browser_tab_suspend_cancels_initial_refresh():
    tab = object.__new__(BrowserTab)
    tab._initial_refresh_after_id = "initial"
    tab._profile_render_after_id = None
    tab._deferred_refresh_pending = False
    tab._deferred_render_pending = False
    cancelled = []
    tab.after_cancel = lambda after_id: cancelled.append(after_id)

    BrowserTab._suspend_background_work(tab)

    assert cancelled == ["initial"]
    assert tab._initial_refresh_after_id is None
    assert tab._deferred_refresh_pending is True
