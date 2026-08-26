import ast
import hashlib
import io
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

from core import local_proxy, remote_proxy


def _release(tag="v1.19.30"):
    return {
        "tag_name": tag,
        "assets": [
            {
                "name": f"mihomo-windows-amd64-{tag}.zip",
                "browser_download_url": f"https://github.example/{tag}.zip",
            }
        ],
    }


def _patch_local_core_paths(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_BIN_DIR", bin_dir)
    monkeypatch.setattr(local_proxy, "MIHOMO_RELEASE_STATE_PATH", bin_dir / "mihomo-release.json")
    monkeypatch.setattr(local_proxy, "MIHOMO_PENDING_BINARY_PATH", bin_dir / "mihomo.pending.exe")
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_args, **_kwargs: False)
    return bin_dir / "mihomo.exe"


def test_mihomo_asset_picker_prefers_canonical_generic_build():
    assets = [
        {"name": "mihomo-windows-amd64-compatible-v1.19.30.zip"},
        {"name": "mihomo-windows-amd64-v1-go120-v1.19.30.zip"},
        {"name": "mihomo-windows-amd64-v3-v1.19.30.zip"},
        {"name": "mihomo-windows-amd64-v1.19.30.zip"},
    ]

    selected = local_proxy._pick_mihomo_asset(assets, "windows-amd64", "v1.19.30")

    assert selected["name"] == "mihomo-windows-amd64-v1.19.30.zip"


def test_mihomo_release_asset_verifies_size_and_github_digest():
    payload = b"verified-release-payload"
    asset = {
        "size": len(payload),
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }

    local_proxy._verify_mihomo_release_asset(asset, payload)

    with pytest.raises(RuntimeError, match="SHA-256"):
        local_proxy._verify_mihomo_release_asset(
            {**asset, "digest": "sha256:" + "0" * 64},
            payload,
        )
    with pytest.raises(RuntimeError, match="大小校验失败"):
        local_proxy._verify_mihomo_release_asset({**asset, "size": len(payload) + 1}, payload)


def test_mihomo_zip_prefers_exact_binary_and_rejects_non_pe(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("nested/clash-helper.exe", b"MZhelper")
        archive.writestr("mihomo.exe", b"MZcanonical")
    target = tmp_path / "mihomo.exe"

    local_proxy._write_mihomo_payload(target, "https://example/mihomo.zip", payload.getvalue())

    assert target.read_bytes() == b"MZcanonical"
    with pytest.raises(RuntimeError, match="文件头校验失败"):
        local_proxy._write_mihomo_payload(target, "https://example/mihomo.exe", b"not-a-pe")
    assert target.read_bytes() == b"MZcanonical"


def test_mihomo_release_check_uses_shorter_retry_after_failure():
    now = time.time()

    assert local_proxy._mihomo_release_check_due(
        {"checked_at_epoch": now - 60, "last_check_success": True},
        now=now,
    ) is False
    assert local_proxy._mihomo_release_check_due(
        {
            "checked_at_epoch": now - local_proxy.MIHOMO_RELEASE_FAILURE_RETRY_SECONDS - 1,
            "last_check_success": False,
        },
        now=now,
    ) is True


def test_local_core_update_failure_keeps_usable_existing_binary(monkeypatch, tmp_path):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old")
    monkeypatch.setattr(
        local_proxy,
        "_try_mihomo_binary_info",
        lambda path: ("1.19.25", "Mihomo Meta v1.19.25 windows amd64") if Path(path) == binary else None,
    )
    monkeypatch.setattr(
        local_proxy,
        "_fetch_mihomo_release",
        lambda: (_ for _ in ()).throw(OSError("offline")),
    )

    result = local_proxy._ensure_latest_mihomo_binary()

    assert result == binary
    state = json.loads(local_proxy.MIHOMO_RELEASE_STATE_PATH.read_text(encoding="utf-8"))
    assert state["last_check_success"] is False
    assert state["installed_version"] == "1.19.25"


def test_local_core_startup_never_waits_for_release_when_core_is_usable(
    monkeypatch, tmp_path
):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old")
    monkeypatch.setattr(
        local_proxy,
        "_try_mihomo_binary_info",
        lambda path: ("1.19.25", "Mihomo Meta v1.19.25 windows amd64")
        if Path(path) == binary
        else None,
    )
    monkeypatch.setattr(
        local_proxy,
        "_fetch_mihomo_release",
        lambda: (_ for _ in ()).throw(AssertionError("startup must not query GitHub")),
    )

    assert local_proxy._ensure_mihomo_binary() == binary


def test_local_core_startup_does_not_wait_behind_background_update(
    monkeypatch, tmp_path
):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old")
    update_lock = threading.RLock()
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    monkeypatch.setattr(local_proxy, "_MIHOMO_BINARY_LOCK", update_lock)
    monkeypatch.setattr(
        local_proxy,
        "_try_mihomo_binary_info",
        lambda path: ("1.19.25", "Mihomo Meta v1.19.25 windows amd64")
        if Path(path) == binary
        else None,
    )

    def hold_update_lock():
        with update_lock:
            lock_acquired.set()
            release_lock.wait(2.0)

    worker = threading.Thread(target=hold_update_lock, daemon=True)
    worker.start()
    assert lock_acquired.wait(1.0) is True
    started = time.monotonic()
    try:
        assert local_proxy._ensure_mihomo_binary() == binary
        assert time.monotonic() - started < 0.5
    finally:
        release_lock.set()
        worker.join(timeout=1.0)


def test_core_update_check_is_scheduled_in_background(monkeypatch, tmp_path):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"core")
    completed = threading.Event()
    monkeypatch.setattr(local_proxy, "_MIHOMO_UPDATE_THREAD", None)
    monkeypatch.setattr(local_proxy, "_load_mihomo_release_state", lambda: {})
    monkeypatch.setattr(
        local_proxy,
        "_check_mihomo_update_availability",
        lambda: completed.set() or binary,
    )

    assert local_proxy._schedule_mihomo_update_check() is True
    assert completed.wait(1.0) is True


