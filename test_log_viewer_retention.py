"""Bounded log rendering must discard complete entries, including tracebacks."""
from collections import deque
from types import SimpleNamespace

import pytest

from ui.tabs.log_viewer_tab import LOG_LEVELS, LogViewerTab


class TextModel:
    def __init__(self):
        self.content = ""

    def configure(self, **_kwargs):
        pass

    def insert(self, _index, text):
        self.content += text

    def index(self, _index):
        return f"{self.content.count(chr(10)) + 1}.0"

    def delete(self, _start, end):
        if end == "end":
            self.content = ""
        else:
            self.content = "".join(self.content.splitlines(keepends=True)[int(end.split('.')[0]) - 1:])

    def tag_add(self, *_args):
        pass

    def see(self, *_args):
        pass


def entry(message, level="INFO"):
    return {"level": level, "levelno": {"INFO": 20, "ERROR": 40}[level], "message": message}


def make_view(text=None):
    tab = object.__new__(LogViewerTab)
    tab.MAX_STORED_ENTRIES = 9
    tab.MAX_RENDERED_LINES = 3
    tab._log_text = text if text is not None else TextModel()
    tab._log_entries = []
    tab._log_counts = dict.fromkeys(LOG_LEVELS, 0)
    tab._filtered_entry_count = 0
    tab._visible_line_count = 0
    tab._rendered_entry_lines = deque()
    tab._filter_level = "DEBUG"
    tab._auto_scroll = False
    tab._stats_label = SimpleNamespace(configure=lambda **_kwargs: None)
    tab._render_status_label = SimpleNamespace(configure=lambda **_kwargs: None)
    return tab


@pytest.mark.parametrize("message", ["one\ntwo\nthree", "one\n\n", "诊断🌐\n异常详情"])
def test_live_append_trims_whole_multiline_record(message):
    tab = make_view()
    tab._append_log_entries([entry(message), entry("second"), entry("third")])
    tab._append_log_entries([entry("fourth")])
    assert tab._log_text.content == "second\nthird\nfourth\n"
    assert len(tab._log_entries) == 4  # Rendering must not truncate export data.
    assert tab._filtered_entry_count == tab._log_counts["INFO"] == 4


def test_repeated_multiline_batches_remain_bounded_and_match_full_redraw():
    tab = make_view()
    for index in range(30):
        tab._append_log_entries([entry(f"record-{index}\ntraceback-{index}\n")])
        expected = "".join(item["message"] + "\n" for item in tab._log_entries[-3:])
        assert tab._log_text.content == expected
    assert tab._visible_line_count == 3
    assert len(tab._log_entries) == 9
    before = tab._log_text.content
    tab._render_log_entries()
    assert tab._log_text.content == before


def test_filter_redraw_rebuilds_entry_boundaries_before_next_trim():
    tab = make_view()
    tab._append_log_entries([entry("info\nextra"), entry("error\ntraceback", "ERROR"), entry("tail")])
    tab._filter_level = "ERROR"
    tab._render_log_entries()
    for index in range(3):
        tab._append_log_entries([entry(f"new-{index}", "ERROR")])
    assert tab._log_text.content == "new-0\nnew-1\nnew-2\n"
    assert tab._filtered_entry_count == 4


def test_clear_discards_old_boundaries_without_losing_future_entries(monkeypatch):
    from ui.tabs import log_viewer_tab
    tab = make_view()
    tab._append_log_entries([entry("old\ntraceback")])
    monkeypatch.setattr(log_viewer_tab, "log_manager", SimpleNamespace(clear_history=lambda: None))
    monkeypatch.setattr(log_viewer_tab, "show_toast", lambda *_args, **_kwargs: None)
    tab.winfo_toplevel = lambda: None
    tab._clear_logs()
    for index in range(4):
        tab._append_log_entries([entry(f"new-{index}")])
    assert tab._log_text.content == "new-1\nnew-2\nnew-3\n"
