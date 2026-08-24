import customtkinter as ctk

from ui.feedback import (
    feedback_duration_ms,
    feedback_title,
    resolve_feedback_severity,
    safe_feedback_text,
)
from ui.theme import COLORS, _screen_bounds, _window_scaling, font


_MAX_VISIBLE_TOASTS = 3


def _toast_wraplength(screen_width: int, scaling: float = 1.0) -> int:
    """Return a logical text width that leaves room for padding and screen edges."""

    try:
        scale = max(float(scaling), 0.1)
    except (TypeError, ValueError):
        scale = 1.0
    logical_screen_width = max(1, round(max(1, int(screen_width)) / scale))
    return max(1, min(360, logical_screen_width - 64))


class Toast(ctk.CTkToplevel):
    """A readable, stackable notification popup that auto-dismisses."""

    def __init__(
        self,
        master,
        message: str,
        duration: int | None = None,
        is_error: bool = False,
        severity: str | None = None,
        title: str | None = None,
    ):
        super().__init__(master)
        self._dismiss_after_id = None
        self._toast_master = master
        self._message = safe_feedback_text(message).strip() or "操作已完成"
        self._severity = resolve_feedback_severity(
            self._message,
            severity=severity,
            is_error=is_error,
        )
        self._duration = (
            max(800, int(duration))
            if duration is not None
            else feedback_duration_ms(self._message, self._severity)
        )
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        accent = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
            "busy": COLORS["accent"],
            "info": COLORS["primary"],
        }.get(self._severity, COLORS["primary"])
        self.configure(fg_color=COLORS["surface"])
        _screen_x, _screen_y, screen_width, _screen_height = _screen_bounds(master)
        wraplength = _toast_wraplength(screen_width, _window_scaling(self))

        body = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=accent,
            corner_radius=8,
        )
        body.pack()

        heading = ctk.CTkFrame(body, fg_color="transparent")
        heading.pack(fill="x", padx=(15, 8), pady=(9, 0))
        icon = {
            "success": "✓",
            "warning": "!",
            "error": "×",
            "busy": "…",
            "info": "i",
        }.get(self._severity, "i")
        ctk.CTkLabel(
            heading,
            text=icon,
            width=22,
            text_color=accent,
            font=font(15, "bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            heading,
            text=title or feedback_title(self._severity),
            text_color=accent,
            font=font(12, "bold"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(4, 8))
        close_button = ctk.CTkButton(
            heading,
            text="×",
            width=24,
            height=24,
            corner_radius=6,
            fg_color="transparent",
            hover_color=COLORS["surface_hover"],
            text_color=COLORS["muted"],
            font=font(14),
            command=self._dismiss,
        )
        close_button.pack(side="right")

        label = ctk.CTkLabel(
            body,
            text=self._message,
            text_color=COLORS["text"],
            font=font(13),
            padx=18,
            pady=9,
            wraplength=wraplength,
            anchor="w",
            justify="left",
        )
        label.pack(fill="x")

        self.update_idletasks()
        self._register_toast()
        self.bind("<Enter>", self._pause_dismiss, add="+")
        self.bind("<Leave>", self._resume_dismiss, add="+")
        self._schedule_dismiss()

    @staticmethod
    def _registered(master) -> list:
        try:
            registered = getattr(master, "_api_switcher_active_toasts", None)
            if not isinstance(registered, list):
                registered = []
                setattr(master, "_api_switcher_active_toasts", registered)
            registered[:] = [item for item in registered if item._exists()]
            return registered
        except Exception:
            return []

    def _exists(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

    def _register_toast(self) -> None:
        registered = self._registered(self._toast_master)
        registered.append(self)
        while len(registered) > _MAX_VISIBLE_TOASTS:
            oldest = registered[0]
            if oldest is self:
                break
            oldest._dismiss()
            registered = self._registered(self._toast_master)
        self._position_registered(self._toast_master)

    @classmethod
    def _position_registered(cls, master) -> None:
        registered = cls._registered(master)
        if not registered:
            return
        try:
            screen_x, screen_y, screen_width, screen_height = _screen_bounds(master)
            parent_right = master.winfo_rootx() + max(master.winfo_width(), 1)
            next_y = master.winfo_rooty() + 40
        except Exception:
            return
        for item in registered:
            try:
                item.update_idletasks()
                width = item.winfo_width()
                height = item.winfo_height()
                px = parent_right - width - 20
                px = min(max(px, screen_x + 8), screen_x + max(screen_width - width - 8, 0))
                py = min(max(next_y, screen_y + 8), screen_y + max(screen_height - height - 8, 0))
                item.geometry(f"+{px}+{py}")
                next_y = py + height + 8
            except Exception:
                continue

    def _schedule_dismiss(self) -> None:
        if self._dismiss_after_id:
            try:
                self.after_cancel(self._dismiss_after_id)
            except Exception:
                pass
        self._dismiss_after_id = self.after(self._duration, self._dismiss)

    def _pause_dismiss(self, _event=None) -> None:
        if not self._dismiss_after_id:
            return
        try:
            self.after_cancel(self._dismiss_after_id)
        except Exception:
            pass
        self._dismiss_after_id = None

    def _resume_dismiss(self, _event=None) -> None:
        if self._exists() and not self._dismiss_after_id:
            self._schedule_dismiss()

    def refresh_lifetime(self) -> None:
        """Keep a duplicate notification visible without creating an overlap."""

        try:
            self.lift()
            self._schedule_dismiss()
        except Exception:
            pass

    def _dismiss(self):
        pending = self._dismiss_after_id
        self._dismiss_after_id = None
        if pending:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
        self.destroy()

    def destroy(self):
        if self._dismiss_after_id:
            try:
                self.after_cancel(self._dismiss_after_id)
            except Exception:
                pass
            self._dismiss_after_id = None
        master = getattr(self, "_toast_master", None)
        try:
            registered = self._registered(master)
            if self in registered:
                registered.remove(self)
        except Exception:
            pass
        try:
            super().destroy()
        finally:
            if master is not None:
                self._position_registered(master)


def show_toast(
    master,
    message: str,
    is_error: bool = False,
    *,
    severity: str | None = None,
    title: str | None = None,
    duration: int | None = None,
):
    """Show or refresh a semantic toast notification."""

    safe_message = safe_feedback_text(message).strip() or "操作已完成"
    resolved = resolve_feedback_severity(
        safe_message,
        severity=severity,
        is_error=is_error,
    )
    for existing in Toast._registered(master):
        if existing._message == safe_message and existing._severity == resolved:
            existing.refresh_lifetime()
            return existing
    return Toast(
        master,
        safe_message,
        duration=duration,
        is_error=is_error,
        severity=resolved,
        title=title,
    )
