"""Restore only the modal grab that a dialog actually displaced."""
import tkinter as tk

import customtkinter as ctk


class RestoreGrabDialog(ctk.CTkToplevel):
    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self._previous_grab = self.grab_current()
        self._modal_destroyed = False

    def destroy(self):
        if self._modal_destroyed:
            return
        self._modal_destroyed = True
        # Complete pending geometry before nested native Windows teardown, but
        # never run timers/input via a reentrant update().
        self.update_idletasks()
        held_grab = self.grab_current() is self
        if held_grab:
            self.grab_release()
        super().destroy()
        previous = self._previous_grab
        try:
            if (held_grab and previous is not None and previous.winfo_exists()
                    and not getattr(previous, "_closed", False)
                    and not getattr(previous, "_modal_destroyed", False)
                    and previous.grab_current() is None):
                previous.grab_set()
        except tk.TclError:
            pass
