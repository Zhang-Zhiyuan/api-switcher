"""Proxy workers must never use Tcl as an asynchronous fallback dispatcher."""
import threading

import pytest

from ui.tabs.local_proxy_tab import LocalProxyTab
from ui.tabs.ssh_tab import SSHTab


@pytest.fixture(params=[LocalProxyTab, SSHTab], ids=["win11", "ssh"])
def proxy_tab(request):
    tab = object.__new__(request.param)
    tab._destroyed = False
    tab._ui_dispatch = None
    return tab


@pytest.mark.parametrize("response", [True, False, None, "raise"])
def test_proxy_dispatch_reports_real_acceptance_without_fallback(proxy_tab, response):
    queued, tk_calls = [], []
    def dispatch(callback):
        if response == "raise":
            raise RuntimeError("synthetic dispatch unavailable")
        if response is not False:
            queued.append(callback)
        return response
    proxy_tab._ui_dispatch = dispatch
    proxy_tab.after = lambda *_args: tk_calls.append("after")
    assert proxy_tab._run_on_ui_thread(lambda: None) is (response in (True, None))
    assert len(queued) == int(response in (True, None)) and not tk_calls


def test_worker_without_dispatch_never_touches_tk(proxy_tab):
    calls, results = [], []
    proxy_tab.after = lambda *_args: calls.append("after")
    proxy_tab.winfo_toplevel = lambda: calls.append("winfo_toplevel")
    worker = threading.Thread(target=lambda: results.append(proxy_tab._run_on_ui_thread(lambda: None)))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and results == [False] and not calls


def test_main_thread_only_can_use_after_when_no_dispatch(proxy_tab):
    calls = []
    def callback():
        pass
    proxy_tab.after = lambda delay, cb: calls.append((delay, cb))
    assert proxy_tab._run_on_ui_thread(callback) is True
    assert calls == [(0, callback)]


def test_closed_proxy_tab_rejects_callbacks_without_touching_dispatcher(proxy_tab):
    calls = []
    proxy_tab._destroyed = True
    proxy_tab._ui_dispatch = lambda *_args: calls.append("dispatch")
    proxy_tab.after = lambda *_args: calls.append("after")
    assert proxy_tab._run_on_ui_thread(lambda: None) is False
    assert not calls
