import json
import threading
import time

import pytest

from core import network_diagnostic_settings


@pytest.fixture(autouse=True)
def strict_secret_reads_follow_test_secret_store(monkeypatch):
    """Keep transaction snapshots inside each test's in-memory secret stub."""
    monkeypatch.setattr(
        network_diagnostic_settings.security,
        "get_secret_strict",
        lambda ref: network_diagnostic_settings.security.get_secret(ref),
    )


def test_parse_api_keys_splits_and_deduplicates():
    assert network_diagnostic_settings.parse_api_keys(" a, b；a\nc  ") == ["a", "b", "c"]
    assert network_diagnostic_settings.parse_api_keys('["a", "b", "a"]') == ["a", "b"]
    assert network_diagnostic_settings.parse_api_keys(["- c", "1. d"]) == ["c", "d"]


def test_parse_api_keys_extracts_dashboard_and_email_snippets():
    proxycheck_key = "a92709-80366h-34707l-12pmwq"
    ipqs_key = "zXppKcJBZKEfcBrW7S7KPIQjDMCgq9vi"
    vpnapi_key = "6e31466319a94a94b43153b58b24acc3"
    pasted = f"""
dashboard / proxycheck.io
Account Information
API Key
{proxycheck_key}
Plan Tier Free Queries Today 0 / 1K

Welcome to IPQualityScore.com!
Account Details
Email: user@example.com
API KEY: {ipqs_key}
Credit Balance: 1,000

Dashboard
Manage your API, plan, and account settings.
API Key
{vpnapi_key}
"""

    assert network_diagnostic_settings.parse_api_keys(pasted) == [
        proxycheck_key,
        ipqs_key,
        vpnapi_key,
    ]


def test_normalize_services_accepts_common_aliases():
    assert network_diagnostic_settings.normalize_services(["ip.net.coffee", "proxycheck.io", "ipapi.is", "IPQualityScore", "VPN API", "unknown"]) == [
        network_diagnostic_settings.SERVICE_NETCOFFEE,
        network_diagnostic_settings.SERVICE_PROXYCHECK,
        network_diagnostic_settings.SERVICE_IPAPI,
        network_diagnostic_settings.SERVICE_IPQS,
        network_diagnostic_settings.SERVICE_VPNAPI,
    ]


def test_save_and_load_settings_store_api_keys_as_secret_refs(tmp_path, monkeypatch):
    secrets = {}
    deleted = []

    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", tmp_path / "network_diagnostics.json")
    monkeypatch.setattr(network_diagnostic_settings.security, "set_secret", lambda ref, value: secrets.__setitem__(ref, value))
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: secrets.get(ref))
    monkeypatch.setattr(network_diagnostic_settings.security, "delete_secret", lambda ref: deleted.append(ref))

    settings = network_diagnostic_settings.settings_from_values(
        {
            network_diagnostic_settings.SERVICE_PROXYCHECK,
            network_diagnostic_settings.SERVICE_IPQS,
        },
        {
            network_diagnostic_settings.SERVICE_PROXYCHECK: "proxy-a, proxy-b",
            network_diagnostic_settings.SERVICE_IPQS: ["ipqs-a", "ipqs-b"],
        },
    )

    network_diagnostic_settings.save_settings(settings)
    loaded = network_diagnostic_settings.load_settings()

    assert loaded.enabled_services() == [
        network_diagnostic_settings.SERVICE_PROXYCHECK,
    ]
    assert loaded.enabled_services(include_hidden=True) == [
        network_diagnostic_settings.SERVICE_PROXYCHECK,
        network_diagnostic_settings.SERVICE_IPQS,
    ]
    assert loaded.keys_for(network_diagnostic_settings.SERVICE_PROXYCHECK) == ["proxy-a", "proxy-b"]
    assert loaded.keys_for(network_diagnostic_settings.SERVICE_IPQS) == ["ipqs-a", "ipqs-b"]
    assert "proxy-a" not in network_diagnostic_settings.SETTINGS_FILE.read_text(encoding="utf-8")
    assert "ipqs-a" not in network_diagnostic_settings.SETTINGS_FILE.read_text(encoding="utf-8")
    assert deleted == []


