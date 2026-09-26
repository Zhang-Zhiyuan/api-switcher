"""Synthetic native-mainloop benchmarks; never opens live login/proxy data.

Run from the checkout: python tools/ui_performance_probe.py --scenario profiles
Results/screenshots go under build/. The parent uses release_check's isolated
HOME, app data, proxy-free environment and null keyring for the child process.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import sys
import time
from types import SimpleNamespace


WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("profiles", "nodes", "routes"), default="profiles")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--width", type=int, default=1040, help="Window width in logical pixels")
    parser.add_argument("--long-names", action="store_true", help="Exercise wrapping with long synthetic node names")
    parser.add_argument("--label", choices=("before", "after", "check"), default="check")
    parser.add_argument("--screenshot", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.count <= 1000:
        parser.error("count must be between 1 and 1000")
    if not 320 <= args.width <= 2000:
        parser.error("width must be between 320 and 2000")
    if not args.child:
        import release_check

        os.chdir(WORKSPACE)
        release_check.PYTEST_BASETEMP = release_check.PYTEST_BASETEMP.parent / "native-perf" / "pytest"
        return 0 if release_check.run_command("pytest", [sys.executable, __file__, *sys.argv[1:], "--child"]) else 1
    if not os.environ.get("API_SWITCHER_RELEASE_TEST_ROOT"):
        parser.error("child requires release_check isolation")

    import tkinter
    import customtkinter as ctk
    from ui import theme

    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.title("API Switcher — 隔离性能实测（合成数据）")
    root.geometry(f"{args.width}x760")
    root.configure(fg_color=theme.COLORS["app_bg"])
    errors, results, messages = [], [], queue.Queue()
    root._ui_dispatch = lambda callback: messages.put(callback)
    root.report_callback_exception = lambda _type, error, _tb: errors.append(str(error))
    monitor = {"active": False, "last": time.perf_counter(), "gaps": [], "calls": defaultdict(lambda: [0, 0.0, 0.0])}
    original_call = tkinter.CallWrapper.__call__

    def call(self, *values):
        started = time.perf_counter()
        try:
            return original_call(self, *values)
        finally:
            if monitor["active"]:
                callback = self.func
                # Tk wraps after callbacks; unwrap for useful attribution.
                cells = dict(zip(getattr(callback, "__code__", SimpleNamespace(co_freevars=())).co_freevars,
                                 getattr(callback, "__closure__", ()) or ()))
                if "func" in cells:
                    callback = cells["func"].cell_contents
                name = getattr(callback, "__qualname__", type(callback).__name__)
                entry = monitor["calls"][name]
                elapsed = (time.perf_counter() - started) * 1000
                entry[0] += 1
                entry[1] += elapsed
                entry[2] = max(entry[2], elapsed)

    tkinter.CallWrapper.__call__ = call

    def tick():
        now = time.perf_counter()
        if monitor["active"]:
            monitor["gaps"].append((now - monitor["last"]) * 1000)
        monitor["last"] = now
        for _ in range(4):
            try:
                messages.get_nowait()()
            except queue.Empty:
                break
        root.after(16, tick)

    target = {}
    phases = []
    if args.scenario == "profiles":
        from models.profile import CodexAccountProfile, CodexProfile
        from ui.tabs import codex_tab

        data = {
            "profiles": [CodexProfile(f"API {i:03}", custom_base_url="https://example.invalid/v1") for i in range(args.count)],
            "accounts": [CodexAccountProfile(f"Account {i:03}", "synthetic-ref") for i in range(args.count)],
        }
        codex_tab.profile_manager = SimpleNamespace(
            list_switchable_codex_profiles=lambda: deepcopy(data["profiles"]),
            list_codex_account_profiles=lambda: deepcopy(data["accounts"]),
            get_codex_runtime_summary=lambda: {}, get_codex_account_runtime_summary=lambda: {},
            describe_codex_profile_identity=lambda _: "synthetic", validate_codex_account_snapshot=lambda _: (True, "synthetic"),
        )

        def opening():
            tab = target["widget"] = codex_tab.CodexTab(root)
            tab.pack(fill="both", expand=True)
            tab.refresh()

        def ready():
            tab = target["widget"]
            return not tab._refresh_inflight and not tab._card_render_pending()

        def refresh():
            target["widget"].refresh()

        def change():
            data["profiles"][0].model = "synthetic-new-model"
            refresh()

        def burst():
            for _ in range(30):
                refresh()

        def switch_tabs():
            tab = target["widget"]
            tab._suspend_background_work()
            tab.pack_forget()
            tab.pack(fill="both", expand=True)
            tab._resume_background_work()

        phases = [("open", opening, ready), ("unchanged_refresh", refresh, ready),
                  ("single_edit", change, ready), ("refresh_burst", burst, ready),
                  ("tab_return", switch_tabs, ready)]
    elif args.scenario == "nodes":
        from core.remote_proxy import ProxySubscriptionNode
        from ui.widgets.proxy_node_picker import ProxyNodePicker

        nodes = [ProxySubscriptionNode(i, {"name": f"美国-{i:04}", "type": "http", "server": "example.invalid", "port": 1000 + i})
                 for i in range(args.count)]
        if args.long_names:
            for item in nodes:
                item.node["name"] += " · 合成测试长节点名称" * 5
                item.node["server"] = "long-synthetic-host.example.invalid"

        def opening():
            picker = target["widget"] = ProxyNodePicker(root)
            picker.pack(fill="both", expand=True)
            picker.set_nodes(nodes)

        def ready():
            picker = target["widget"]
            return not (picker._render_after_id or picker._render_batch_after_id or picker._render_plan_pending
                        or picker.__dict__.get("_checkbox_sync_after_id"))

        def search(value):
            picker = target["widget"]
            picker._search_entry.delete(0, "end")
            picker._search_entry.insert(0, value)
            picker._render_nodes()

        phases = [("open", opening, ready), ("same_nodes", lambda: target["widget"].set_nodes(nodes), ready),
                  ("filter", lambda: search("000"), ready), ("clear_filter", lambda: search(""), ready),
                  ("check_all", lambda: target["widget"]._set_matching_checked(True), ready)]
    else:
        from core import proxy_routing
        from ui.dialogs.service_routes_dialog import ServiceRoutesDialog

        catalog = [{"id": f"p{i}", "name": f"Synthetic subscription {i}",
                    "nodes": [{"key": f"n{j}", "label": f"Node {j}"} for j in range(args.count)]} for i in range(3)]

        def opening():
            target["widget"] = ServiceRoutesDialog(root, scopes=["隔离本机", "隔离 SSH"],
                load_preferences=lambda _: proxy_routing.route_snapshot({}), catalog_loader=lambda: catalog,
                apply_preferences=lambda *_: (_ for _ in ()).throw(AssertionError("benchmark must not apply settings")))

        def ready():
            return not target["widget"]._busy

        phases = [("open", opening, ready), ("switch_scope", lambda: target["widget"]._switch_scope("隔离 SSH"), ready),
                  ("return_scope", lambda: target["widget"]._switch_scope("隔离本机"), ready)]

    def finish():
        monitor["active"] = False
        widget = target.get("widget")
        if args.scenario == "profiles":
            assert len(widget._cards_frame.winfo_children()) == args.count
            assert len(widget._account_cards_frame.winfo_children()) == args.count
        elif args.scenario == "nodes":
            assert len(widget.filtered_items()) == args.count
            assert len(widget.checked_items()) == args.count
        suffix = f"-{args.width}" if args.width != 1040 else ""
        output = WORKSPACE / "build" / f"ui-perf-{args.scenario}-{args.label}{suffix}"
        output.parent.mkdir(exist_ok=True)
        output.with_suffix(".json").write_text(json.dumps({"scenario": args.scenario, "count": args.count,
            "width": args.width, "long_names": args.long_names,
            "phases": results, "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.screenshot:
            import ctypes
            from PIL import ImageGrab
            get_parent = ctypes.windll.user32.GetParent
            get_parent.argtypes = (ctypes.c_void_p,)
            get_parent.restype = ctypes.c_void_p
            window = widget if args.scenario == "routes" else root
            ImageGrab.grab(window=get_parent(window.winfo_id())).save(output.with_suffix(".png"))
        print("CALLBACK_ERRORS", errors, flush=True)
        root.destroy()

    def run_phase(index=0):
        if index >= len(phases):
            finish()
            return
        label, action, ready = phases[index]
        monitor.update(active=True, last=time.perf_counter(), gaps=[], calls=defaultdict(lambda: [0, 0.0, 0.0]))
        start = time.perf_counter()
        action()
        action_ms = (time.perf_counter() - start) * 1000

        def report(ready_ms):
            monitor["active"] = False
            gaps = sorted(monitor["gaps"])
            result = {"phase": label, "action_ms": round(action_ms, 1), "ready_ms": round(ready_ms, 1),
                      "max_gap_ms": round(max(gaps, default=0), 1),
                      "p95_gap_ms": round(gaps[min(len(gaps) - 1, int(len(gaps) * 0.95))], 1) if gaps else 0,
                      "slow_callbacks": sorted(((name, [values[0], round(values[1], 1), round(values[2], 1)])
                                                for name, values in monitor["calls"].items()), key=lambda item: item[1][1], reverse=True)[:8]}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            root.after(200, lambda: run_phase(index + 1))

        def poll():
            if ready():
                ready_ms = (time.perf_counter() - start) * 1000
                root.after(300, lambda: report(ready_ms))
            elif time.perf_counter() - start > 45:
                errors.append(f"{label} timed out")
                finish()
            else:
                root.after(20, poll)

        root.after(20, poll)

    def watchdog():
        errors.append("watchdog timeout")
        root.destroy()

    root.after(90000, watchdog)
    root.after(16, tick)
    root.after(600, run_phase)
    root.mainloop()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