def test_background_core_check_records_update_without_downloading(monkeypatch, tmp_path):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old")
    monkeypatch.setattr(
        local_proxy,
        "_try_mihomo_binary_info",
        lambda path: ("1.19.25", "Mihomo Meta v1.19.25 windows amd64")
        if Path(path) == binary
        else None,
    )
    monkeypatch.setattr(local_proxy, "_fetch_mihomo_release", lambda: _release())
    monkeypatch.setattr(
        local_proxy,
        "_download_mihomo_binary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("background metadata check must not download")
        ),
    )

    assert local_proxy._check_mihomo_update_availability() == binary
    state = json.loads(local_proxy.MIHOMO_RELEASE_STATE_PATH.read_text(encoding="utf-8"))
    assert state["installed_version"] == "1.19.25"
    assert state["latest_version"] == "1.19.30"
    assert not local_proxy.MIHOMO_PENDING_BINARY_PATH.exists()
    assert "手动下载" in local_proxy._local_mihomo_core_status_detail()


def test_background_core_metadata_fetch_does_not_hold_binary_lock(monkeypatch, tmp_path):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old")
    update_lock = threading.RLock()
    fetch_started = threading.Event()
    finish_fetch = threading.Event()
    outcome = []
    monkeypatch.setattr(local_proxy, "_MIHOMO_BINARY_LOCK", update_lock)
    monkeypatch.setattr(
        local_proxy,
        "_try_mihomo_binary_info",
        lambda path: ("1.19.25", "Mihomo Meta v1.19.25 windows amd64")
        if Path(path) == binary
        else None,
    )

    def fetch_release():
        fetch_started.set()
        finish_fetch.wait(2.0)
        return _release()

    monkeypatch.setattr(local_proxy, "_fetch_mihomo_release", fetch_release)

    def check():
        try:
            outcome.append(local_proxy._check_mihomo_update_availability())
        except Exception as exc:  # pragma: no cover - asserted below
            outcome.append(exc)

    worker = threading.Thread(target=check, daemon=True)
    worker.start()
    assert fetch_started.wait(1.0) is True
    acquired = update_lock.acquire(blocking=False)
    try:
        assert acquired is True
    finally:
        if acquired:
            update_lock.release()
        finish_fetch.set()
        worker.join(timeout=2.0)
    assert outcome == [binary]


