from __future__ import annotations

from ui.tabs.tab_visibility import is_active_tab


class _TabView:
    def __init__(self, current: str):
        self._current = current

    def get(self) -> str:
        return self._current


class _Widget:
    def __init__(self, master=None, label: str = "", top=None):
        self.master = master
        self._api_switcher_tab_label = label
        self._top = top or getattr(master, "_top", self)

    def winfo_toplevel(self):
        return self._top


def test_is_active_tab_walks_parent_chain_for_nested_widgets():
    top = _Widget(label="")
    top._tabview = _TabView("Win11 代理")
    tab = _Widget(master=top, label="Win11 代理", top=top)
    child = _Widget(master=tab, top=top)

    assert is_active_tab(child) is True

    top._tabview = _TabView("SSH 服务器")
    assert is_active_tab(child) is False


def test_is_active_tab_defaults_true_without_owner_label():
    top = _Widget(label="")
    top._tabview = _TabView("Win11 代理")
    child = _Widget(master=top, top=top)

    assert is_active_tab(child) is True
