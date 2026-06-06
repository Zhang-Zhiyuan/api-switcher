import threading

import customtkinter as ctk

from core import network_diagnostics
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, font, textbox_style
from ui.widgets.toast import show_toast


class NetworkDiagnosticsTab(ctk.CTkScrollableFrame):
    """Tab for public network exit diagnostics."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._busy = False
        self._last_report = None
        self._run_button = None
        self._copy_button = None
        self._status_label = None
        self._content_frame = None
        self._report_box = None
        self._build_ui()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="环境检测",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        subtitle = ctk.CTkLabel(
            title_area,
            text="当前网络出口、ASN、位置、IPv4/IPv6 与启发式风险",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        subtitle.pack(anchor="w", fill="x", pady=(2, 0))
        bind_wraplength(title_area, subtitle, padding=24, min_width=260, max_width=760)

        action_bar = ctk.CTkFrame(header, fg_color="transparent")
        action_bar.pack(side="right", padx=(12, 0))
        self._run_button = ctk.CTkButton(
            action_bar,
            text="开始检测",
            width=108,
            command=self._start_detection,
            **button_style("primary"),
        )
        self._run_button.pack(side="left")
        self._copy_button = ctk.CTkButton(
            action_bar,
            text="复制报告",
            width=108,
            command=self._copy_report,
            state="disabled",
            **button_style("secondary"),
        )
        self._copy_button.pack(side="left", padx=(8, 0))

        status_card = ctk.CTkFrame(self, **card_frame_kwargs())
        status_card.pack(fill="x", padx=14, pady=(0, 10))
        self._status_label = ctk.CTkLabel(
            status_card,
            text="未检测。公开查询只会在点击“开始检测”后发起。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._status_label.pack(fill="x", padx=14, pady=12)
        bind_wraplength(status_card, self._status_label, padding=28)

        self._content_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._content_frame.pack(fill="x", padx=14, pady=(0, 10))
        self._render_empty()

        self._report_box = ctk.CTkTextbox(self, height=190, **textbox_style(monospace=True))
        self._report_box.pack(fill="x", padx=14, pady=(0, 14))
        self._set_report_text("等待检测结果...")

    def refresh(self):
        if self._last_report:
            self._render_report(self._last_report)

    def _start_detection(self):
        if self._busy:
            show_toast(self.winfo_toplevel(), "环境检测正在进行中", is_error=True)
            return

        self._busy = True
        if self._run_button:
            self._run_button.configure(text="检测中...", state="disabled")
        if self._copy_button:
            self._copy_button.configure(state="disabled")
        self._set_status("正在检测公网出口、Geo/ASN 和反向 DNS...")
        self._clear_content()
        self._add_info_card("检测中", ["正在请求公开端点，请稍等。"])
        self._set_report_text("检测中...")

        def worker():
            try:
                report = network_diagnostics.detect_network()
                payload = {"ok": True, "report": report, "error": ""}
            except Exception as exc:
                payload = {"ok": False, "report": None, "error": str(exc)}

            def finish():
                if not self.winfo_exists():
                    return
                self._busy = False
                if self._run_button:
                    self._run_button.configure(text="重新检测", state="normal")
                if not payload["ok"]:
                    self._set_status(f"检测失败: {payload['error']}", "error")
                    self._clear_content()
                    self._add_info_card("检测失败", [payload["error"]], COLORS["danger"])
                    self._set_report_text(f"检测失败: {payload['error']}")
                    return

                self._last_report = payload["report"]
                self._render_report(payload["report"])
                if self._copy_button:
                    self._copy_button.configure(state="normal")

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _render_empty(self):
        self._clear_content()
        self._add_info_card(
            "待检测",
            [
                "当前页不会自动上传网络信息。",
                "启发式结果会明确标注数据来源和限制。",
            ],
        )

    def _render_report(self, report: network_diagnostics.NetworkDiagnosticReport):
        self._clear_content()
        self._set_status(f"{report.generated_at}  |  {report.summary}", "success")

        self._add_info_card(
            "概览",
            [
                report.summary,
                f"IPv4: {'已检测到' if report.has_ipv4 else '未检测到'}",
                f"IPv6: {'已检测到' if report.has_ipv6 else '未检测到'}",
            ],
            COLORS["success"] if report.diagnostics else COLORS["warning"],
        )

        self._add_info_card(
            "公开端点",
            [self._format_probe(probe) for probe in report.probes],
        )

        for diagnostic in report.diagnostics:
            self._add_diagnostic_card(diagnostic)

        self._add_info_card("限制", report.notices + _collect_limitations(report), COLORS["warning"])
        self._set_report_text(self._format_report(report))

    def _add_diagnostic_card(self, diagnostic: network_diagnostics.IpDiagnostic):
        cls = diagnostic.classification
        geo = diagnostic.geo
        border = _risk_border(cls.risk_score)
        lines = [
            f"类型: {cls.ip_type}  |  启发式风险: {cls.risk_score}% {cls.risk_label}  |  置信度: {cls.confidence}",
            f"位置: {geo.location_text()}",
            f"ASN: {geo.owner_text()}",
            f"企业/ISP: {geo.org or '-'} / {geo.isp or '-'}",
            f"反向 DNS: {diagnostic.reverse_dns or '-'}",
        ]
        if geo.latitude is not None and geo.longitude is not None:
            lines.append(f"经纬度: {geo.latitude}, {geo.longitude}")
        if geo.timezone:
            lines.append(f"时区: {geo.timezone}")
        if cls.signals:
            lines.extend(f"信号: {signal}" for signal in cls.signals)
        self._add_info_card(f"{diagnostic.label}  {diagnostic.ip}", lines, border)

    def _add_info_card(self, title: str, lines: list[str], border_color: str | None = None):
        if not self._content_frame:
            return
        card = ctk.CTkFrame(self._content_frame, **card_frame_kwargs(border_color))
        card.pack(fill="x", pady=5)
        ctk.CTkLabel(
            card,
            text=title,
            text_color=COLORS["text"],
            font=font(14, "bold"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 4))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=14, pady=(0, 12))
        for line in lines:
            label = ctk.CTkLabel(
                body,
                text=line,
                text_color=COLORS["muted"],
                font=font(12),
                anchor="w",
                justify="left",
            )
            label.pack(fill="x", pady=(1, 0))
            bind_wraplength(body, label, padding=8)

    def _clear_content(self):
        if not self._content_frame:
            return
        for child in self._content_frame.winfo_children():
            child.destroy()

    def _set_status(self, message: str, severity: str = "info"):
        if not self._status_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._status_label.configure(text=message, text_color=color)

    def _set_report_text(self, text: str):
        if not self._report_box:
            return
        self._report_box.configure(state="normal")
        self._report_box.delete("1.0", "end")
        self._report_box.insert("1.0", text)
        self._report_box.configure(state="disabled")

    def _copy_report(self):
        if not self._last_report:
            return
        text = self._format_report(self._last_report)
        self.clipboard_clear()
        self.clipboard_append(text)
        show_toast(self.winfo_toplevel(), "检测报告已复制")

    def _format_probe(self, probe: network_diagnostics.EndpointProbe) -> str:
        elapsed = f"{probe.response_time:.2f}s" if probe.response_time is not None else "-"
        if probe.ok:
            return f"{probe.label}: {probe.ip}  |  {elapsed}"
        return f"{probe.label}: 失败  |  {probe.error or '-'}  |  {elapsed}"

    def _format_report(self, report: network_diagnostics.NetworkDiagnosticReport) -> str:
        lines = [
            f"生成时间: {report.generated_at}",
            f"摘要: {report.summary}",
            "",
            "公开端点:",
        ]
        lines.extend(f"- {self._format_probe(probe)}" for probe in report.probes)
        lines.append("")
        lines.append("IP 诊断:")
        for diagnostic in report.diagnostics:
            geo = diagnostic.geo
            cls = diagnostic.classification
            lines.extend(
                [
                    f"- {diagnostic.label}: {diagnostic.ip}",
                    f"  类型: {cls.ip_type}",
                    f"  启发式风险: {cls.risk_score}% {cls.risk_label}",
                    f"  位置: {geo.location_text()}",
                    f"  ASN: {geo.owner_text()}",
                    f"  企业/ISP: {geo.org or '-'} / {geo.isp or '-'}",
                    f"  反向 DNS: {diagnostic.reverse_dns or '-'}",
                ]
            )
            for signal in cls.signals:
                lines.append(f"  信号: {signal}")
        lines.append("")
        lines.append("限制:")
        for notice in report.notices + _collect_limitations(report):
            lines.append(f"- {notice}")
        return "\n".join(lines)


def _risk_border(score: int) -> str:
    if score >= 70:
        return COLORS["danger"]
    if score >= 50:
        return COLORS["warning"]
    if score <= 25:
        return COLORS["success"]
    return COLORS["border_soft"]


def _collect_limitations(report: network_diagnostics.NetworkDiagnosticReport) -> list[str]:
    limitations: list[str] = []
    seen: set[str] = set()
    for diagnostic in report.diagnostics:
        for item in diagnostic.classification.limitations:
            if item not in seen:
                seen.add(item)
                limitations.append(item)
    return limitations
