"""Headless checks for draft-only node-choice shortcuts."""

from types import SimpleNamespace

import pytest

from ui.dialogs.route_selection_dialogs import (
    AUTO_MODE, FIXED_MODE, MODE_LABELS, POOL_MODE, RouteNodeDialog,
)


class ChoiceHarness:
    _set_mode = RouteNodeDialog._set_mode
    _select_mode_label = RouteNodeDialog._select_mode_label
    _prioritize_pool = RouteNodeDialog._prioritize_pool

    def __init__(self, *, mode=FIXED_MODE, key="primary", pool=(), pool_enabled=True):
        self._mode = mode
        self._selected_key = key
        self._selected_keys = list(pool)
        self._pool_enabled = pool_enabled
        self._pool_was_opened = mode == POOL_MODE
        self._modes = SimpleNamespace(set=lambda label: self.labels.append(label))
        self.labels, self.renders, self.filters = [], [], []
        self._filter = lambda: self.filters.append(True)
        self._render_pool = self.renders.append


@pytest.mark.parametrize("mode", [FIXED_MODE, POOL_MODE, AUTO_MODE])
def test_short_display_labels_select_original_internal_modes(mode):
    dialog = ChoiceHarness()
    dialog._select_mode_label(MODE_LABELS[mode])
    assert dialog._mode == mode
    assert dialog.labels == [MODE_LABELS[mode]]
    assert len(dialog.filters) == 1


def test_fixed_to_pool_keeps_only_original_primary_even_when_missing_from_cache():
    dialog = ChoiceHarness(key="missing-primary")
    dialog._set_mode(POOL_MODE)
    assert dialog._selected_keys == ["missing-primary"]
    assert dialog._selected_key == "missing-primary"
    # Validation remains the commit handler's responsibility; the shortcut
    # must neither silently choose a replacement nor omit the missing key.


@pytest.mark.parametrize("pool", [[], ["backup", "primary"]])
def test_reentering_pool_never_reseeds_or_reorders_user_edits(pool):
    dialog = ChoiceHarness()
    dialog._set_mode(POOL_MODE)
    dialog._selected_keys = list(pool)
    dialog._set_mode(FIXED_MODE)
    dialog._set_mode(POOL_MODE)
    assert dialog._selected_keys == pool


def test_existing_pool_is_preserved_when_first_entered_from_fixed_mode():
    dialog = ChoiceHarness(pool=["backup", "primary"])
    dialog._set_mode(POOL_MODE)
    assert dialog._selected_keys == ["backup", "primary"]


def test_automatic_to_pool_does_not_infer_candidate_from_dormant_pin():
    dialog = ChoiceHarness(mode=AUTO_MODE)
    dialog._set_mode(POOL_MODE)
    assert dialog._selected_keys == []
    assert dialog._selected_key == "primary"


@pytest.mark.parametrize("label", ["invalid", MODE_LABELS[POOL_MODE]])
def test_invalid_or_unavailable_choice_does_not_modify_the_draft(label):
    dialog = ChoiceHarness(pool_enabled=False)
    dialog._select_mode_label(label)
    assert dialog._mode == FIXED_MODE and not dialog._selected_keys
    assert not dialog.filters and not dialog.labels and not dialog._pool_was_opened


def test_make_primary_preserves_every_candidate_including_missing_entries():
    dialog = ChoiceHarness(mode=POOL_MODE, pool=["old-primary", "missing-node", "new-primary", "backup"])
    dialog._pool_list = SimpleNamespace(curselection=lambda: (2,))
    dialog._prioritize_pool()
    assert dialog._selected_keys == ["new-primary", "old-primary", "missing-node", "backup"]
    assert dialog.renders == [0]


@pytest.mark.parametrize("mode,index", [(FIXED_MODE, 1), (POOL_MODE, None), (POOL_MODE, 0)])
def test_primary_shortcut_is_noop_without_an_eligible_selection(mode, index):
    dialog = ChoiceHarness(mode=mode, pool=["primary", "backup"])
    dialog._pool_list = SimpleNamespace(curselection=lambda: () if index is None else (index,))
    dialog._prioritize_pool()
    assert dialog._selected_keys == ["primary", "backup"]
    assert not dialog.renders
