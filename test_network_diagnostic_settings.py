import json

from core import network_diagnostic_settings


def test_parse_api_keys_splits_and_deduplicates():
    assert network_diagnostic_settings.parse_api_keys(" a, b；a\nc  ") == ["a", "b", "c"]
    assert network_diagnostic_settings.parse_api_keys('["a", "b", "a"]') == ["a", "b"]
    assert network_diagnostic_settings.parse_api_keys(["- c", "1. d"]) == ["c", "d"]


def test_normalize_services_accepts_common_aliases():
    assert network_diagnostic_settings.normalize_services(["proxycheck.io", "IPQualityScore", "VPN API", "unknown"]) == [
        network_diagnostic_settings.SERVICE_PROXYCHECK,
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
        network_diagnostic_settings.SERVICE_IPQS,
    ]
    assert loaded.keys_for(network_diagnostic_settings.SERVICE_PROXYCHECK) == ["proxy-a", "proxy-b"]
    assert loaded.keys_for(network_diagnostic_settings.SERVICE_IPQS) == ["ipqs-a", "ipqs-b"]
    assert "proxy-a" not in network_diagnostic_settings.SETTINGS_FILE.read_text(encoding="utf-8")
    assert "ipqs-a" not in network_diagnostic_settings.SETTINGS_FILE.read_text(encoding="utf-8")
    assert deleted == []


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
