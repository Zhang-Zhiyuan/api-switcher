"""Capture real application windows using isolated, synthetic local data only.

This is a visual audit, not a network/deployment or authenticated login test.
The parent starts a child through release_check's credential/proxy isolation.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import ctypes
import json
import os
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch


WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))


def capture_window_image(window, *, onscreen=False):
    """Capture only a mapped native HWND; never fall back to desktop pixels."""
    from PIL import ImageGrab

    if os.name != "nt":
        raise RuntimeError("native-window capture currently requires Windows")
    window.update_idletasks()
    if not window.winfo_ismapped():
        raise RuntimeError("refusing to capture an unmapped preview window")
    get_parent = ctypes.windll.user32.GetParent
    get_parent.argtypes, get_parent.restype = (ctypes.c_void_p,), ctypes.c_void_p
    hwnd = get_parent(window.winfo_id())
    if not hwnd:
        raise RuntimeError("preview has no native window handle")
    if onscreen:
        # Explicit comparison mode for Tk/PrintWindow repaint artifacts. Never
        # read desktop pixels unless this mapped preview owns the foreground.
        topmost = window.attributes("-topmost")
        try:
            window.attributes("-topmost", True)
            window.lift()
            window.focus_force()
            window.update()
            foreground = ctypes.windll.user32.GetForegroundWindow
            foreground.argtypes, foreground.restype = (), ctypes.c_void_p
            if foreground() != hwnd:
                raise RuntimeError("refusing screen capture: preview is not the foreground window")
            left, top = window.winfo_rootx(), window.winfo_rooty()
            return ImageGrab.grab(bbox=(left, top, left + window.winfo_width(), top + window.winfo_height()), all_screens=True)
        finally:
            window.attributes("-topmost", topmost)
    redraw = ctypes.windll.user32.RedrawWindow
    redraw.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
    redraw(hwnd, None, None, 0x1 | 0x4 | 0x80 | 0x100)
    return ImageGrab.grab(window=hwnd)


def seed_data():
    from config import paths
    from core import local_proxy, profile_manager, remote_proxy, security
    from models.profile import ClaudeAccountProfile, ClaudeProfile, CodexAccountProfile, CodexProfile, SSHProfile
    import yaml

    store = profile_manager._get_default_store()
    store["claude_profiles"] = [ClaudeProfile(
        "演示 Claude 中转配置 · 长名称布局检查", "visual:claude-key",
        "https://claude.synthetic.example.invalid", provider="custom",
    ).to_dict()]
    store["codex_profiles"] = [CodexProfile(
        "演示 Codex 中转配置 · 长名称布局检查", api_key_ref="visual:codex-key",
        custom_base_url="https://codex.synthetic.example.invalid/v1", model_provider="custom",
    ).to_dict()]
    for kind, klass in (("claude", ClaudeAccountProfile), ("codex", CodexAccountProfile)):
        store[f"{kind}_account_profiles"] = [klass(
            "合成账号 · 用于布局检查", f"visual:{kind}-account", "demo@example.invalid", "2026-09-21T12:00:00Z",
        ).to_dict()]
    store["ssh_profiles"] = [SSHProfile(
        "演示开发服务器 · 不会连接", "server.example.invalid", username="demo",
    ).to_dict()]
    profile_manager._save_store(store)
    security.set_secret("visual:claude-key", "synthetic-only-claude-key")
    security.set_secret("visual:codex-key", "synthetic-only-codex-key")
    security.set_secret_json("visual:claude-account", {"claudeAiOauth": {
        "accessToken": "synthetic-access", "refreshToken": "synthetic-refresh",
    }})
    claim = base64.urlsafe_b64encode(json.dumps({"sub": "synthetic", "email": "demo@example.invalid"}).encode()).decode().rstrip("=")
    security.set_secret_json("visual:codex-account", {"auth_mode": "chatgpt", "tokens": {
        "id_token": f"synthetic.{claim}.signature", "access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
    }})
    ids = []
    for index, (name, network_type) in enumerate((("家宽 A · AI 专用", "residential"), ("非家宽 B · 浏览与视频", "datacenter"))):
        nodes = [{"name": f"{'日本' if index == 0 else '美国'} · 合成节点 {i:02}", "type": "http",
                  "server": "proxy.example.invalid", "port": 12000 + index * 100 + i} for i in range(12)]
        source = Path(paths.STORAGE_DIR) / f"synthetic-{index}.yaml"
        source.write_text(yaml.safe_dump({"proxies": nodes}, allow_unicode=True), encoding="utf-8")
        remote_proxy.import_proxy_subscription_file(source)
        profile_id = remote_proxy.load_proxy_subscription_state()["active_profile_id"]
        remote_proxy.rename_proxy_subscription_profile(profile_id, name)
        remote_proxy.set_proxy_subscription_network_type(profile_id, network_type)
        remote_proxy.save_proxy_subscription_profile_state(profile_id, url=f"https://subscription.example.invalid/{index}")
        ids.append(profile_id)
    remote_proxy.set_active_proxy_subscription_profile(ids[0])
    local_proxy.save_local_proxy_preferences(
        builtin_sites={"youtube": True, "google": True},
        service_profile_bindings={"openai": ids[0], "claude": ids[0], "google_ai": ids[0], "youtube": ids[1], "google": ids[1]},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--view", choices=("wide", "compact", "scaled", "tiny"), default="wide")
    parser.add_argument("--label", choices=("before", "after", "verify"), default="verify")
    parser.add_argument("--tab", help="Capture one tab by its exact display label")
    parser.add_argument("--focus-maintenance", action="store_true", help="Also capture the Win11 proxy lifecycle notice")
    parser.add_argument("--onscreen", action="store_true", help="Compare screen rendering; requires the preview to own the foreground")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.child:
        import release_check
        os.chdir(WORKSPACE)
        release_check.PYTEST_BASETEMP = release_check.PYTEST_BASETEMP.parent / "visual-audit" / "pytest"
        return int(not release_check.run_command("pytest", [sys.executable, __file__, *sys.argv[1:], "--child"]))
    isolation = os.environ.get("API_SWITCHER_RELEASE_TEST_ROOT")
    if not isolation:
        parser.error("child requires release_check isolation")
    for name in ("HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "API_SWITCHER_DATA_DIR", "APPDATA"):
        assert Path(isolation).resolve() in Path(os.environ[name]).resolve().parents, name
    assert os.environ.get("PYTHON_KEYRING_BACKEND") == "keyring.backends.null.Keyring"
    os.environ["API_SWITCHER_PRELOAD_TABS"] = "0"
    os.environ["API_SWITCHER_WARM_TABS"] = "0"

    import customtkinter as ctk
    from core import persistent_env, security, startup_manager
    from ui.app import App, TAB_SPECS
    specs = [spec for spec in TAB_SPECS if not args.tab or spec[0] == args.tab]
    if not specs:
        parser.error("unknown tab")

    destination = WORKSPACE / "dist" / "ui-audit-20260921" / args.label
    destination.mkdir(parents=True, exist_ok=True)
    errors, captures, blocked = [], [], []
    secrets = {}

    def forbidden(*_args, **_kwargs):
        blocked.append("blocked external operation")
        raise RuntimeError("visual audit blocks network and persistent system writes")

    def store_secret(key, value):
        secrets[key] = value

    with ExitStack() as guards:
        for name in ("_start_tray_icon", "_auto_start_local_proxy", "_schedule_local_proxy_watchdog", "_restore_subscription_timers"):
            guards.enter_context(patch.object(App, name, lambda _self: None))
        for name in ("get_secret", "get_secret_strict"):
            guards.enter_context(patch.object(security, name, secrets.get))
        guards.enter_context(patch.object(security, "set_secret", store_secret))
        guards.enter_context(patch.object(security, "delete_secret", lambda key: secrets.pop(key, None)))
        for name in ("_local_user_env_value", "_local_user_env_value_strict"):
            guards.enter_context(patch.object(persistent_env, name, lambda _name: None))
        for name in ("set_local_user_env", "delete_local_user_env"):
            guards.enter_context(patch.object(persistent_env, name, forbidden))
        guards.enter_context(patch.object(startup_manager, "get_registered_command", lambda: None))
        guards.enter_context(patch.object(socket.socket, "connect", forbidden))
        guards.enter_context(patch.object(socket.socket, "connect_ex", forbidden))
        if os.name == "nt":
            import winreg
            for name in ("SetValueEx", "DeleteValue", "CreateKey", "CreateKeyEx"):
                guards.enter_context(patch.object(winreg, name, forbidden))
        seed_data()
        ctk.set_default_color_theme("blue")
        ctk.set_appearance_mode("dark")
        if args.view in {"scaled", "tiny"}:
            ctk.set_widget_scaling(1.5)
        root = App()
        root.title("API 配置切换器 · 隔离界面检查（合成数据）")
        root.geometry({"wide": "1120x800+30+30", "compact": "740x720+30+30", "scaled": "960x760+30+30", "tiny": "480x600+30+30"}[args.view])
        def callback_error(kind, error, _tb):
            message = f"{kind.__name__}: {error}"
            errors.append(message)
            print("VISUAL_CALLBACK_ERROR", message, flush=True)
            root.after(0, finish)

        root.report_callback_exception = callback_error
        def capture(label, tab, suffix="top"):
            root.lift()
            path = destination / f"{args.view}-{label}-{suffix}.png"
            capture_window_image(root, onscreen=args.onscreen).save(path)
            left, right = root.winfo_rootx(), root.winfo_rootx() + root.winfo_width()
            top, bottom = root.winfo_rooty(), root.winfo_rooty() + root.winfo_height()
            overflow, pending = [], [root]
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                if not isinstance(widget, (ctk.CTkButton, ctk.CTkCheckBox, ctk.CTkComboBox, ctk.CTkEntry)):
                    continue
                if not widget.winfo_viewable():
                    continue
                x, y, width, height = widget.winfo_rootx(), widget.winfo_rooty(), widget.winfo_width(), widget.winfo_height()
                if y + height > top and y < bottom and (x < left - 2 or x + width > right + 2):
                    try:
                        text = widget.cget("text")
                    except Exception:
                        text = type(widget).__name__
                    overflow.append({"text": text, "x": x - left, "width": width})
            captures.append({"file": path.name, "tab": label, "size": [root.winfo_width(), root.winfo_height()], "horizontal_overflow": overflow})
            print(json.dumps(captures[-1], ensure_ascii=False), flush=True)

        def finish():
            root._exit_requested = True
            report = {"view": args.view, "captures": captures, "callback_errors": errors, "blocked_operations": blocked,
                      "widget_scaling": root._shell._get_widget_scaling(), "window_scaling": root._get_window_scaling(),
                      "capture_mode": "foreground-client" if args.onscreen else "native-window"}
            (destination / f"{args.view}-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("VISUAL_ERRORS", errors, "BLOCKED_OPERATIONS", len(blocked), flush=True)
            root.destroy()

        def show(index=0):
            if index >= len(specs):
                finish()
                return
            label, attr, *_ = specs[index]
            root._select_tab(label)
            started = time.monotonic()

            def wait_ready():
                tab = getattr(root, attr)
                state = vars(tab) if tab is not None else {}
                if label == "环境变量" and tab is not None:
                    tab._build_local_env_control()
                    tab._build_remote_env_control()
                pending = tab is None or bool(state.get("_refresh_inflight") or state.get("_refresh_in_progress"))
                if label == "Win11 代理":
                    pending = pending or not state.get("_routing_preferences_snapshot") or not state.get("_subscription_nodes")
                    if args.focus_maintenance and tab is not None and not state.get("_policy_expanded"):
                        tab._toggle_policy()
                if label == "SSH 服务器":
                    if tab is not None and not state.get("_deployment_sections_built"):
                        tab._build_deployment_sections()
                    pending = pending or not state.get("_remote_auto_section_built") or not state.get("_proxy_subscription_nodes")
                if tab is not None and hasattr(tab, "_card_render_pending"):
                    pending = pending or tab._card_render_pending()
                if time.monotonic() - started < 2.8 or (pending and time.monotonic() - started < 25):
                    root.after(80, wait_ready)
                    return
                if pending:
                    errors.append(f"{label}: load timeout")
                capture(label, tab)
                if args.focus_maintenance and label == "Win11 代理":
                    canvas = tab._parent_canvas
                    bounds = canvas.bbox("all")
                    offset = (tab._maintenance_notice.winfo_rooty() - tab.winfo_rooty()
                              - int(canvas.winfo_height() * 0.6))
                    canvas.yview_moveto(max(0, offset) / max(1, bounds[3] - bounds[1]))
                    root.after(700, lambda: capture_maintenance(tab))
                    return
                if hasattr(tab, "_parent_canvas"):
                    tab._parent_canvas.yview_moveto(0.5 if label in {"SSH 服务器", "Win11 代理"} else 1)
                    root.after(700, lambda: capture_scrolled(tab))
                else:
                    root.after(80, lambda: show(index + 1))

            def capture_maintenance(tab):
                notice, canvas = tab._maintenance_notice, tab._parent_canvas
                if (not notice.winfo_viewable() or notice.winfo_rooty() < canvas.winfo_rooty()
                        or notice.winfo_rooty() + notice.winfo_height() > canvas.winfo_rooty() + canvas.winfo_height()):
                    errors.append("maintenance notice was not fully visible")
                capture(label, tab, "maintenance")
                root.after(80, lambda: show(index + 1))

            def capture_scrolled(tab, bottom=False):
                if label in {"SSH 服务器", "Win11 代理"} and not bottom:
                    capture(label, tab, "middle")
                    tab._parent_canvas.yview_moveto(1)
                    root.after(700, lambda: capture_scrolled(tab, True))
                    return
                capture(label, tab, "bottom")
                tab._parent_canvas.yview_moveto(0)
                root.after(80, lambda: show(index + 1))

            root.after(100, wait_ready)

        root.after(200, show)
        root.after(180000, lambda: (errors.append("audit timeout"), finish()))
        root.mainloop()
    return int(bool(errors or blocked))


if __name__ == "__main__":
    raise SystemExit(main())