def test_load_settings_reuses_cache_and_returns_copy(tmp_path, monkeypatch):
    secrets = {"network-diagnostics:proxycheck:0": "proxy-a"}
    target = tmp_path / "network_diagnostics.json"
    target.write_text(
        json.dumps({
            "services": {
                network_diagnostic_settings.SERVICE_PROXYCHECK: {
                    "enabled": True,
                    "key_refs": ["network-diagnostics:proxycheck:0"],
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", target)
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: secrets.get(ref))
    network_diagnostic_settings.clear_settings_cache()

    original_read_text = type(target).read_text
    read_count = {"value": 0}

    def counting_read_text(self, *args, **kwargs):
        if self == target:
            read_count["value"] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(target), "read_text", counting_read_text)

    first = network_diagnostic_settings.load_settings()
    first.service(network_diagnostic_settings.SERVICE_PROXYCHECK).api_keys.append("mutated")
    second = network_diagnostic_settings.load_settings()

    assert second.keys_for(network_diagnostic_settings.SERVICE_PROXYCHECK) == ["proxy-a"]
    assert read_count["value"] == 1


def test_save_settings_deletes_removed_secret_refs(tmp_path, monkeypatch):
    secrets = {}
    deleted = []

    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", tmp_path / "network_diagnostics.json")
    monkeypatch.setattr(network_diagnostic_settings.security, "set_secret", lambda ref, value: secrets.__setitem__(ref, value))
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: secrets.get(ref))
    monkeypatch.setattr(network_diagnostic_settings.security, "delete_secret", lambda ref: deleted.append(ref))

    first = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_VPNAPI},
        {network_diagnostic_settings.SERVICE_VPNAPI: "vpn-a, vpn-b"},
    )
    second = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_VPNAPI},
        {network_diagnostic_settings.SERVICE_VPNAPI: "vpn-a"},
    )

    network_diagnostic_settings.save_settings(first)
    network_diagnostic_settings.save_settings(second)

    assert "network-diagnostics:vpnapi:1" in deleted


def test_saved_empty_key_pool_does_not_reload_environment_key(tmp_path, monkeypatch):
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", tmp_path / "network_diagnostics.json")
    monkeypatch.setenv("VPNAPI_KEY", "env-vpn-key")
    monkeypatch.setattr(network_diagnostic_settings.security, "set_secret", lambda ref, value: None)
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: None)
    monkeypatch.setattr(network_diagnostic_settings.security, "delete_secret", lambda ref: None)

    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_VPNAPI},
        {network_diagnostic_settings.SERVICE_VPNAPI: ""},
    )

    network_diagnostic_settings.save_settings(settings)
    loaded = network_diagnostic_settings.load_settings()

    assert loaded.service(network_diagnostic_settings.SERVICE_VPNAPI).enabled is True
    assert loaded.keys_for(network_diagnostic_settings.SERVICE_VPNAPI) == []


def test_load_settings_accepts_string_boolean_values(tmp_path, monkeypatch):
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", tmp_path / "network_diagnostics.json")
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: None)
    network_diagnostic_settings.SETTINGS_FILE.write_text(
        json.dumps({
            "services": {
                network_diagnostic_settings.SERVICE_IPQS: {"enabled": "false", "key_refs": []},
                network_diagnostic_settings.SERVICE_VPNAPI: {"enabled": "yes", "key_refs": []},
            },
        }),
        encoding="utf-8",
    )

    loaded = network_diagnostic_settings.load_settings()

    assert loaded.service(network_diagnostic_settings.SERVICE_IPQS).enabled is False
    assert loaded.service(network_diagnostic_settings.SERVICE_VPNAPI).enabled is True


def test_hidden_ipqs_is_preserved_but_not_user_selectable(tmp_path, monkeypatch):
    secrets = {}
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", tmp_path / "network_diagnostics.json")
    monkeypatch.setattr(network_diagnostic_settings.security, "set_secret", lambda ref, value: secrets.__setitem__(ref, value))
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: secrets.get(ref))
    monkeypatch.setattr(network_diagnostic_settings.security, "delete_secret", lambda ref: None)

    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_IPQS},
        {network_diagnostic_settings.SERVICE_IPQS: "hidden-ipqs-key"},
    )

    network_diagnostic_settings.save_settings(settings)
    loaded = network_diagnostic_settings.load_settings()

    assert network_diagnostic_settings.SERVICE_IPQS not in network_diagnostic_settings.VISIBLE_SERVICE_ORDER
    assert loaded.enabled_services() == []
    assert loaded.enabled_services(include_hidden=True) == [network_diagnostic_settings.SERVICE_IPQS]
    assert loaded.keys_for(network_diagnostic_settings.SERVICE_IPQS) == ["hidden-ipqs-key"]


