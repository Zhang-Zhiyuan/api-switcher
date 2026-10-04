"""UI-thread lifecycle for saved-only Win11/SSH subscription timers."""
from __future__ import annotations

import queue
import threading
import math

from core import subscription_auto_refresh
from core.lazy_imports import LazyModule

remote_proxy = LazyModule("core.remote_proxy")


def normalized_refresh_delay_seconds(value):
    """Accept bounded positive timer hints, never NaN/inf or Boolean flags."""
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return min(86400.0, max(1.0, seconds))


def periodic_refresh_delay_ms(default_seconds, delay_seconds=None):
    hint = normalized_refresh_delay_seconds(delay_seconds)
    return round((default_seconds if hint is None else hint) * 1000)


def _saved_interval_seconds(value):
    try:
        minutes = min(max(int(value), 5), 1440) if not isinstance(value, bool) else 60
    except (TypeError, ValueError, OverflowError):
        minutes = 60
    return minutes * 60


def _feedback_messages(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple)):
        raise ValueError("订阅刷新反馈格式无效")
    return [str(item) for item in value if item]


def _feedback_count(value):
    try:
        return max(0, int(value)) if not isinstance(value, bool) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _progress_message(event, label):
    """Render fixed progress fields only, never worker-supplied URLs or text."""
    if not isinstance(event, dict):
        return ""
    completed, total, index = (event.get(key) for key in ("completed", "total", "index"))
    if not all(type(value) is int and 0 <= value <= 100000 for value in (completed, total, index)):
        return ""
    if completed > total or index > total:
        return ""
    if event.get("stage") == "apply":
        return f"{label} 定时刷新：下载处理完成，正在逐项核对并应用线路 {completed}/{total}。"
    if event.get("stage") != "download":
        return ""
    outcome = event.get("outcome")
    detail = ({"downloaded": "下载完成", "cached": "复用缓存", "waiting": "等待下次刷新",
               "failed": "本次未更新，保留已有缓存"}.get(outcome)
              if isinstance(outcome, str) else None)
    suffix = f"；订阅 {index} {detail}" if index and detail else ""
    return f"{label} 定时刷新：已处理订阅 {completed}/{total}（最多 2 路同时下载）{suffix}。"


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
    # Snapshot only the confirmed interval; neither the worker nor completion
    # reads or submits whatever the user is currently typing in the entry.
    interval_seconds = _saved_interval_seconds(getattr(tab, prefix + "periodic_update_interval_saved", 60))
    if not remote_proxy.try_acquire_proxy_subscription_hot_update():
        status("另一项订阅刷新正在进行，定时任务将在 1 分钟后重试。", "info")
        schedule(retry=True)
        return
    channel = queue.SimpleQueue()
    progress_channel = queue.SimpleQueue()
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
        retry = True
        schedule_options = {}
        try:
            set_busy(False)
            if not isinstance(payload, dict):
                raise ValueError("订阅刷新结果格式无效")
            errors = _feedback_messages(payload.get("errors"))
            results = payload.get("results") or {}
            if not isinstance(results, dict):
                raise ValueError("订阅刷新缓存结果格式无效")
            steps = payload.get("steps")
            structured = isinstance(steps, list) and all(isinstance(step, dict) for step in steps)
            if steps is not None and not structured:
                raise ValueError("订阅刷新步骤格式无效")
            retryable = payload.get("retryable")
            retry = retryable if isinstance(retryable, bool) else bool(
                errors or (structured and any(step.get("retryable") for step in steps))
            )
            hint = normalized_refresh_delay_seconds(payload.get("next_delay_seconds"))
            current_interval = _saved_interval_seconds(getattr(tab, prefix + "periodic_update_interval_saved", 60))
            if hint is not None and current_interval == interval_seconds:
                schedule_options["delay_seconds"] = hint
            result_sources = payload.get("result_sources")
            has_source_identity = "result_sources" in payload
            if has_source_identity and not isinstance(result_sources, dict):
                raise ValueError("订阅刷新来源身份格式无效")
            if results:
                refresh = tab._refresh_subscription_profile_options if scope == "local" else tab._refresh_proxy_subscription_profile_options
                refresh(preserve_editor=True)
                if scope == "local":
                    tab._request_route_catalog_refresh()
                if has_source_identity:
                    # A saved edit can occur after the worker queues completion.
                    # Validate at the point of UI consumption, retaining healthy
                    # siblings rather than discarding the entire refresh batch.
                    saved = remote_proxy.load_proxy_subscription_state().get("profiles") or {}
                    valid = {
                        profile_id: result for profile_id, result in results.items()
                        if isinstance(saved.get(profile_id), dict)
                        and result_sources.get(profile_id) == subscription_auto_refresh.subscription_source_identity(
                            profile_id, saved[profile_id],
                        )
                    }
                    if len(valid) != len(results):
                        errors.append("订阅来源已变化，未用旧链接节点覆盖当前列表")
                        retry = True
                        schedule_options.clear()
                    results = valid
                dirty = tab._subscription_profile_blocks_automatic_refresh if scope == "local" else tab._proxy_subscription_profile_blocks_automatic_refresh
                current_id = tab._current_subscription_profile_id() if scope == "local" else tab._current_proxy_subscription_profile_id()
                if current_id in results and not dirty():
                    selected = tab._selected_subscription_node_key() if scope == "local" else tab._selected_proxy_subscription_node_key()
                    if scope == "local":
                        tab._set_subscription_nodes(results[current_id].nodes, preserve_key=selected)
                    else:
                        tab._set_proxy_subscription_nodes(results[current_id].nodes, preserve_key=selected)
            apply_messages = _feedback_messages(payload.get("apply_messages"))
            warnings = [str(step.get("message") or "") for step in (steps or ()) if step.get("warning")]
            details = list(dict.fromkeys(message for message in [*errors, *warnings, *apply_messages] if message))
            if structured:
                message = (
                    f"{label} 定时刷新：下载 {_feedback_count(payload.get('downloaded_count'))} 个，"
                    f"复用缓存 {_feedback_count(payload.get('reused_count'))} 个，"
                    f"等待 {_feedback_count(payload.get('waiting_count'))} 个"
                )
            else:
                message = f"{label} 定时刷新：已处理 {len(results)} 个已保存订阅"
            if details:
                message += "；" + "；".join(details)
            elif not structured and not results:
                message += "；本轮没有更新订阅缓存"
            # Old workers did not expose an apply verdict. Show their text
            # conservatively without guessing success from Chinese keywords.
            warning = bool(errors) or (any(step.get("warning") for step in steps) if structured else bool(apply_messages))
            status(message, "warning" if warning else "info")
        except Exception as exc:
            retry = True
            schedule_options.clear()
            status(f"{label} 定时刷新界面同步失败，任务会继续重试：{exc}", "warning")
        finally:
            schedule(retry=retry, **schedule_options)

    def poll():
        setattr(tab, poll_attr, None)
        if completed or getattr(tab, "_destroyed", False):
            return
        try:
            payload = channel.get_nowait()
        except queue.Empty:
            # Drain/coalesce bounded progress on Tk's thread. A slow sibling
            # cannot hide completed items; progress never replaces node lists
            # or releases the reservation before the final source validation.
            latest_progress = ""
            for _ in range(64):
                try:
                    event = progress_channel.get_nowait()
                except queue.Empty:
                    break
                latest_progress = _progress_message(event, label) or latest_progress
            if latest_progress:
                try:
                    status(latest_progress, "info")
                except Exception:
                    pass  # Advisory feedback must not strand the worker lock.
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
            payload = subscription_auto_refresh.refresh_saved_subscriptions(
                scope, server_names=names, interval_seconds=interval_seconds,
                on_progress=progress_channel.put,
            )
        except Exception as exc:
            payload = {"errors": [str(exc)], "results": {}}
        finally:
            try:
                release()
            except Exception as exc:
                if not isinstance(payload, dict):
                    payload = {"results": {}, "errors": []}
                try:
                    errors = _feedback_messages(payload.get("errors"))
                except ValueError:
                    errors = ["订阅刷新错误反馈格式无效"]
                payload["errors"] = [*errors, f"订阅刷新锁释放失败：{exc}"]
                payload["retryable"] = True
                payload.pop("next_delay_seconds", None)
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
