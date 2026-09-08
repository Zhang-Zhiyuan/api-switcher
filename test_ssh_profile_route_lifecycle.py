from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from core import profile_manager, proxy_routing, remote_proxy, security
from models.profile import SSHProfile


ROUTES = {"service_profile_bindings": {"openai": "home", "youtube": "dc"},
          "service_node_bindings": {"openai": "a" * 64}, "builtin_sites": {"youtube": True}}


@pytest.fixture
def ssh_routes(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(profile_manager, "_disconnect_ssh_profiles", lambda _names: None)
    secrets = {}
    monkeypatch.setattr(security, "get_secret", secrets.get)
    monkeypatch.setattr(security, "get_secret_strict", secrets.get)
    monkeypatch.setattr(security, "set_secret", lambda key, value: secrets.__setitem__(key, value))
    monkeypatch.setattr(security, "delete_secret", lambda key: secrets.pop(key, None))
    profile_manager.save_ssh_profile(SSHProfile(name="old", host="host.example.test"))
    profile_manager.set_active_ssh("old")
    proxy_routing._save_ssh_routes("old", ROUTES)
    return secrets


@pytest.mark.parametrize("with_secrets", [False, True])
def test_ssh_rename_migrates_route_authority_and_fixed_nodes(ssh_routes, with_secrets):
    profile = SSHProfile(name="new", host="host.example.test")
    if with_secrets:
        profile_manager.save_ssh_profile_with_secrets(profile, {}, previous_name="old")
    else:
        profile_manager.save_ssh_profile(profile, previous_name="old")
    assert not proxy_routing._host_path("old").exists()
    assert proxy_routing.load_ssh_routes("new") == proxy_routing.normalize_routes(ROUTES)
    assert proxy_routing.ssh_bindings_for_profile("home")[0].startswith("new / ")
    assert profile_manager._load_store()["active_ssh_profile"] == "new"


def test_ssh_delete_releases_subscription_references(ssh_routes):
    profile_manager.delete_ssh_profile("old")
    assert proxy_routing.ssh_bindings_for_profile("home") == ()
    assert proxy_routing.ssh_bindings_for_profile("dc") == ()
    assert not proxy_routing._host_path("old").exists()
    assert profile_manager.list_ssh_profiles() == []


@pytest.mark.parametrize("action", ["rename", "delete", "rename_with_secrets"])
def test_failed_profile_save_restores_exact_route_bytes(ssh_routes, monkeypatch, action):
    original = proxy_routing._host_path("old").read_bytes()
    store = profile_manager.PROFILES_FILE.read_bytes()
    monkeypatch.setattr(profile_manager, "_save_store", lambda _store: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        if action == "delete":
            profile_manager.delete_ssh_profile("old")
        elif action == "rename_with_secrets":
            profile_manager.save_ssh_profile_with_secrets(SSHProfile(name="new", host="host.example.test"), {}, previous_name="old")
        else:
            profile_manager.save_ssh_profile(SSHProfile(name="new", host="host.example.test"), previous_name="old")
    assert proxy_routing._host_path("old").read_bytes() == original
    assert not proxy_routing._host_path("new").exists()
    assert profile_manager.PROFILES_FILE.read_bytes() == store


def test_failed_route_write_does_not_commit_ssh_rename(ssh_routes, monkeypatch):
    original = profile_manager.PROFILES_FILE.read_bytes()
    routes = proxy_routing._host_path("old").read_bytes()
    monkeypatch.setattr(proxy_routing, "atomic_write_text", lambda *_args: (_ for _ in ()).throw(OSError("route write failed")))
    with pytest.raises(OSError, match="route write failed"):
        profile_manager.save_ssh_profile(SSHProfile(name="new", host="host.example.test"), previous_name="old")
    assert profile_manager.PROFILES_FILE.read_bytes() == original
    assert proxy_routing._host_path("old").read_bytes() == routes


def test_ssh_rename_never_overwrites_destination_route_registry(ssh_routes):
    proxy_routing._save_ssh_routes("new", {"service_profile_bindings": {"claude": "other"}})
    old = proxy_routing._host_path("old").read_bytes()
    new = proxy_routing._host_path("new").read_bytes()
    with pytest.raises(ValueError, match="已有分流配置"):
        profile_manager.save_ssh_profile(SSHProfile(name="new", host="host.example.test"), previous_name="old")
    assert proxy_routing._host_path("old").read_bytes() == old
    assert proxy_routing._host_path("new").read_bytes() == new
    assert profile_manager.list_ssh_profiles()[0].name == "old"


def test_delete_secret_failure_restores_routes_and_profile(ssh_routes, monkeypatch):
    ssh_routes["ssh:old:password"] = "test-password"
    original = proxy_routing._host_path("old").read_bytes()
    monkeypatch.setattr(security, "delete_secret", lambda _key: (_ for _ in ()).throw(OSError("secret delete failed")))
    with pytest.raises(OSError, match="secret delete failed"):
        profile_manager.delete_ssh_profile("old")
    assert proxy_routing._host_path("old").read_bytes() == original
    assert profile_manager.list_ssh_profiles()[0].name == "old"
    assert ssh_routes["ssh:old:password"] == "test-password"


def test_secret_save_waits_for_host_before_acquiring_profile_lock(ssh_routes, monkeypatch):
    started = threading.Event()
    real_host_lock = proxy_routing.host_lock
    main_thread = threading.get_ident()

    def observed_host_lock(name):
        if threading.get_ident() != main_thread:
            started.set()
        return real_host_lock(name)

    monkeypatch.setattr(proxy_routing, "host_lock", observed_host_lock)

    def save():
        profile_manager.save_ssh_profile_with_secrets(SSHProfile(name="new", host="host.example.test"), {}, previous_name="old")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with proxy_routing.host_lock("old"):
            saving = executor.submit(save)
            assert started.wait(5)
            assert profile_manager._STORE_CACHE_LOCK.acquire(timeout=1)
            profile_manager._STORE_CACHE_LOCK.release()
            assert not saving.done()
        saving.result(timeout=5)
    assert proxy_routing.load_ssh_routes("new")["service_profile_bindings"] == ROUTES["service_profile_bindings"]
