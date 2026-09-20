"""UI-thread lifecycle for saved-only Win11/SSH subscription timers."""
from __future__ import annotations

import queue
import threading

from core import subscription_auto_refresh
from core.lazy_imports import LazyModule

remote_proxy = LazyModule("core.remote_proxy")


def start_saved_refresh(tab, *, scope: str, thread_factory):
    prefix = "_" if scope == "local" else "_proxy_"
    running_attr = prefix + "periodic_update_running"
    poll_attr = prefix + "periodic_update_poll_after_id"
    busy_attr = "_busy" if scope == "local" else "_proxy_busy"
    set_busy = tab._set_busy if scope == "local" else tab._set_proxy_busy
    status = tab._set_status if scope == "local" else tab._set_proxy_status
    schedule = tab._schedule_periodic_update if scope == "local" else tab._schedule_proxy_periodic_update
    cancel = tab._cancel_periodic_update if scope == "local" else tab._cancel_proxy_periodic_update
    label = "Win11" if scope == "local" else "SSH"
    if getattr(tab, "_destroyed", False):
        return
    if getattr(tab, running_attr, False) or getattr(tab, busy_attr, False) or (scope == "ssh" and getattr(tab, "_ssh_busy", False)):
        schedule(retry=True)
        return
    names = () if scope == "local" else subscription_auto_refresh.saved_server_names(getattr(tab, "_proxy_periodic_update_servers", ()))
    if scope == "ssh" and not names:
        status("SSH 定时刷新未保存目标；请勾选服务器后重新开启定时热更新。", "warning")
        schedule()
        return
    if not remote_proxy.try_acquire_proxy_subscription_hot_update():
        status("另一项订阅刷新正在进行，定时任务将在 1 分钟后重试。", "info")
        schedule(retry=True)
        return
    channel = queue.SimpleQueue()
    lock_owned = True
    release_guard = threading.Lock()
    start_guard = threading.Lock()
    worker_started = False
    start_failed = False
    completed = False

    def release():
        nonlocal lock_owned
        with release_guard:
            if lock_owned:
                lock_owned = False
                remote_proxy.release_proxy_subscription_hot_update()

    def finish(payload):
        nonlocal completed
        if completed:
            return
        completed = True
        setattr(tab, poll_attr, None)
        setattr(tab, running_attr, False)
        if getattr(tab, "_destroyed", False):
            return
        errors = list(payload.get("errors") or ())
        results = payload.get("results") or {}
        try:
            set_busy(False)
            if results:
                refresh = tab._refresh_subscription_profile_options if scope == "local" else tab._refresh_proxy_subscription_profile_options
                refresh(preserve_editor=True)
                if scope == "local":
                    tab._request_route_catalog_refresh()
                dirty = tab._subscription_profile_blocks_automatic_refresh if scope == "local" else tab._proxy_subscription_profile_blocks_automatic_refresh
                current_id = tab._current_subscription_profile_id() if scope == "local" else tab._current_proxy_subscription_profile_id()
                if current_id in results and not dirty():
                    selected = tab._selected_subscription_node_key() if scope == "local" else tab._selected_proxy_subscription_node_key()
                    if scope == "local":
                        tab._set_subscription_nodes(results[current_id].nodes, preserve_key=selected)
                    else:
                        tab._set_proxy_subscription_nodes(results[current_id].nodes, preserve_key=selected)
            details = [*errors, *(payload.get("apply_messages") or ())]
            message = f"{label} 定时刷新：已更新 {len(results)} 个已保存订阅"
            if details:
                message += "；" + "；".join(details)
            elif not results:
                message += "；当前没有可下载的订阅链接"
            incomplete = any(any(marker in detail for marker in ("失败", "未运行", "跳过", "无法", "未执行")) for detail in details)
            status(message, "warning" if errors or incomplete else "info")
        except Exception as exc:
            errors.append(str(exc))
            status(f"{label} 定时刷新界面同步失败，任务会继续重试：{exc}", "warning")
        finally:
            schedule(retry=bool(errors))

    def poll():
        setattr(tab, poll_attr, None)
        if completed or getattr(tab, "_destroyed", False):
            return
        try:
            payload = channel.get_nowait()
        except queue.Empty:
            try:
                setattr(tab, poll_attr, tab.after(150, poll))
            except Exception:
                if getattr(tab, "_destroyed", False):
                    return
                try:
                    # Tk can reject a timed callback during a transient widget
                    # transition; keep completion on the main thread.
                    setattr(tab, poll_attr, tab.after_idle(poll))
                except Exception as exc:
                    setattr(tab, running_attr, False)
                    set_busy(False)
                    status(f"{label} 刷新结果调度失败：{exc}；后台任务会自行释放订阅锁。", "warning")
                    # Do not release a worker-owned reservation, even if the
                    # destroyed UI can no longer receive its completion.
                    try:
                        schedule(retry=True)
                    except Exception:
                        pass
        else:
            finish(payload)

    def run():
        nonlocal worker_started
        with start_guard:
            if start_failed:
                return
            worker_started = True
        try:
            payload = subscription_auto_refresh.refresh_saved_subscriptions(scope, server_names=names)
        except Exception as exc:
            payload = {"errors": [str(exc)], "results": {}}
        finally:
            try:
                release()
            except Exception as exc:
                payload.setdefault("errors", []).append(f"订阅刷新锁释放失败：{exc}")
        channel.put(payload)

    try:
        cancel()
        setattr(tab, running_attr, True)
        set_busy(True)
        status(f"正在定时刷新 {label} 已保存订阅及已绑定线路；未保存草稿不会提交。", "info")
        thread_factory(target=run, name=f"{scope}-saved-subscriptions-refresh", daemon=True).start()
    except Exception as exc:
        with start_guard:
            started = worker_started
            if not started:
                start_failed = True
        if not started:
            release()
            finish({"errors": [f"后台任务启动失败：{exc}"], "results": {}})
            return
        # A Thread implementation can raise after starting its worker. That
        # worker still owns the reservation and the sole completion event.
    poll()
