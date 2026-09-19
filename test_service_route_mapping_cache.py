"""Pure catalog presentation caches: no Tk windows or user data."""
import copy
from types import MethodType, SimpleNamespace

from ui.dialogs import service_routes_dialog as routes


def _view(catalog):
    view = SimpleNamespace(_catalog=catalog)
    for name in ("_ensure_mapping_cache", "_profile_values", "_node_values"):
        setattr(view, name, MethodType(getattr(routes.ServiceRoutesDialog, name), view))
    return view


def _catalog(labels):
    return [{"id": "a", "name": "Subscription", "network_type": "residential",
             "nodes": [{"key": f"key-{index}", "label": label} for index, label in enumerate(labels)]}]


def _legacy_names(labels, default):
    mapping = {default: ""}
    for index, value in enumerate(labels):
        base = " ".join(routes.safe_feedback_text(value).split())
        label, suffix = base, 2
        while label in mapping:
            label = f"{base} ({suffix})"
            suffix += 1
        mapping[label] = f"key-{index}"
    return mapping


def test_duplicate_labels_match_legacy_order_including_explicit_suffix_collisions():
    labels = ["node", "node (2)", "node", "node (2)", "node", "node (3)",
              routes.DEFAULT_NODE, routes.DEFAULT_NODE, "node password=synthetic-one",
              "node password=synthetic-two", "  same   words ", "same words"]
    catalog = _catalog(labels)
    original = copy.deepcopy(catalog)
    view = _view(catalog)
    assert view._node_values("a") == _legacy_names(labels, routes.DEFAULT_NODE)
    assert catalog == original
    assert "synthetic-one" not in repr(view._node_values("a"))


def test_shared_mapping_redacts_each_node_once_across_many_service_rows(monkeypatch):
    view = _view(_catalog([f"node-{index}" for index in range(1000)]))
    calls = []
    original = routes.safe_feedback_text

    def redact(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(routes, "safe_feedback_text", redact)
    mapping = view._node_values("a")
    for _ in range(36):
        assert view._node_values("a") is mapping
    assert len(calls) == 1000
    assert view._node_reverse_cache["a"]["key-999"] == "node-999"


def test_catalog_replacement_invalidates_label_and_reverse_mapping():
    view = _view(_catalog(["old-node"]))
    old = view._node_values("a")
    view._catalog = _catalog(["new-node"])
    assert view._node_values("a") == {routes.DEFAULT_NODE: "", "new-node": "key-0"}
    assert view._node_values("a") is not old
    assert view._node_reverse_cache["a"]["key-0"] == "new-node"


def test_replaced_node_list_invalidates_only_its_profile():
    catalog = _catalog(["first"])
    catalog.append({"id": "b", "name": "Other", "nodes": [{"key": "b-1", "label": "stable"}]})
    view = _view(catalog)
    first, other = view._node_values("a"), view._node_values("b")
    catalog[0]["nodes"] = [{"key": "replacement", "label": "new-name"}]
    assert view._node_values("a") is not first
    assert view._node_values("b") is other
    assert "key-0" not in view._node_reverse_cache["a"]


def test_metadata_changes_invalidate_profile_labels_but_not_node_cache():
    view = _view(_catalog(["node"]))
    old_profiles = view._profile_values()
    old_nodes = view._node_values("a")
    view._catalog[0].update(name="Renamed", network_type="datacenter")
    assert view._profile_values() == {routes.DEFAULT_PROFILE: "", "Renamed · 非家宽": "a"}
    assert view._profile_values() is not old_profiles
    assert view._node_values("a") is old_nodes
    assert view._profile_values("custom:1")[routes.DEFAULT_CUSTOM_PROFILE] == ""
    assert routes.DEFAULT_PROFILE not in view._profile_values("custom:1")


def test_same_named_profiles_have_stable_unique_choices():
    view = _view([{"id": str(index), "name": "Same", "nodes": []} for index in range(3000)])
    choices = view._profile_values()
    assert len(choices) == 3001
    assert choices["Same · 请先拉取 (3000)"] == "2999"
    assert len(set(choices.values())) == len(choices)


def test_duplicate_node_keys_keep_first_label_like_previous_lookup():
    catalog = _catalog(["first", "second"])
    catalog[0]["nodes"][1]["key"] = "key-0"
    view = _view(catalog)
    view._node_values("a")
    assert view._node_reverse_cache["a"]["key-0"] == "first"


def test_large_duplicate_names_use_linear_number_of_suffix_lookups():
    class Counter(dict):
        probes = 0

        def __contains__(self, value):
            self.probes += 1
            return super().__contains__(value)

    mapping, suffixes = Counter(), {}
    for index in range(5000):
        mapping[routes._unused_label("same", mapping, suffixes)] = index
    assert len(mapping) == 5000
    assert mapping.probes < 10001