def test_corrupt_settings_file_is_quarantined(tmp_path, monkeypatch):
    settings_file = tmp_path / "network_diagnostics.json"
    settings_file.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: None)
    network_diagnostic_settings.clear_settings_cache()

    loaded = network_diagnostic_settings.load_settings()

    assert loaded.enabled_services(include_hidden=True) == [
        service
        for service, enabled in network_diagnostic_settings.DEFAULT_ENABLED.items()
        if enabled
    ]
    assert not settings_file.exists()
    corrupt_files = list(tmp_path.glob("network_diagnostics.json.corrupt-*"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == "{bad json"


def test_invalid_settings_root_is_quarantined(tmp_path, monkeypatch):
    settings_file = tmp_path / "network_diagnostics.json"
    settings_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: None)
    network_diagnostic_settings.clear_settings_cache()

    network_diagnostic_settings.load_settings()

    assert not settings_file.exists()
    assert len(list(tmp_path.glob("network_diagnostics.json.corrupt-*"))) == 1


def test_save_settings_keeps_old_secret_refs_when_file_write_fails(tmp_path, monkeypatch):
    settings_file = tmp_path / "network_diagnostics.json"
    settings_file.write_text(
        json.dumps({
            "services": {
                network_diagnostic_settings.SERVICE_VPNAPI: {
                    "enabled": True,
                    "key_refs": [
                        "network-diagnostics:vpnapi:0",
                        "network-diagnostics:vpnapi:1",
                    ],
                }
            }
        }),
        encoding="utf-8",
    )
    secrets = {
        "network-diagnostics:vpnapi:0": "old-a",
        "network-diagnostics:vpnapi:1": "old-b",
    }
    deleted = []
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(
        network_diagnostic_settings.security,
        "set_secret",
        lambda ref, value: secrets.__setitem__(ref, value),
    )
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: secrets.get(ref))
    monkeypatch.setattr(
        network_diagnostic_settings.security,
        "delete_secret",
        lambda ref: (deleted.append(ref), secrets.pop(ref, None)),
    )

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(network_diagnostic_settings, "atomic_write_text", fail_write)
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_VPNAPI},
        {network_diagnostic_settings.SERVICE_VPNAPI: "kept-key"},
    )

    try:
        network_diagnostic_settings.save_settings(settings)
    except OSError:
        pass
    else:
        raise AssertionError("save_settings should surface the write failure")

    assert deleted == []
    assert secrets == {
        "network-diagnostics:vpnapi:0": "old-a",
        "network-diagnostics:vpnapi:1": "old-b",
    }


def test_save_settings_aborts_before_mutation_when_snapshot_read_fails(
    tmp_path,
    monkeypatch,
):
    settings_file = tmp_path / "network_diagnostics.json"
    settings_file.write_text(
        json.dumps({
            "services": {
                network_diagnostic_settings.SERVICE_VPNAPI: {
                    "enabled": True,
                    "key_refs": ["network-diagnostics:vpnapi:0"],
                },
            },
        }),
        encoding="utf-8",
    )
    original = settings_file.read_bytes()
    writes = []
    deletes = []
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(
        network_diagnostic_settings.security,
        "get_secret_strict",
        lambda _ref: (_ for _ in ()).throw(RuntimeError("backend unreadable")),
    )
    monkeypatch.setattr(
        network_diagnostic_settings.security,
        "set_secret",
        lambda ref, value: writes.append((ref, value)),
    )
    monkeypatch.setattr(
        network_diagnostic_settings.security,
        "delete_secret",
        lambda ref: deletes.append(ref),
    )
    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_VPNAPI},
        {network_diagnostic_settings.SERVICE_VPNAPI: ["new-key"]},
    )

    with pytest.raises(RuntimeError, match="backend unreadable"):
        network_diagnostic_settings.save_settings(settings)

    assert writes == [] and deletes == []
    assert settings_file.read_bytes() == original


def test_save_settings_ignores_stale_secret_delete_failure(tmp_path, monkeypatch):
    settings_file = tmp_path / "network_diagnostics.json"
    settings_file.write_text(
        json.dumps({
            "services": {
                network_diagnostic_settings.SERVICE_VPNAPI: {
                    "enabled": True,
                    "key_refs": [
                        "network-diagnostics:vpnapi:0",
                        "network-diagnostics:vpnapi:1",
                    ],
                }
            }
        }),
        encoding="utf-8",
    )
    secrets = {}
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(network_diagnostic_settings.security, "set_secret", lambda ref, value: secrets.__setitem__(ref, value))
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: secrets.get(ref))

    def fail_delete(_ref):
        raise RuntimeError("keyring busy")

    monkeypatch.setattr(network_diagnostic_settings.security, "delete_secret", fail_delete)

    settings = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_VPNAPI},
        {network_diagnostic_settings.SERVICE_VPNAPI: "kept-key"},
    )

    network_diagnostic_settings.save_settings(settings)

    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["services"][network_diagnostic_settings.SERVICE_VPNAPI]["key_refs"] == [
        "network-diagnostics:vpnapi:0"
    ]


