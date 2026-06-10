"""Measure API Switcher startup and concurrency-sensitive hotspots."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import statistics
import sys
import sysconfig
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable


DEFAULT_IMPORT_MODULES = (
    "core.profile_manager",
    "core.remote_proxy",
    "core.network_diagnostics",
    "core.local_proxy",
    "core.ssh_manager",
    "core.auto_continue.manager",
    "ui.theme",
    "ui.widgets.proxy_node_picker",
    "ui.widgets.proxy_quality_panel",
    "ui.app",
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def interpreter_info() -> dict[str, Any]:
    gil_status = None
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    if callable(is_gil_enabled):
        try:
            gil_status = bool(is_gil_enabled())
        except Exception:
            gil_status = None

    py_gil_disabled = sysconfig.get_config_var("Py_GIL_DISABLED")
    free_threaded_build = py_gil_disabled == 1
    return {
        "executable": sys.executable,
        "version": sys.version.replace("\n", " "),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "py_gil_disabled": py_gil_disabled,
        "free_threaded_build": free_threaded_build,
        "gil_enabled": gil_status,
        "cwd": str(Path.cwd()),
    }


def measure_imports(module_names: tuple[str, ...] = DEFAULT_IMPORT_MODULES) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name in module_names:
        started = time.perf_counter()
        status = "ok"
        error = ""
        try:
            importlib.import_module(name)
        except Exception as exc:
            status = "error"
            error = str(exc).splitlines()[0][:200]
        results.append(
            {
                "module": name,
                "status": status,
                "duration_ms": _duration_ms(started),
                "error": error,
            }
        )
    return results


def _cpu_work(size: int) -> int:
    total = 0
    for index in range(size):
        total = (total + ((index * index + 17) % 1_000_003)) % 1_000_000_007
    return total


def _io_work(delay_seconds: float) -> float:
    time.sleep(delay_seconds)
    return delay_seconds


def _run_sequential(worker: Callable[[Any], Any], items: list[Any]) -> float:
    started = time.perf_counter()
    for item in items:
        worker(item)
    return _duration_ms(started)


def _run_threads(worker: Callable[[Any], Any], items: list[Any], workers: int) -> float:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        list(executor.map(worker, items))
    return _duration_ms(started)


def _speedup(sequential_ms: float, threaded_ms: float) -> float | None:
    if threaded_ms <= 0:
        return None
    return round(sequential_ms / threaded_ms, 3)


def measure_thread_benchmarks(
    *,
    workers: int,
    io_tasks: int,
    io_delay: float,
    cpu_tasks: int,
    cpu_size: int,
) -> dict[str, Any]:
    io_items = [float(io_delay)] * max(0, io_tasks)
    cpu_items = [int(cpu_size)] * max(0, cpu_tasks)

    io_sequential = _run_sequential(_io_work, io_items) if io_items else 0.0
    io_threaded = _run_threads(_io_work, io_items, workers) if io_items else 0.0
    cpu_sequential = _run_sequential(_cpu_work, cpu_items) if cpu_items else 0.0
    cpu_threaded = _run_threads(_cpu_work, cpu_items, workers) if cpu_items else 0.0

    return {
        "workers": max(1, workers),
        "io": {
            "tasks": len(io_items),
            "delay_seconds": io_delay,
            "sequential_ms": io_sequential,
            "threaded_ms": io_threaded,
            "speedup": _speedup(io_sequential, io_threaded),
        },
        "cpu": {
            "tasks": len(cpu_items),
            "size": cpu_size,
            "sequential_ms": cpu_sequential,
            "threaded_ms": cpu_threaded,
            "speedup": _speedup(cpu_sequential, cpu_threaded),
        },
    }


def measure_ui_tabs(tab_labels: tuple[str, ...] = ()) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        import customtkinter as ctk
        from ui import app as app_module
    except Exception as exc:
        return {
            "enabled": True,
            "status": "error",
            "duration_ms": _duration_ms(started),
            "error": f"failed to import UI dependencies: {str(exc).splitlines()[0][:200]}",
            "tabs": [],
        }

    specs = {
        label: (module_name, class_name)
        for label, _attr, module_name, class_name, _eager in app_module.TAB_SPECS
    }
    selected = tab_labels or tuple(specs)
    results: list[dict[str, Any]] = []
    root = None
    try:
        root = ctk.CTk()
        root.withdraw()
        root.geometry("1120x760")
        host = ctk.CTkFrame(root)
        host.pack(fill="both", expand=True)

        for label in selected:
            if label not in specs:
                results.append(
                    {
                        "label": label,
                        "status": "missing",
                        "duration_ms": 0.0,
                        "error": "unknown tab label",
                    }
                )
                continue
            module_name, class_name = specs[label]
            frame = ctk.CTkFrame(host)
            frame.pack(fill="both", expand=True)
            tab_started = time.perf_counter()
            status = "ok"
            error = ""
            try:
                module = importlib.import_module(module_name)
                tab_class = getattr(module, class_name)
                widget = tab_class(frame)
                widget.pack(fill="both", expand=True)
                root.update_idletasks()
                widget.destroy()
            except Exception as exc:
                status = "error"
                error = str(exc).splitlines()[0][:200]
            finally:
                try:
                    frame.destroy()
                except Exception:
                    pass
            results.append(
                {
                    "label": label,
                    "status": status,
                    "duration_ms": _duration_ms(tab_started),
                    "error": error,
                }
            )
    except Exception as exc:
        return {
            "enabled": True,
            "status": "error",
            "duration_ms": _duration_ms(started),
            "error": str(exc).splitlines()[0][:200],
            "tabs": results,
        }
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    durations = [item["duration_ms"] for item in results if item.get("status") == "ok"]
    summary = {
        "count": len(results),
        "ok": sum(1 for item in results if item.get("status") == "ok"),
        "total_ms": round(sum(durations), 3),
        "median_ms": round(statistics.median(durations), 3) if durations else 0.0,
        "max_ms": round(max(durations), 3) if durations else 0.0,
    }
    return {
        "enabled": True,
        "status": "ok",
        "duration_ms": _duration_ms(started),
        "summary": summary,
        "tabs": results,
    }


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    report: dict[str, Any] = {
        "schema_version": 1,
        "interpreter": interpreter_info(),
    }
    if not args.skip_imports:
        modules = tuple(args.imports or DEFAULT_IMPORT_MODULES)
        report["imports"] = measure_imports(modules)
    if not args.skip_benchmarks:
        report["benchmarks"] = measure_thread_benchmarks(
            workers=args.workers,
            io_tasks=args.io_tasks,
            io_delay=args.io_delay,
            cpu_tasks=args.cpu_tasks,
            cpu_size=args.cpu_size,
        )
    if args.ui:
        report["ui"] = measure_ui_tabs(tuple(args.tabs or ()))
    else:
        report["ui"] = {"enabled": False}
    report["total_ms"] = _duration_ms(started)
    return report


def format_report(report: dict[str, Any]) -> str:
    lines = []
    interpreter = report.get("interpreter", {})
    lines.append("API Switcher performance probe")
    lines.append("=" * 38)
    lines.append(f"Python: {interpreter.get('version', '')}")
    lines.append(f"Executable: {interpreter.get('executable', '')}")
    lines.append(
        "Free-threaded build: "
        f"{interpreter.get('free_threaded_build')} "
        f"(Py_GIL_DISABLED={interpreter.get('py_gil_disabled')}, "
        f"GIL enabled={interpreter.get('gil_enabled')})"
    )
    lines.append(f"CPU count: {interpreter.get('cpu_count')}")

    imports = report.get("imports")
    if imports:
        lines.append("")
        lines.append("Import timings:")
        for item in imports:
            suffix = f" - {item['error']}" if item.get("error") else ""
            lines.append(f"  {item['duration_ms']:>8.3f} ms  {item['status']:<7} {item['module']}{suffix}")

    benchmarks = report.get("benchmarks")
    if benchmarks:
        lines.append("")
        lines.append(f"Thread pool benchmarks (workers={benchmarks.get('workers')}):")
        for key in ("io", "cpu"):
            item = benchmarks.get(key, {})
            lines.append(
                f"  {key.upper():<3} sequential={item.get('sequential_ms', 0):.3f} ms  "
                f"threaded={item.get('threaded_ms', 0):.3f} ms  speedup={item.get('speedup')}"
            )

    ui = report.get("ui")
    if ui and ui.get("enabled"):
        lines.append("")
        lines.append(f"UI tabs: {ui.get('status')} ({ui.get('duration_ms')} ms)")
        if ui.get("error"):
            lines.append(f"  error: {ui['error']}")
        for item in ui.get("tabs", []):
            suffix = f" - {item['error']}" if item.get("error") else ""
            lines.append(f"  {item['duration_ms']:>8.3f} ms  {item['status']:<7} {item['label']}{suffix}")

    lines.append("")
    lines.append(f"Total: {report.get('total_ms')} ms")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure API Switcher performance hotspots.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--ui", action="store_true", help="Construct UI tabs and measure first-paint costs.")
    parser.add_argument("--tabs", nargs="*", help="Optional UI tab labels to measure when --ui is set.")
    parser.add_argument("--skip-imports", action="store_true", help="Skip project import timing.")
    parser.add_argument("--skip-benchmarks", action="store_true", help="Skip thread pool benchmarks.")
    parser.add_argument("--imports", nargs="*", help="Override modules used by import timing.")
    parser.add_argument("--workers", type=int, default=min(8, max(1, os.cpu_count() or 1)))
    parser.add_argument("--io-tasks", type=int, default=32)
    parser.add_argument("--io-delay", type=float, default=0.03)
    parser.add_argument("--cpu-tasks", type=int, default=min(8, max(1, os.cpu_count() or 1)))
    parser.add_argument("--cpu-size", type=int, default=120_000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_probe(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        print(format_report(report), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
