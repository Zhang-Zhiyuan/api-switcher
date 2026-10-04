"""Synthetic-clock scheduler checks; no network, GUI, credentials or user I/O."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from core.subscription_refresh_schedule import SubscriptionRefreshSchedule, make_source_key


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def scheduler():
    clock = Clock()
    return SubscriptionRefreshSchedule(clock=clock), clock


def download(schedule, key="source", consumer="local", interval=600, revision="r1"):
    decision = schedule.begin(key, consumer, interval)
    assert decision.action == "fetch"
    version = schedule.finish_success(decision.token, revision)
    assert isinstance(version, int) and version > 0
    return version


def test_download_is_shared_but_consumers_acknowledge_independently(scheduler):
    schedule, _clock = scheduler
    version = download(schedule)
    for consumer in ("local", ("ssh", "server-A"), ("ssh", "server-B")):
        decision = schedule.begin("source", consumer, 600)
        assert (decision.action, decision.revision, decision.version) == ("cached", "r1", version)
        assert schedule.mark_applied("source", consumer, decision.revision, version=decision.version)
        assert schedule.begin("source", consumer, 600).action == "wait"


def test_short_period_is_not_blocked_by_another_consumers_long_period(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=3600)
    schedule.mark_applied("source", "local", "r1", version=version)
    schedule.mark_applied("source", ("ssh", "fast"), "r1", version=version)
    clock.advance(60)
    assert schedule.begin("source", "local", 3600).action == "wait"
    next_version = download(schedule, consumer=("ssh", "fast"), interval=60, revision="r2")
    assert next_version != version
    assert schedule.begin("source", "local", 3600).action == "cached"


def test_download_due_takes_priority_over_pending_apply(scheduler):
    schedule, clock = scheduler
    download(schedule, interval=60)
    clock.advance(60)
    assert schedule.begin("source", "local", 60).action == "fetch"


def test_failed_apply_can_retry_cached_revision_before_next_download(scheduler):
    schedule, clock = scheduler
    version = download(schedule)
    schedule.defer_apply("source", "local", "r1", version=version, retryable=True, interval_seconds=600)
    decision = schedule.begin("source", "local", 600)
    assert (decision.action, decision.retry_after, decision.next_due) == ("wait", 60, clock.now + 60)
    clock.advance(60)
    assert schedule.begin("source", "local", 600).action == "cached"


def test_download_backoff_is_exponential_capped_and_has_no_phantom_cache(scheduler):
    schedule, clock = scheduler
    for delay in (60, 120, 240, 480, 960, 1800, 1800):
        decision = schedule.begin("source", "local", 600)
        assert decision.action == "fetch"
        retry_at = schedule.finish_failure(decision.token)
        assert retry_at == clock.now + delay
        waiting = schedule.begin("source", "local", 600)
        assert waiting.action == "wait" and waiting.revision is None
        assert waiting.retry_after == delay and waiting.next_due == retry_at
        clock.advance(delay)


def test_success_resets_download_backoff(scheduler):
    schedule, clock = scheduler
    token = schedule.begin("source", "local", 60).token
    schedule.finish_failure(token)
    clock.advance(60)
    download(schedule, interval=60)
    clock.advance(60)
    token = schedule.begin("source", "local", 60).token
    assert schedule.finish_failure(token) == clock.now + 60


def test_one_bad_source_does_not_delay_another_subscription(scheduler):
    schedule, _clock = scheduler
    schedule.finish_failure(schedule.begin("bad", "local", 600).token)
    assert schedule.begin("bad", "local", 600).action == "wait"
    assert schedule.begin("good", "local", 600).action == "fetch"


def test_one_consumers_failure_does_not_reapply_anothers_success(scheduler):
    schedule, clock = scheduler
    version = download(schedule)
    schedule.mark_applied("source", "local", "r1", version=version)
    schedule.defer_apply("source", ("ssh", "bad"), "r1", version=version, retryable=True, interval_seconds=600)
    clock.advance(60)
    assert schedule.begin("source", ("ssh", "bad"), 600).action == "cached"
    assert schedule.begin("source", "local", 600).action == "wait"


def test_cache_remains_usable_by_new_consumer_during_download_backoff(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=600)
    schedule.mark_applied("source", "local", "r1", version=version)
    clock.advance(60)
    schedule.finish_failure(schedule.begin("source", "fast-consumer", 60).token)
    assert schedule.begin("source", "new-consumer", 60).action == "cached"
    assert schedule.begin("source", "local", 600).action == "wait"


def test_same_cache_fingerprint_does_not_repeat_apply_before_consumers_period(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=600)
    schedule.mark_applied("source", "local", "r1", version=version)
    clock.advance(60)
    assert download(schedule, consumer="fast", interval=60, revision="r1") == version
    assert schedule.begin("source", "local", 600).action == "wait"


def test_apply_backoff_is_per_consumer_and_capped(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=86400)
    for delay in (60, 120, 240, 480, 960, 1800, 1800):
        deadline = schedule.defer_apply("source", "local", "r1", version=version,
                                        retryable=True, interval_seconds=86400)
        assert deadline == clock.now + delay
        waiting = schedule.begin("source", "local", 86400)
        assert waiting.action == "wait" and waiting.retry_after == delay
        assert schedule.begin("source", ("ssh", "other"), 86400).action == "cached"
        clock.advance(delay)
        assert schedule.begin("source", "local", 86400).action == "cached"


def test_non_retryable_skip_waits_consumers_normal_period(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=600)
    deadline = schedule.defer_apply("source", "local", "r1", version=version,
                                    retryable=False, interval_seconds=600)
    assert deadline == clock.now + 600
    assert schedule.begin("source", "local", 600).retry_after == 600
    clock.advance(600)
    assert schedule.begin("source", "local", 600).action == "fetch"


def test_wait_deadline_is_earliest_download_or_apply_action(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=90)
    schedule.defer_apply("source", "local", "r1", version=version, retryable=True, interval_seconds=90)
    assert schedule.begin("source", "local", 90).next_due == clock.now + 60
    clock.advance(60)
    schedule.defer_apply("source", "local", "r1", version=version, retryable=True, interval_seconds=90)
    decision = schedule.begin("source", "local", 90)
    assert (decision.next_due, decision.retry_after) == (clock.now + 30, 30)
    clock.advance(30)
    assert schedule.begin("source", "local", 90).action == "fetch"


def test_new_revision_immediately_clears_apply_deferral(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=30)
    schedule.defer_apply("source", "local", "r1", version=version, retryable=False, interval_seconds=600)
    clock.advance(30)
    updated_version = download(schedule, interval=30, revision="r2")
    decision = schedule.begin("source", "local", 600)
    assert decision.action == "cached" and decision.version == updated_version
    assert not schedule.mark_applied("source", "local", "r1", version=version)
    assert schedule.defer_apply("source", "local", "r1", version=version,
                                retryable=True, interval_seconds=600) is None


def test_same_revision_preserves_apply_backoff_across_downloads(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=30)
    schedule.defer_apply("source", "local", "r1", version=version, retryable=True, interval_seconds=600)
    clock.advance(30)
    assert download(schedule, interval=30) == version
    assert schedule.begin("source", "local", 600).retry_after == 30


def test_mark_applied_clears_own_deferral(scheduler):
    schedule, _clock = scheduler
    version = download(schedule)
    schedule.defer_apply("source", "local", "r1", version=version, retryable=True, interval_seconds=600)
    assert schedule.mark_applied("source", "local", "r1", version=version)
    assert schedule.begin("source", "local", 600).retry_after == 600


def test_stale_reservations_are_rejected_after_cancel_or_reset(scheduler):
    schedule, _clock = scheduler
    old = schedule.begin("source", "local", 600).token
    assert schedule.cancel(old)
    current = schedule.begin("source", "local", 600).token
    assert current != old
    assert schedule.finish_success(old, "stale") is None
    assert schedule.finish_failure(old) is None
    assert not schedule.cancel(old)
    schedule.reset()
    fresh = schedule.begin("source", "local", 600).token
    assert fresh != current
    assert schedule.finish_success(current, "stale") is None
    assert schedule.finish_success(fresh, "fresh") is not None


def test_old_apply_cannot_acknowledge_same_named_revision_after_invalidate(scheduler):
    schedule, _clock = scheduler
    old_version = download(schedule)
    schedule.invalidate("source")
    new_version = download(schedule)
    assert new_version != old_version
    assert not schedule.mark_applied("source", "local", "r1", version=old_version)
    assert schedule.begin("source", "local", 600).action == "cached"


def test_all_busy_capacity_rejects_new_reservation_without_evicting_live_fetch():
    schedule = SubscriptionRefreshSchedule(clock=Clock(), max_entries=1)
    active = schedule.begin("A", "local", 60)
    blocked = schedule.begin("B", "local", 60)
    assert blocked.action == "busy" and blocked.reason == "capacity" and blocked.revision is None
    assert len(schedule) == 1
    assert schedule.begin("A", "ssh", 60).action == "busy"
    assert schedule.finish_success(active.token, "A-r1") is not None
    assert schedule.begin("B", "local", 60).action == "fetch"
    assert len(schedule) == 1


def test_lru_evicts_only_idle_sources():
    schedule = SubscriptionRefreshSchedule(clock=Clock(), max_entries=2)
    pinned = schedule.begin("pinned", "local", 60)
    download(schedule, key="idle")
    assert schedule.begin("next", "local", 60).action == "fetch"
    assert schedule.finish_success(pinned.token, "pinned-r1") is not None
    assert len(schedule) == 2


def test_consumer_acknowledgements_are_bounded():
    schedule = SubscriptionRefreshSchedule(clock=Clock(), max_consumers_per_source=2)
    version = download(schedule)
    for consumer in ("A", "B", "C"):
        schedule.mark_applied("source", consumer, "r1", version=version)
    assert len(schedule._sources["source"].consumers) == 2
    assert schedule.begin("source", "A", 600).action == "cached"
    assert schedule.begin("source", "C", 600).action == "wait"


def test_threads_share_one_download_reservation(scheduler):
    schedule, _clock = scheduler
    barrier = threading.Barrier(12)

    def begin(index):
        barrier.wait(timeout=3)
        return schedule.begin("source", ("ssh", str(index)), 60)

    with ThreadPoolExecutor(max_workers=12) as executor:
        decisions = list(executor.map(begin, range(12)))
    assert sum(decision.action == "fetch" for decision in decisions) == 1
    assert sum(decision.action == "busy" for decision in decisions) == 11


def test_source_key_is_secret_free_order_stable_and_changes_with_source_or_policy(scheduler):
    schedule, _clock = scheduler
    base = dict(profile_id="profile", url="https://synthetic.invalid/?token=synthetic-secret",
                source_path="source.yaml", source_revision="edit-1", privacy_policy={"strict": True, "direct": False})
    original = make_source_key(**base)
    assert len(original) == 64 and "synthetic-secret" not in original
    assert make_source_key(**{**base, "privacy_policy": {"direct": False, "strict": True}}) == original
    schedule.finish_failure(schedule.begin(original, "local", 600).token)
    for field, value in (("profile_id", "other"), ("url", "https://other.invalid"),
                         ("source_path", "other.yaml"), ("source_revision", "edit-2"),
                         ("privacy_policy", {"strict": False, "direct": False})):
        changed = make_source_key(**{**base, field: value})
        assert changed != original
        assert schedule.begin(changed, "local", 600).action == "fetch"


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), None, "bad", True])
def test_invalid_periods_do_not_create_schedule_entries(scheduler, interval):
    schedule, _clock = scheduler
    with pytest.raises(ValueError):
        schedule.begin("source", "local", interval)
    assert len(schedule) == 0


def test_success_requires_revision_and_uninitialized_cache_cannot_be_acknowledged(scheduler):
    schedule, _clock = scheduler
    pending = schedule.begin("source", "local", 600)
    with pytest.raises(ValueError):
        schedule.finish_success(pending.token, "")
    assert not schedule.mark_applied("source", "local", None, version=None)
    assert schedule.cancel(pending.token)


def test_next_delay_unknown_source_does_not_create_or_evict_state():
    schedule = SubscriptionRefreshSchedule(clock=Clock(), max_entries=1)
    assert schedule.next_delay("unknown", "local", 60) == 0
    assert len(schedule) == 0
    token = schedule.begin("busy", "local", 60).token
    assert schedule.next_delay("unknown", "local", 60) == 0
    assert len(schedule) == 1
    assert schedule.finish_success(token, "r1") is not None


def test_next_delay_does_not_reserve_or_update_source_and_consumer_lru(scheduler):
    schedule, clock = scheduler
    version = download(schedule, key="A", interval=60)
    schedule.mark_applied("A", "older", "r1", version=version)
    schedule.mark_applied("A", "newer", "r1", version=version)
    download(schedule, key="B", interval=60)
    before_sources = list(schedule._sources)
    before_consumers = list(schedule._sources["A"].consumers)
    serial = schedule._serial
    assert schedule.next_delay("A", "older", 60) == 60
    clock.advance(70)
    assert schedule.next_delay("A", "older", 60) == 0
    assert schedule._sources["A"].reservation is None
    assert list(schedule._sources) == before_sources
    assert list(schedule._sources["A"].consumers) == before_consumers
    assert schedule._serial == serial


def test_next_delay_respects_busy_without_changing_reservation(scheduler):
    schedule, _clock = scheduler
    decision = schedule.begin("source", "local", 600)
    assert schedule.next_delay("source", "local", 600) == 1
    assert schedule._sources["source"].reservation == decision.token
    assert schedule.finish_failure(decision.token) is not None
    assert schedule.next_delay("source", "local", 600) == 60


def test_next_delay_shares_earliest_download_and_apply_deadline_formula(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=90)
    assert schedule.next_delay("source", "local", 90) == 0
    schedule.defer_apply("source", "local", "r1", version=version, retryable=True, interval_seconds=90)
    assert schedule.next_delay("source", "local", 90) == 60
    clock.advance(60)
    assert schedule.next_delay("source", "local", 90) == 0
    schedule.defer_apply("source", "local", "r1", version=version, retryable=True, interval_seconds=90)
    assert schedule.next_delay("source", "local", 90) == 30
    assert schedule.next_delay("source", "local", 90) == schedule.begin("source", "local", 90).retry_after


def test_next_delay_uses_each_callers_period_and_successful_apply_state(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=3600)
    schedule.mark_applied("source", "slow", "r1", version=version)
    schedule.mark_applied("source", "fast", "r1", version=version)
    clock.advance(30)
    assert schedule.next_delay("source", "slow", 3600) == 3570
    assert schedule.next_delay("source", "fast", 60) == 30
    assert schedule.next_delay("source", "not-applied", 3600) == 0


def test_unchanged_download_allows_health_check_after_consumers_period(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=60)
    schedule.mark_applied("source", "local", "r1", version=version)
    clock.advance(60)
    assert download(schedule, interval=60, revision="r1") == version
    assert schedule.next_delay("source", "local", 60) == 0
    assert schedule.begin("source", "local", 60).action == "cached"
    schedule.mark_applied("source", "local", "r1", version=version)
    assert schedule.next_delay("source", "local", 60) == 60


def test_fast_downloads_do_not_starve_slower_consumers_health_verification(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=60)
    schedule.mark_applied("source", "local", "r1", version=version)
    schedule.mark_applied("source", ("ssh", "slow"), "r1", version=version)
    for elapsed in (60, 120, 180, 240, 300):
        clock.advance(60)
        assert download(schedule, interval=60, revision="r1") == version
        schedule.mark_applied("source", "local", "r1", version=version)
        assert schedule.next_delay("source", ("ssh", "slow"), 300) == 300 - elapsed
        assert schedule.begin("source", ("ssh", "slow"), 300).action == ("cached" if elapsed == 300 else "wait")
    schedule.mark_applied("source", ("ssh", "slow"), "r1", version=version)
    assert schedule.next_delay("source", ("ssh", "slow"), 300) == 300


def test_health_check_deadline_survives_partial_source_failure_retry(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=600)
    schedule.mark_applied("source", "local", "r1", version=version)
    schedule.finish_failure(schedule.begin("unrelated-bad", "local", 600).token)
    clock.advance(60)
    assert schedule.begin("source", "local", 600).action == "wait"
    assert schedule.next_delay("source", "local", 600) == 540


def test_proven_origin_survives_failed_apply_retry_but_success_clears_it(scheduler):
    schedule, clock = scheduler
    version = download(schedule)
    origin = "a" * 64
    assert schedule.remember_origin("source", "local", "r1", version=version, current_key=origin)
    for retryable in (True, False):
        schedule.defer_apply("source", "local", "r1", version=version,
                             retryable=retryable, interval_seconds=600)
        assert schedule.proven_origin("source", "local", "r1", version=version) == origin
    clock.advance(600)
    assert download(schedule, revision="r1") == version
    assert schedule.proven_origin("source", "local", "r1", version=version) == origin
    assert schedule.mark_applied("source", "local", "r1", version=version)
    assert schedule.proven_origin("source", "local", "r1", version=version) is None


def test_proven_origin_isolated_by_source_consumer_and_cache_generation(scheduler):
    schedule, clock = scheduler
    version = download(schedule, interval=60)
    assert schedule.remember_origin("source", ("ssh", "A"), "r1", version=version, current_key="b" * 64)
    for key, consumer, revision, checked_version in (
        ("source", ("ssh", "B"), "r1", version),
        ("source", "local", "r1", version),
        ("other-source", ("ssh", "A"), "r1", version),
        ("source", ("ssh", "A"), "r2", version),
        ("source", ("ssh", "A"), "r1", version + 1),
    ):
        assert schedule.proven_origin(key, consumer, revision, version=checked_version) is None
    clock.advance(60)
    updated = download(schedule, interval=60, revision="r2")
    assert schedule.proven_origin("source", ("ssh", "A"), "r2", version=updated) is None
    assert not schedule.remember_origin("source", ("ssh", "A"), "r1", version=version, current_key="c" * 64)


@pytest.mark.parametrize("invalid", [None, "", "original", "a" * 63, "a" * 65, "z" * 64, "秘密" * 32])
def test_proven_origin_accepts_only_exact_sha256_fingerprints(scheduler, invalid):
    schedule, _clock = scheduler
    version = download(schedule)
    assert not schedule.remember_origin("source", "local", "r1", version=version, current_key=invalid)
    assert schedule.proven_origin("source", "local", "r1", version=version) is None
    assert not schedule._sources["source"].consumers


def test_origin_proof_read_is_non_mutating_and_bounded_by_consumer_lru():
    schedule = SubscriptionRefreshSchedule(clock=Clock(), max_consumers_per_source=2)
    version = download(schedule)
    schedule.remember_origin("source", "old", "r1", version=version, current_key="a" * 64)
    schedule.remember_origin("source", "new", "r1", version=version, current_key="b" * 64)
    assert schedule.proven_origin("source", "old", "r1", version=version) == "a" * 64
    schedule.remember_origin("source", "newest", "r1", version=version, current_key="c" * 64)
    assert schedule.proven_origin("source", "old", "r1", version=version) is None
    assert len(schedule._sources["source"].consumers) == 2


@pytest.mark.parametrize("reset", ["clear", "invalidate", "evict"])
def test_origin_proof_cannot_survive_reset_invalidation_or_eviction(reset):
    schedule = SubscriptionRefreshSchedule(clock=Clock(), max_entries=1)
    version = download(schedule)
    schedule.remember_origin("source", "local", "r1", version=version, current_key="d" * 64)
    if reset == "clear":
        schedule.clear()
    elif reset == "invalidate":
        schedule.invalidate("source")
    else:
        download(schedule, key="other-source")
    updated = download(schedule)
    assert updated != version
    assert schedule.proven_origin("source", "local", "r1", version=updated) is None