def test_older_background_core_response_does_not_overwrite_foreground_state(
    monkeypatch,
    tmp_path,
):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old")
    monkeypatch.setattr(
        local_proxy,
        "_try_mihomo_binary_info",
        lambda path: ("1.19.25", "Mihomo Meta v1.19.25 windows amd64")
        if Path(path) == binary
        else None,
    )

    def fetch_release():
        local_proxy._save_mihomo_release_state(
            {
                "checked_at_epoch": time.time() + 10,
                "last_check_success": True,
                "latest_tag": "v1.19.31",
                "latest_version": "1.19.31",
                "installed_version": "1.19.25",
            }
        )
        return _release()

    monkeypatch.setattr(local_proxy, "_fetch_mihomo_release", fetch_release)

    assert local_proxy._check_mihomo_update_availability() == binary
    state = json.loads(local_proxy.MIHOMO_RELEASE_STATE_PATH.read_text(encoding="utf-8"))
    assert state["latest_version"] == "1.19.31"


def test_manual_core_update_reuses_verified_staged_release(monkeypatch, tmp_path):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old")
    local_proxy.MIHOMO_PENDING_BINARY_PATH.write_bytes(b"new")
    monkeypatch.setattr(
        local_proxy,
        "_try_mihomo_binary_info",
        lambda path: {
            binary: ("1.19.25", "Mihomo Meta v1.19.25 windows amd64"),
            local_proxy.MIHOMO_PENDING_BINARY_PATH: (
                "1.19.30",
                "Mihomo Meta v1.19.30 windows amd64",
            ),
        }.get(Path(path)),
    )
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_a, **_k: True)
    monkeypatch.setattr(local_proxy, "_fetch_mihomo_release", lambda: _release())
    monkeypatch.setattr(
        local_proxy,
        "_download_mihomo_binary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("verified staged release must be reused")
        ),
    )

    assert local_proxy._ensure_latest_mihomo_binary(force_check=True) == binary
    state = json.loads(local_proxy.MIHOMO_RELEASE_STATE_PATH.read_text(encoding="utf-8"))
    assert state["pending_version"] == "1.19.30"
    assert state["latest_version"] == "1.19.30"


def test_missing_staged_core_does_not_report_false_pending_update(monkeypatch, tmp_path):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old")
    local_proxy._save_mihomo_release_state(
        {
            "installed_version": "1.19.25",
            "latest_version": "1.19.30",
            "pending_version": "1.19.30",
            "pending_asset": "missing.zip",
            "last_check_success": True,
        }
    )

    assert "待代理重启" not in local_proxy._local_mihomo_core_status_detail()
    assert local_proxy._apply_pending_mihomo_update(binary) is False
    state = json.loads(local_proxy.MIHOMO_RELEASE_STATE_PATH.read_text(encoding="utf-8"))
    assert "pending_version" not in state
    assert "pending_asset" not in state


def test_local_core_update_is_staged_while_running_then_applied(monkeypatch, tmp_path):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old")
    running = {"value": True}
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_a, **_k: running["value"])

    def binary_info(path):
        path = Path(path)
        if path == local_proxy.MIHOMO_PENDING_BINARY_PATH and path.exists():
            return "1.19.30", "Mihomo Meta v1.19.30 windows amd64"
        if path == binary and path.exists():
            payload = path.read_bytes()
            version = "1.19.30" if payload == b"new" else "1.19.25"
            return version, f"Mihomo Meta v{version} windows amd64"
        return None

    monkeypatch.setattr(local_proxy, "_try_mihomo_binary_info", binary_info)
    monkeypatch.setattr(local_proxy, "_fetch_mihomo_release", lambda: _release())

    def download(target, *, release):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"new")
        return "1.19.30", "Mihomo Meta v1.19.30 windows amd64", release["assets"][0]["name"]

    monkeypatch.setattr(local_proxy, "_download_mihomo_binary", download)

    assert local_proxy._ensure_latest_mihomo_binary() == binary
    assert binary.read_bytes() == b"old"
    assert local_proxy.MIHOMO_PENDING_BINARY_PATH.read_bytes() == b"new"
    assert "待代理重启时应用" in local_proxy._local_mihomo_core_status_detail()

    running["value"] = False
    assert local_proxy._apply_pending_mihomo_update(binary) is True
    assert binary.read_bytes() == b"new"
    assert not local_proxy.MIHOMO_PENDING_BINARY_PATH.exists()


