"""Real Tk newline/index contract for log retention, without real log data."""
import customtkinter as ctk
import pytest

from test_log_viewer_retention import entry, make_view


@pytest.mark.parametrize("wrap", ["none", "word"])
def test_native_tracebacks_trim_at_record_boundary_and_keep_tags(tk_root, wrap):
    window = ctk.CTkToplevel(tk_root)
    widget = ctk.CTkTextbox(window, width=280, wrap=wrap)
    widget.pack(fill="both", expand=True)
    tab = make_view(widget)
    try:
        for index in range(20):
            tab._append_log_entries([entry(f"record-{index} 🌐\n中文详情 {'wide ' * 30}\n", "ERROR")])
            tk_root.update()
            expected = "".join(item["message"] + "\n" for item in tab._log_entries[-3:])
            assert widget.get("1.0", "end-1c") == expected
            assert len(tab._rendered_entry_lines) == (3 if index >= 2 else index + 1)
        ranges = widget._textbox.tag_ranges("ERROR")
        assert str(ranges[0]) == "1.0"
        assert str(ranges[-1]) == widget.index("end-1c")
        tab._render_log_entries()
        assert widget.get("1.0", "end-1c") == expected
    finally:
        window.destroy()
        tk_root.update()
