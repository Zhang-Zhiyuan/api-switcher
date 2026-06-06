import customtkinter as ctk

from ui.tabs.network_diagnostics_tab import NetworkDiagnosticsTab
from ui.theme import COLORS, center_window


class NetworkDiagnosticsDialog(ctk.CTkToplevel):
    """Proxy quality diagnostics window launched from proxy pages."""

    def __init__(self, master, on_close=None):
        super().__init__(master)
        self._on_close = on_close
        self._closed = False
        self.title("代理质量检测")
        self.geometry("900x700")
        self.minsize(760, 560)
        self.configure(fg_color=COLORS["app_bg"])
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Destroy>", self._on_destroy, add="+")

        self.panel = NetworkDiagnosticsTab(self)
        self.panel.pack(fill="both", expand=True)

        center_window(self, master)
        try:
            self.transient(master)
        except Exception:
            pass
        self.focus()
        self.lift()

    def _close(self):
        self._notify_closed()
        self.destroy()

    def destroy(self):
        self._notify_closed()
        super().destroy()

    def _on_destroy(self, event):
        if event.widget is self:
            self._notify_closed()

    def _notify_closed(self):
        if self._closed:
            return
        self._closed = True
        if self._on_close:
            self._on_close()