def test_local_core_update_does_not_restart_running_proxy_by_default(monkeypatch, tmp_path):
    binary = _patch_local_core_paths(monkeypatch, tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"old")
    local_proxy.MIHOMO_PENDING_BINARY_PATH.write_bytes(b"new")
    monkeypatch.setattr(local_proxy.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        local_proxy,
        "_ensure_latest_mihomo_binary",
        lambda **_kwargs: binary,
    )
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17897})
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda *_a, **_k: True)
    monkeypatch.setattr(local_proxy, "_is_port_listening", lambda _port: True)
    monkeypatch.setattr(local_proxy, "_local_mihomo_core_status_detail", lambda: "update staged")
    monkeypatch.setattr(
        local_proxy,
        "_start_local_mihomo",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not restart")),
    )

    assert local_proxy.update_local_mihomo_core() == "update staged"


def test_managed_local_config_path_ignores_state_path_outside_owned_directory(
    monkeypatch,
    tmp_path,
):
    managed_dir = tmp_path / "managed"
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_CONFIG_DIR", managed_dir)
    unrelated = tmp_path / "unrelated.yaml"

    selected = local_proxy._managed_local_config_path({"config_path": str(unrelated)})

    assert selected == managed_dir / "config.yaml"


def test_local_mihomo_log_rotation_keeps_one_previous_log(monkeypatch, tmp_path):
    log_path = tmp_path / "mihomo.log"
    payload = b"old" + (b"x" * 1021) + b"newest"
    log_path.write_bytes(payload)
    monkeypatch.setattr(local_proxy, "LOCAL_PROXY_LOG_PATH", log_path)

    local_proxy._rotate_local_mihomo_log(max_bytes=1024)

    assert not log_path.exists()
    rotated = log_path.with_suffix(".log.1").read_bytes()
    assert len(rotated) == 1024
    assert rotated == payload[-1024:]


def test_remote_core_updater_embedded_python_is_syntactically_valid_and_hardened():
    command = remote_proxy._build_ensure_mihomo_command("/home/me")
    embedded = command.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    ast.parse(embedded)
    assert "ProxyHandler({})" in command
    assert "hmac.compare_digest" in command
    assert "os.replace(candidate_path, target)" in command
    assert "exact_name" in command
    assert "kernel_version=" in command


def test_missing_remote_core_is_bootstrapped_once_before_isolated_probe(monkeypatch):
    probes = []
    bootstraps = []

    def probe(server, text, timeout=8):
        probes.append((server, text, timeout))
        if len(probes) == 1:
            raise remote_proxy.RemoteMihomoCoreMissingError("missing")
        return "isolated-ok"

    monkeypatch.setattr(remote_proxy, "probe_ai_proxy_candidate_isolated", probe)
    monkeypatch.setattr(
        remote_proxy,
        "ensure_remote_mihomo_core",
        lambda server: bootstraps.append(server) or "ready",
    )

    result = remote_proxy._probe_ai_proxy_candidate_with_core_bootstrap("gpu", "node", timeout=9)

    assert result == "isolated-ok"
    assert bootstraps == ["gpu"]
    assert len(probes) == 2


def test_remote_start_script_rotates_large_managed_log():
    script = remote_proxy._build_start_script(
        "/home/me/.config/mihomo",
        "/home/me/.config/api-switcher",
        "/home/me/.local/bin",
        7890,
    )

    assert 'log_size="$(wc -c < "$LOG_FILE"' in script
    assert 'mv "$LOG_FILE" "$LOG_FILE.1"' in script
