"""Incremental profile cards. Only presentation data is retained, never auth files."""
from collections import deque
from dataclasses import asdict
import json
import logging

import customtkinter as ctk

from ui.theme import COLORS, font, recent_user_scroll
from ui.widgets.empty_state import EmptyState


logger = logging.getLogger(__name__)


class ProfileCardList:
    """Reuse unchanged cards and perform at most one costly Tk operation per tick."""

    STEP_MS = 16

    def __init__(self, owner, frame, *, is_active, on_error, should_pause=lambda: False):
        self.owner = owner
        self.frame = frame
        self.is_active = is_active
        self.on_error = on_error
        self.should_pause = should_pause
        self.rows = {}
        self._after_id = None
        self._generation = 0
        self._on_complete = None

    @property
    def pending(self):
        return self._after_id is not None

    def cancel(self):
        self._generation += 1
        self._on_complete = None
        if self._after_id is not None:
            self.owner.after_cancel(self._after_id)
            self._after_id = None

    def render(self, items, create_card, create_empty, *, on_complete=None):
        self.cancel()
        self._on_complete = on_complete
        desired = []
        for item in items:
            # Freeze mutable model fields now, so an in-place edit cannot make
            # an old widget appear up to date. Profiles contain secret *refs*,
            # not token values; snapshots here are (valid, display message).
            data = {**item, "profile": asdict(item["profile"])}
            signature = json.dumps(data, ensure_ascii=False, sort_keys=True)
            desired.append((item["profile"].name, signature, item, create_card))
        if not desired:
            desired = [(None, "empty", None, lambda _item: create_empty())]
        self._reconcile(desired)

    def show_message(self, message, create_label):
        self.cancel()
        self._reconcile([(None, message, None, lambda _item: create_label())])

    def _reconcile(self, desired):
        children = list(self.frame.pack_slaves())
        self.rows = {key: row for key, row in self.rows.items() if row[1] in children}
        signatures = {key: signature for key, signature, _item, _create in desired}
        keep = {row[1] for key, row in self.rows.items() if signatures.get(key) == row[0]}
        # Remove bottom-up to avoid repeatedly shifting every following native
        # window. Include unpacked widgets left behind by a failed factory.
        packed = set(children)
        obsolete = ([child for child in reversed(children) if child not in keep]
                    + [child for child in self.frame.winfo_children() if child not in packed])
        operations = deque(("remove", child) for child in obsolete)
        order = [child for child in children if child in keep]
        self.rows = {key: row for key, row in self.rows.items() if row[1] in keep}
        by_widget = {row[1]: key for key, row in self.rows.items()}
        planned = [by_widget[child] for child in order]
        for index, entry in enumerate(desired):
            key, _signature, _item, _create = entry
            if index < len(planned) and planned[index] == key:
                continue
            operations.append(("place", (index, entry)))
            if key in planned:
                planned.remove(key)
            planned.insert(index, key)
        if operations:
            self._schedule(operations, order, self._generation)
        else:
            self._complete()

    def _complete(self):
        callback, self._on_complete = self._on_complete, None
        if callback is not None:
            callback()

    def _schedule(self, operations, order, generation, *, settle=False):
        if settle:
            def after_layout():
                if generation == self._generation:
                    self._schedule(operations, order, generation)
            self._after_id = self.owner.after_idle(after_layout)
            return
        self._after_id = self.owner.after(
            self.STEP_MS, lambda: self._step(operations, order, generation),
        )

    def _step(self, operations, order, generation):
        if generation != self._generation:
            return
        self._after_id = None
        if not self.is_active():
            return  # The owner resumes from the latest payload when visible.
        if self.should_pause():
            self._after_id = self.owner.after(120, lambda: self._step(operations, order, generation))
            return
        try:
            action, data = operations.popleft()
            if action == "remove":
                data.destroy()
            else:
                index, (key, signature, item, create) = data
                row = self.rows.get(key)
                card = row[1] if row else create(item)
                if card in order:
                    order.remove(card)
                pack = {"fill": "x", "pady": 5}
                if index < len(order):
                    pack["before"] = order[index]
                card.pack(**pack)
                order.insert(index, card)
                self.rows[key] = (signature, card)
        except Exception:
            # Never cache a failed/partially constructed card as complete.
            logger.exception("Profile card rendering failed")
            self._on_complete = None
            self.on_error()
            return
        if operations:
            # Drain the previous card's geometry/paint events before queuing
            # more widgets; timer-only batching can outrun Tk's layout queue.
            self._schedule(operations, order, generation, settle=True)
        else:
            self._complete()


class ProfileTabRendering:
    """Shared, cancellable rendering for Claude and Codex account/API lists."""

    def _init_card_rendering(self, client, is_active):
        self._client_label = client
        self._profile_cards_active = is_active
        self._profile_card_lists = tuple(
            ProfileCardList(self, frame, is_active=is_active, on_error=self._card_render_failed,
                            should_pause=lambda: recent_user_scroll(self, idle_ms=140))
            for frame in (self._cards_frame, self._account_cards_frame)
        )

    def _card_render_failed(self):
        self._runtime_label.configure(text="列表显示失败，请刷新重试。", text_color=COLORS["danger"])

    def _cancel_profile_render(self):
        for renderer in self.__dict__.get("_profile_card_lists", ()):
            renderer.cancel()

    def _card_render_pending(self):
        return any(renderer.pending for renderer in self.__dict__.get("_profile_card_lists", ()))

    def _show_refresh_loading(self):
        # Keep completed cards and scroll position while fresh state is read.
        for frame, kind in ((self._cards_frame, "API 配置"), (self._account_cards_frame, "官方账号快照")):
            if not frame.winfo_children():
                ctk.CTkLabel(
                    frame, text=f"正在后台读取 {self._client_label} {kind}...",
                    text_color=COLORS["muted"], font=font(12), anchor="w",
                ).pack(fill="x", pady=5)

    def _show_refresh_error(self, message):
        text = f"读取 {self._client_label} 配置失败: {message}"
        self._runtime_label.configure(text=text, text_color=COLORS["danger"])
        if not self._profile_cards_active():
            self._deferred_render_pending = True
            return
        # A failed read must not leave stale account-validity cards on screen.
        for renderer in self._profile_card_lists:
            renderer.show_message(text, lambda frame=renderer.frame: ctk.CTkLabel(
                frame, text=text, text_color=COLORS["danger"], font=font(12), anchor="w", justify="left",
            ))

    def _render_profile_cards_batch(self, profiles, generation, *, on_complete=None):
        if generation != self._refresh_generation:
            return
        names = {item["profile"].name for item in profiles}
        self._profile_test_buttons = {name: button for name, button in self._profile_test_buttons.items() if name in names}
        self._profile_card_lists[0].render(profiles, self._create_profile_card, lambda: EmptyState(
            self._cards_frame, f"暂无 {self._client_label} API 配置",
            "新建第三方 API 配置，或从当前设置中导入。", "新建 API 配置", self._create_profile,
        ), on_complete=on_complete)

    def _render_account_cards_batch(self, accounts, generation):
        if generation != self._refresh_generation:
            return
        self._profile_card_lists[1].render(accounts, self._create_account_card, lambda: EmptyState(
            self._account_cards_frame, f"暂无 {self._client_label} 官方账号",
            f"先在 {self._client_label} 登录，再导入当前账号快照。", "导入当前账号", self._import_current_account,
        ))