def test_load_and_save_ignore_noncanonical_secret_refs(tmp_path, monkeypatch):
    settings_file = tmp_path / "network_diagnostics.json"
    settings_file.write_text(
        json.dumps({
            "services": {
                network_diagnostic_settings.SERVICE_VPNAPI: {
                    "enabled": True,
                    "key_refs": [
                        "app:unrelated:secret",
                        "network-diagnostics:vpnapi:00",
                        "network-diagnostics:vpnapi:²",
                        f"network-diagnostics:vpnapi:{'9' * 5000}",
                        "network-diagnostics:vpnapi:0",
                    ],
                },
                "evil-service": {
                    "enabled": True,
                    "key_refs": ["app:another:secret"],
                },
            },
        }),
        encoding="utf-8",
    )
    reads = []
    deleted = []
    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(
        network_diagnostic_settings.security,
        "get_secret",
        lambda ref: (reads.append(ref), "vpn-key")[1],
    )
    monkeypatch.setattr(network_diagnostic_settings.security, "set_secret", lambda *_args: None)
    monkeypatch.setattr(
        network_diagnostic_settings.security,
        "delete_secret",
        lambda ref: deleted.append(ref),
    )
    network_diagnostic_settings.clear_settings_cache()

    loaded = network_diagnostic_settings.load_settings()
    network_diagnostic_settings.save_settings(loaded)

    assert reads
    assert set(reads) == {"network-diagnostics:vpnapi:0"}
    assert "app:unrelated:secret" not in deleted
    assert "app:another:secret" not in deleted


def test_concurrent_saves_cannot_mix_file_and_secret_values(tmp_path, monkeypatch):
    settings_file = tmp_path / "network_diagnostics.json"
    secrets = {}
    first_write_entered = threading.Event()
    release_first_write = threading.Event()
    second_secret_written = threading.Event()
    real_atomic_write = network_diagnostic_settings.atomic_write_text

    monkeypatch.setattr(network_diagnostic_settings, "SETTINGS_FILE", settings_file)

    def set_secret(ref, value):
        secrets[ref] = value
        if value == "second-key":
            second_secret_written.set()

    monkeypatch.setattr(network_diagnostic_settings.security, "set_secret", set_secret)
    monkeypatch.setattr(network_diagnostic_settings.security, "get_secret", lambda ref: secrets.get(ref))
    monkeypatch.setattr(
        network_diagnostic_settings.security,
        "delete_secret",
        lambda ref: secrets.pop(ref, None),
    )

    def controlled_write(path, content, *args, **kwargs):
        payload = json.loads(content)
        refs = payload["services"][network_diagnostic_settings.SERVICE_VPNAPI]["key_refs"]
        if refs and secrets.get(refs[0]) == "first-key" and not first_write_entered.is_set():
            first_write_entered.set()
            assert release_first_write.wait(1)
        return real_atomic_write(path, content, *args, **kwargs)

    monkeypatch.setattr(network_diagnostic_settings, "atomic_write_text", controlled_write)
    first = network_diagnostic_settings.settings_from_values(
        {network_diagnostic_settings.SERVICE_VPNAPI},
        {network_diagnostic_settings.SERVICE_VPNAPI: "first-key"},
    )
    second = network_diagnostic_settings.settings_from_values(
        set(),
        {network_diagnostic_settings.SERVICE_VPNAPI: "second-key"},
    )
    first_thread = threading.Thread(target=network_diagnostic_settings.save_settings, args=(first,))
    second_thread = threading.Thread(target=network_diagnostic_settings.save_settings, args=(second,))

    first_thread.start()
    assert first_write_entered.wait(1)
    second_thread.start()
    time.sleep(0.05)
    assert not second_secret_written.is_set()
    release_first_write.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    payload = json.loads(settings_file.read_text(encoding="utf-8"))
    service_payload = payload["services"][network_diagnostic_settings.SERVICE_VPNAPI]
    [ref] = service_payload["key_refs"]
    assert service_payload["enabled"] is False
    assert secrets[ref] == "second-key"
