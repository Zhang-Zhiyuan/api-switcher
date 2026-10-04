"""Node preparation is identical on Windows and SSH, with no live operations."""
from types import SimpleNamespace

import pytest

from ui.tabs import local_proxy_tab, ssh_tab


class Text:
    def __init__(self):
        self.value = "original draft"

    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, value):
        self.value = value


@pytest.mark.parametrize("module, prefix, method", [
    (local_proxy_tab, "", local_proxy_tab.LocalProxyTab._use_selected_subscription_node),
    (ssh_tab, "_proxy", ssh_tab.SSHTab._use_selected_proxy_subscription_node),
])
@pytest.mark.parametrize("persist, error", [(True, False), (False, False), (True, True)])
def test_prepare_node_never_applies_routing(module, prefix, method, persist, error, monkeypatch):
    item = SimpleNamespace(node={"name": "synthetic", "type": "http", "server": "example.test", "port": 8000})
    calls, messages, toasts = [], [], []
    editor = Text()
    tab = SimpleNamespace(winfo_toplevel=lambda: None)
    setattr(tab, prefix + "_subscription_picker", SimpleNamespace(selected_item=lambda: item))
    setattr(tab, prefix + "_node_text", editor)
    setattr(tab, "_current_proxy_subscription_profile_id" if prefix else "_current_subscription_profile_id", lambda: "profile-a")
    setattr(tab, "_set_proxy_selected_summary" if prefix else "_set_selected_summary", lambda *args: None)
    setattr(tab, "_set_proxy_status" if prefix else "_set_status", lambda message, *_args: messages.append(message))
    tab._apply_saved_routing = lambda *_args: pytest.fail("preparing a node must not apply website routing")
    monkeypatch.setattr(module, "show_toast", lambda _master, message, **_kwargs: toasts.append(message))
    monkeypatch.setattr(module.remote_proxy, "format_proxy_node", lambda _node: "synthetic YAML")
    monkeypatch.setattr(module.remote_proxy, "describe_proxy_node", lambda _node: "synthetic")

    def save(node, *, profile_id):
        calls.append((node, profile_id))
        if error:
            raise ValueError("synthetic cache failure")

    monkeypatch.setattr(module.remote_proxy, "set_proxy_subscription_selected_node", save)
    if module is local_proxy_tab:
        monkeypatch.setattr(module.local_proxy, "local_proxy_service_bindings_for_profile", lambda _id: ["openai"])
    method(tab, show_message=True, persist_selection=persist)
    assert editor.value == "synthetic YAML"
    assert calls == ([(item.node, "profile-a")] if persist else [])
    assert toasts == messages
    assert "尚未应用" in messages[-1]
    assert ("后续应用或订阅更新" in messages[-1]) == (persist and not error)
    assert ("选择缓存失败" in messages[-1]) == error
