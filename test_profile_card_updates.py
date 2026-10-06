"""Native in-place card updates; every action uses synthetic callbacks only."""
import customtkinter as ctk
import pytest

from ui.theme import COLORS
from ui.widgets import profile_card


@pytest.fixture
def surface(tk_root):
    window = ctk.CTkToplevel(tk_root)
    window.geometry("620x500")
    try:
        yield window
    finally:
        window.destroy()
        tk_root.update()


def descendants(widget):
    return [child for direct in widget.winfo_children() for child in [direct, *descendants(direct)]]


def test_text_only_refresh_preserves_native_widgets_and_busy_test(surface):
    calls = []
    options = dict(name="synthetic API", info_lines=["model A", "endpoint invalid"], on_test=calls.append)
    card = profile_card.ProfileCard(surface, **options)
    card.pack(fill="x")
    surface.update()
    card.test_button.configure(text="测试中", state="disabled")
    before = descendants(card)
    labels = card._info_labels[:]
    for i in range(20):
        card.update_content(**{**options, "info_lines": [f"model {i}", "endpoint invalid"]})
    surface.update()
    assert descendants(card) == before
    assert card._info_labels == labels and labels[0].cget("text") == "model 19"
    assert card.test_button.cget("text") == "测试中" and card.test_button.cget("state") == "disabled"
    card.test_button.invoke()
    assert not calls


def test_current_name_callbacks_and_active_badge_are_reused(surface):
    calls = []
    options = dict(name="old", info_lines=["synthetic"], on_switch=lambda n: calls.append(("old", n)))
    card = profile_card.ProfileCard(surface, **options)
    card.pack(fill="x")
    command = card._action_buttons["switch"].cget("command")
    latest = {**options, "name": "new", "on_switch": lambda n: calls.append(("new", n))}
    card.update_content(**latest)
    command()
    assert calls == [("new", "new")]
    card.update_content(**latest, is_active=True, active_label="active A")
    badge = card._active_tag
    assert "switch" not in card._action_buttons
    assert card.cget("fg_color") == COLORS["surface_alt"]
    command()
    assert len(calls) == 1
    for _ in range(10):
        card.update_content(**latest)
        assert not badge.winfo_manager()
        assert card.cget("fg_color") == COLORS["surface"]
        card.update_content(**latest, is_active=True, active_label="active B")
        assert badge is card._active_tag and badge.cget("text") == "active B"
    assert card._indicator.cget("text") == "●"


def test_invalid_account_revokes_switch_export_but_preserves_delete(surface):
    calls = []
    options = dict(name="synthetic account", info_lines=["valid"],
                   on_switch=lambda n: calls.append(("switch", n)),
                   on_export=lambda n: calls.append(("export", n)),
                   on_delete=lambda n: calls.append(("delete", n)))
    card = profile_card.ProfileCard(surface, **options)
    card.pack(fill="x")
    commands = [card._action_buttons[key].cget("command") for key in ("switch", "export")]
    card.update_content(**{**options, "info_lines": ["invalid"], "on_switch": None, "on_export": None},
                        border_color=COLORS["danger"])
    assert set(card._action_buttons) == {"delete"}
    for command in commands:
        command()
    assert not calls
    card._action_buttons["delete"].invoke()
    assert calls == [("delete", "synthetic account")]
    card.update_content(**options)
    assert set(card._action_buttons) == {"switch", "export", "delete"}
    card._action_buttons["export"].invoke()
    assert calls[-1] == ("export", "synthetic account")


def test_info_line_pool_does_not_accumulate_wrapping_callbacks(surface):
    card = profile_card.ProfileCard(surface, "synthetic", ["one", "two", "three"])
    card.pack(fill="x")
    surface.update()
    labels = card._info_labels[:]
    bindings = card._info_frame.bind("<Configure>")
    for _ in range(15):
        for lines in (["new"], [], ["1", "2", "3"]):
            card.update_content("synthetic", lines)
            assert card._info_frame.pack_slaves() == labels[:len(lines)]
            assert [label.cget("text") for label in labels] == lines + [""] * (3 - len(lines))
    assert card._info_labels == labels
    assert card._info_frame.bind("<Configure>") == bindings


def test_failed_action_rebuild_retries_even_when_reverting_to_original(surface, monkeypatch):
    options = dict(name="synthetic", info_lines=[], on_test=lambda _: None)
    card = profile_card.ProfileCard(surface, **options)
    card.pack(fill="x")
    original = ctk.CTkButton
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("synthetic button construction failure")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ctk, "CTkButton", fail_second)
        with pytest.raises(RuntimeError, match="synthetic button"):
            card.update_content(**options, on_switch=lambda _: None)
    assert card._actions_signature is None
    partial = card._btn_frame
    card.update_content(**options)
    assert not partial.winfo_exists()
    assert set(card._action_buttons) == {"test"}
    assert card._actions_signature is not None


@pytest.mark.parametrize("scaling", [1.0, 1.5, 2.5])
def test_updated_actions_stay_inside_card_at_different_scaling(surface, scaling):
    ctk.set_widget_scaling(scaling)
    try:
        options = dict(name="synthetic long title 测试长标题 " * 3, info_lines=["synthetic information"],
                       on_switch=lambda _: None, on_test=lambda _: None, on_edit=lambda _: None,
                       on_clone=lambda _: None, on_export=lambda _: None, on_delete=lambda _: None)
        # The real tab uses a scrollable surface: a high-DPI card must be
        # allowed to exceed the viewport vertically without clipping its row.
        scroll = ctk.CTkScrollableFrame(surface)
        scroll.pack(fill="both", expand=True)
        card = profile_card.ProfileCard(scroll, **options)
        card.pack(fill="x", padx=8, pady=8)
        for active in (False, True, False):
            card.update_content(**options, is_active=active)
            surface.update()
            left, right = card.winfo_rootx(), card.winfo_rootx() + card.winfo_width()
            for button in card._action_buttons.values():
                assert button.winfo_width() > 1
                assert left <= button.winfo_rootx() < right
                assert button.winfo_rootx() + button.winfo_width() <= right
    finally:
        ctk.set_widget_scaling(1.0)
