import threading
import webbrowser

import customtkinter as ctk

from core import network_diagnostic_settings, network_diagnostics
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, font, textbox_style
from ui.widgets.masked_entry import MaskedEntry
from ui.widgets.toast import show_toast


class ProxyQualityPanel(ctk.CTkScrollableFrame):
    """Panel for public network and proxy exit quality diagnostics."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._busy = False
        self._last_report = None
        self._run_button = None
        self._copy_button = None
        self._open_ping0_button = None
        self._ping0_key_entry = None
        self._proxycheck_key_entry = None
        self._ipapi_key_entry = None
        self._ipqs_key_entry = None
        self._vpnapi_key_entry = None
        self._service_vars = {}
        self._service_key_entries = {}
        self._service_key_frames = {}
        self._service_count_labels = {}
        self._service_cards = {}
        self._settings_controls = []
        self._hidden_service_settings = {}
        self._settings_status_label = None
        self._save_settings_button = None
        self._status_label = None
        self._content_frame = None
        self._report_box = None
        self._build_ui()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(fill="x")
        ctk.CTkLabel(
            title_area,
            text="代理质量检测",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        subtitle = ctk.CTkLabel(
            title_area,
            text="用于 Win11/SSH AI 代理的出口质量检测；先测速，再用 Ping0、ProxyCheck、ipapi.is、VPNAPI 评估 IP 质量",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        subtitle.pack(anchor="w", fill="x", pady=(2, 0))
        bind_wraplength(title_area, subtitle, padding=24, min_width=260, max_width=760)

        action_bar = ctk.CTkFrame(header, fg_color="transparent")
        action_bar.pack(anchor="w", pady=(8, 0))
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
        self._open_ping0_button = ctk.CTkButton(
            action_bar,
            text="打开最快 Ping0",
            width=124,
            command=self._open_fastest_ping0,
            state="disabled",
            **button_style("accent"),
        )
        self._open_ping0_button.pack(side="left", padx=(8, 0))

        settings_section = ctk.CTkFrame(self, fg_color="transparent")
        settings_section.pack(fill="x", padx=14, pady=(0, 10))
        settings_header = ctk.CTkFrame(settings_section, fg_color="transparent")
        settings_header.pack(fill="x", pady=(0, 6))
        settings_title = ctk.CTkFrame(settings_header, fg_color="transparent")
        settings_title.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            settings_title,
            text="检测源",
            text_color=COLORS["text"],
            font=font(14, "bold"),
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            settings_title,
            text="勾选检测源，按顺序轮换 Key 池",
            text_color=COLORS["muted_soft"],
            font=font(11),
            anchor="w",
        ).pack(anchor="w", pady=(1, 0))
        self._save_settings_button = ctk.CTkButton(
            settings_header,
            text="保存设置",
            width=92,
            command=lambda: self._save_detection_settings(show_message=True),
            **button_style("secondary"),
        )
        self._save_settings_button.pack(side="right", padx=(10, 0), pady=(2, 0))
        self._settings_controls.append(self._save_settings_button)
        saved_settings = network_diagnostic_settings.load_settings()
        self._hidden_service_settings = {
            service: saved_settings.service(service)
            for service in network_diagnostic_settings.HIDDEN_SERVICES
        }
        service_rows = [
            (
                network_diagnostic_settings.SERVICE_PING0,
                "Ping0",
                "免费 Geo；Key 返回 isidc、iprisk、isnative",
            ),
            (
                network_diagnostic_settings.SERVICE_PROXYCHECK,
                "ProxyCheck",
                "可无 Key；Key 池用于更稳定额度",
            ),
            (
                network_diagnostic_settings.SERVICE_IPAPI,
                "ipapi.is",
                "可无 Key；补充机房、ASN、Abuser、VPN/Proxy 信号",
            ),
            (
                network_diagnostic_settings.SERVICE_VPNAPI,
                "VPNAPI.io",
                "VPN、Proxy、Tor、Relay",
            ),
        ]
        for service, label, description in service_rows:
            self._add_service_setting_row(settings_section, service, label, description, saved_settings)

        settings_actions = ctk.CTkFrame(settings_section, fg_color="transparent")
        settings_actions.pack(fill="x", pady=(4, 0))
        self._settings_status_label = ctk.CTkLabel(
            settings_actions,
            text=self._settings_status_text(saved_settings),
            text_color=COLORS["muted_soft"],
            font=font(11),
            anchor="w",
        )
        self._settings_status_label.pack(fill="x")
        bind_wraplength(settings_actions, self._settings_status_label, padding=20)
        self._update_settings_preview()

        status_card = ctk.CTkFrame(self, **card_frame_kwargs())
        status_card.pack(fill="x", padx=14, pady=(0, 10))
        self._status_label = ctk.CTkLabel(
            status_card,
            text="未检测。点击后会先测速，再只对可连通 IP 调用 Ping0 和信誉检测接口。",
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

    def _add_service_setting_row(self, parent, service: str, label: str, description: str, saved_settings):
        service_settings = saved_settings.service(service)
        var = ctk.BooleanVar(value=service_settings.enabled)
        self._service_vars[service] = var
        border = COLORS["success"] if service_settings.enabled else COLORS["border_soft"]
        card = ctk.CTkFrame(parent, **card_frame_kwargs(border))
        card.pack(fill="x", pady=5)
        self._service_cards[service] = card

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 6))
        top.grid_columnconfigure(1, weight=1)
        service_check = ctk.CTkCheckBox(
            top,
            text=label,
            variable=var,
            command=self._update_settings_preview,
            text_color=COLORS["text"],
            font=font(13, "bold"),
            width=132,
            checkbox_width=18,
            checkbox_height=18,
        )
        service_check.grid(row=0, column=0, sticky="w")
        self._settings_controls.append(service_check)
        desc = ctk.CTkLabel(
            top,
            text=description,
            text_color=COLORS["muted"],
            font=font(11),
            anchor="w",
        )
        desc.grid(row=0, column=1, sticky="ew", padx=(10, 8))
        self._service_count_labels[service] = ctk.CTkLabel(
            top,
            text="",
            text_color=COLORS["muted_soft"],
            font=font(11),
            width=72,
            anchor="e",
        )
        self._service_count_labels[service].grid(row=0, column=2, sticky="e", padx=(0, 8))
        add_button = ctk.CTkButton(
            top,
            text="添加 Key",
            width=82,
            command=lambda service=service: self._add_key_row(service, focus=True),
            **button_style("secondary", compact=True),
        )
        add_button.grid(row=0, column=3, sticky="e")
        self._settings_controls.append(add_button)

        key_frame = ctk.CTkFrame(card, fg_color="transparent")
        key_frame.pack(fill="x", padx=12, pady=(0, 10))
        self._service_key_frames[service] = key_frame
        self._service_key_entries[service] = []
        keys = service_settings.api_keys or [""]
        for key in keys:
            self._add_key_row(service, key)
        if service == network_diagnostic_settings.SERVICE_PING0:
            self._ping0_key_entry = self._service_key_entries[service][0]
        elif service == network_diagnostic_settings.SERVICE_PROXYCHECK:
            self._proxycheck_key_entry = self._service_key_entries[service][0]
        elif service == network_diagnostic_settings.SERVICE_IPAPI:
            self._ipapi_key_entry = self._service_key_entries[service][0]
        elif service == network_diagnostic_settings.SERVICE_IPQS:
            self._ipqs_key_entry = self._service_key_entries[service][0]
        elif service == network_diagnostic_settings.SERVICE_VPNAPI:
            self._vpnapi_key_entry = self._service_key_entries[service][0]
        bind_wraplength(top, desc, padding=320, min_width=180, max_width=620)

    def _add_key_row(self, service: str, value: str = "", focus: bool = False):
        key_frame = self._service_key_frames.get(service)
        if key_frame is None:
            return
        row = ctk.CTkFrame(key_frame, fg_color="transparent")
        row.pack(fill="x", pady=(0, 5))
        row.grid_columnconfigure(0, weight=1)
        key_index = len(self._service_key_entries.get(service, [])) + 1
        entry = MaskedEntry(row, placeholder=f"Key #{key_index}", width=420)
        entry.grid(row=0, column=0, sticky="ew")
        entry.set(value)
        try:
            entry.entry.bind("<KeyRelease>", lambda _event, service=service: self._update_settings_preview(), add="+")
        except TypeError:
            entry.entry.bind("<KeyRelease>", lambda _event, service=service: self._update_settings_preview())
        delete_button = ctk.CTkButton(
            row,
            text="删除",
            width=58,
            command=lambda service=service, entry=entry, row=row: self._remove_key_row(service, entry, row),
            **button_style("secondary", compact=True),
        )
        delete_button.grid(row=0, column=1, padx=(6, 0), sticky="e")
        self._service_key_entries.setdefault(service, []).append(entry)
        self._settings_controls.extend([entry.entry, entry.toggle_btn, delete_button])
        self._update_settings_preview()
        if focus:
            try:
                entry.entry.focus_set()
            except Exception:
                pass

    def _remove_key_row(self, service: str, entry: MaskedEntry, row):
        entries = self._service_key_entries.get(service, [])
        if len(entries) <= 1:
            entry.set("")
            self._update_settings_preview()
            return
        self._service_key_entries[service] = [item for item in entries if item is not entry]
        row.destroy()
        self._renumber_key_placeholders(service)
        self._update_settings_preview()

    def _renumber_key_placeholders(self, service: str):
        for index, entry in enumerate(self._service_key_entries.get(service, []), start=1):
            try:
                entry.entry.configure(placeholder_text=f"Key #{index}")
            except Exception:
                pass

    def _collect_detection_settings(self):
        enabled = [
            service
            for service, var in self._service_vars.items()
            if bool(var.get())
        ]
        api_keys = {
            service: [entry.get() for entry in entries]
            for service, entries in self._service_key_entries.items()
        }
        for service in network_diagnostic_settings.HIDDEN_SERVICES:
            service_settings = self._hidden_service_settings.get(service)
            if service_settings is None:
                continue
            if service_settings.enabled:
                enabled.append(service)
            api_keys[service] = service_settings.api_keys
        return network_diagnostic_settings.settings_from_values(enabled, api_keys)

    def _update_settings_preview(self):
        try:
            settings = self._collect_detection_settings()
        except Exception:
            return
        for service in network_diagnostic_settings.VISIBLE_SERVICE_ORDER:
            service_settings = settings.service(service)
            count_label = self._service_count_labels.get(service)
            if count_label:
                count = len(service_settings.api_keys)
                if count:
                    count_label.configure(text=f"{count} 个 Key", text_color=COLORS["success"])
                elif service in {network_diagnostic_settings.SERVICE_PROXYCHECK, network_diagnostic_settings.SERVICE_IPAPI}:
                    count_label.configure(text="可无 Key", text_color=COLORS["muted_soft"])
                elif service == network_diagnostic_settings.SERVICE_PING0:
                    count_label.configure(text="免费 Geo", text_color=COLORS["muted_soft"])
                else:
                    count_label.configure(text="需 Key", text_color=COLORS["warning"])
            card = self._service_cards.get(service)
            if card:
                card.configure(border_color=COLORS["success"] if service_settings.enabled else COLORS["border_soft"])
        if self._settings_status_label:
            self._settings_status_label.configure(text=self._settings_status_text(settings), text_color=COLORS["muted_soft"])

    def _set_settings_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for widget in list(self._settings_controls):
            try:
                if widget.winfo_exists():
                    widget.configure(state=state)
            except Exception:
                pass

    def _save_detection_settings(self, show_message: bool = False):
        settings = self._collect_detection_settings()
        network_diagnostic_settings.save_settings(settings)
        if self._settings_status_label:
            self._settings_status_label.configure(text=self._settings_status_text(settings), text_color=COLORS["success"])
        if show_message:
            show_toast(self.winfo_toplevel(), "代理质量检测设置已保存")
        return settings

    def _settings_status_text(self, settings) -> str:
        enabled_labels = []
        key_counts = []
        for service in network_diagnostic_settings.VISIBLE_SERVICE_ORDER:
            service_settings = settings.service(service)
            label = network_diagnostic_settings.SERVICE_LABELS.get(service, service)
            if service_settings.enabled:
                enabled_labels.append(label)
            if service_settings.api_keys:
                key_counts.append(f"{label} {len(service_settings.api_keys)} 个 Key")
        enabled_text = "、".join(enabled_labels) if enabled_labels else "未启用检测源"
        keys_text = "；".join(key_counts) if key_counts else "未保存 API Key"
        return f"已启用: {enabled_text}  |  {keys_text}"

    def refresh(self):
        if self._last_report:
            self._render_report(self._last_report)

    def _start_detection(self):
        if self._busy:
            show_toast(self.winfo_toplevel(), "代理质量检测正在进行中", is_error=True)
            return

        self._busy = True
        self._set_settings_enabled(False)
        if self._run_button:
            self._run_button.configure(text="检测中...", state="disabled")
        if self._copy_button:
            self._copy_button.configure(state="disabled")
        if self._open_ping0_button:
            self._open_ping0_button.configure(state="disabled")
        try:
            detection_settings = self._save_detection_settings(show_message=False)
        except Exception as exc:
            self._busy = False
            self._set_settings_enabled(True)
            if self._run_button:
                self._run_button.configure(text="开始检测", state="normal")
            self._set_status(f"保存检测设置失败: {exc}", "error")
            show_toast(self.winfo_toplevel(), f"保存检测设置失败: {exc}", is_error=True)
            return

        enabled_services = detection_settings.enabled_services()
        enabled_text = "、".join(network_diagnostic_settings.SERVICE_LABELS.get(item, item) for item in enabled_services) or "无"
        self._set_status(f"正在测速公网出口；可连通后调用已启用检测源: {enabled_text}...")
        self._clear_content()
        self._add_info_card("检测中", [f"正在测速 IPv4、IPv6 和默认出口；只会对成功连通的 IP 调用: {enabled_text}。"])
        self._set_report_text("检测中...")

        def worker():
            try:
                report = network_diagnostics.detect_network(
                    enabled_services=enabled_services,
                    ping0_api_keys=detection_settings.keys_for(network_diagnostic_settings.SERVICE_PING0),
                    proxycheck_api_keys=detection_settings.keys_for(network_diagnostic_settings.SERVICE_PROXYCHECK),
                    ipapi_api_keys=detection_settings.keys_for(network_diagnostic_settings.SERVICE_IPAPI),
                    ipqs_api_keys=detection_settings.keys_for(network_diagnostic_settings.SERVICE_IPQS),
                    vpnapi_api_keys=detection_settings.keys_for(network_diagnostic_settings.SERVICE_VPNAPI),
                )
                payload = {"ok": True, "report": report, "error": ""}
            except Exception as exc:
                payload = {"ok": False, "report": None, "error": str(exc)}

            def finish():
                if not self._is_alive():
                    return
                self._busy = False
                self._set_settings_enabled(True)
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
                if self._open_ping0_button and payload["report"].diagnostics:
                    self._open_ping0_button.configure(state="normal")

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
                "勾选的检测源才会被调用；开始检测前会自动保存当前设置。",
                "每个检测源可以通过“添加 Key”保存多个 API Key。",
                "检测时会按顺序尝试 Key；遇到失败或限额会自动换下一个。",
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
            f"测速: {self._format_seconds(diagnostic.probe.response_time)}",
            f"类型: {cls.ip_type}  |  风险: {cls.risk_score}% {cls.risk_label}  |  置信度: {cls.confidence}",
            f"Ping0: {diagnostic.ping0.quality_text()}",
        ]
        if diagnostic.reputation:
            for item in diagnostic.reputation:
                lines.append(f"信誉检测: {item.summary_text()}")
                lines.extend(f"{item.source_label} Key: {attempt}" for attempt in item.attempts)
        lines.extend(
            [
                f"位置: {geo.location_text()}",
                f"ASN: {geo.owner_text()}",
                f"企业/ISP: {geo.org or '-'} / {geo.isp or '-'}",
                f"反向 DNS: {diagnostic.reverse_dns or '-'}",
                f"Ping0 详情: {diagnostic.ping0.detail_url}",
                f"Ping0 Ping: {diagnostic.ping0.ping_url}",
            ]
        )
        if diagnostic.ping0.ok and diagnostic.ping0.source == "ping0-api":
            lines.extend(
                [
                    f"Ping0 位置: {diagnostic.ping0.location or '-'}",
                    f"Ping0 ASN: {diagnostic.ping0.asn or '-'} {diagnostic.ping0.asn_name or diagnostic.ping0.org or ''}".strip(),
                ]
            )
            if diagnostic.ping0.attempts:
                lines.extend(f"Ping0 Key: {attempt}" for attempt in diagnostic.ping0.attempts)
        elif diagnostic.ping0.ok and diagnostic.ping0.source == "ping0-free-geo":
            lines.append(f"Ping0 免费 Geo: {diagnostic.ping0.location or '-'} | {diagnostic.ping0.asn or '-'} | {diagnostic.ping0.org or '-'}")
            if diagnostic.ping0.attempts:
                lines.extend(f"Ping0 Key: {attempt}" for attempt in diagnostic.ping0.attempts)
        elif diagnostic.ping0.error:
            lines.append(f"Ping0 状态: {diagnostic.ping0.error}")
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
        clean_lines = [str(line) for line in (lines or []) if str(line)]
        if not clean_lines:
            clean_lines = ["-"]
        for line in clean_lines:
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

    def _is_alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

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
        try:
            self._report_box.configure(state="normal")
            self._report_box.delete("1.0", "end")
            self._report_box.insert("1.0", str(text or ""))
            self._report_box.configure(state="disabled")
        except Exception:
            pass

    def _copy_report(self):
        if not self._last_report:
            return
        try:
            text = self._format_report(self._last_report)
            self.clipboard_clear()
            self.clipboard_append(text)
            show_toast(self.winfo_toplevel(), "检测报告已复制")
        except Exception as exc:
            show_toast(self.winfo_toplevel(), f"复制报告失败: {exc}", is_error=True)

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
                    f"  测速: {self._format_seconds(diagnostic.probe.response_time)}",
                    f"  Ping0: {diagnostic.ping0.quality_text()}",
                    f"  类型: {cls.ip_type}",
                    f"  风险: {cls.risk_score}% {cls.risk_label}",
                    f"  位置: {geo.location_text()}",
                    f"  ASN: {geo.owner_text()}",
                    f"  企业/ISP: {geo.org or '-'} / {geo.isp or '-'}",
                    f"  反向 DNS: {diagnostic.reverse_dns or '-'}",
                    f"  Ping0 详情: {diagnostic.ping0.detail_url}",
                    f"  Ping0 Ping: {diagnostic.ping0.ping_url}",
                ]
            )
            if diagnostic.reputation:
                lines.append("  信誉检测:")
                for item in diagnostic.reputation:
                    lines.append(f"  - {item.summary_text()}")
                    for attempt in item.attempts:
                        lines.append(f"    Key 尝试: {attempt}")
            if diagnostic.ping0.ok:
                lines.append(f"  Ping0 数据源: {diagnostic.ping0.source}")
                if diagnostic.ping0.location:
                    lines.append(f"  Ping0 位置: {diagnostic.ping0.location}")
                if diagnostic.ping0.asn or diagnostic.ping0.asn_name or diagnostic.ping0.org:
                    lines.append(f"  Ping0 ASN/企业: {diagnostic.ping0.asn or '-'} {diagnostic.ping0.asn_name or diagnostic.ping0.org}")
                for attempt in diagnostic.ping0.attempts:
                    lines.append(f"  Ping0 Key 尝试: {attempt}")
            elif diagnostic.ping0.error:
                lines.append(f"  Ping0 状态: {diagnostic.ping0.error}")
            for signal in cls.signals:
                lines.append(f"  信号: {signal}")
        lines.append("")
        lines.append("限制:")
        for notice in report.notices + _collect_limitations(report):
            lines.append(f"- {notice}")
        return "\n".join(lines)

    def _open_fastest_ping0(self):
        if not self._last_report or not self._last_report.diagnostics:
            return
        try:
            fastest = min(
                self._last_report.diagnostics,
                key=lambda diagnostic: diagnostic.probe.response_time if diagnostic.probe.response_time is not None else float("inf"),
            )
            webbrowser.open(fastest.ping0.detail_url)
            show_toast(self.winfo_toplevel(), "已打开最快出口的 Ping0 详情页")
        except Exception as exc:
            show_toast(self.winfo_toplevel(), f"打开 Ping0 失败: {exc}", is_error=True)

    def _format_seconds(self, value: float | None) -> str:
        try:
            return f"{float(value):.2f}s" if value is not None else "-"
        except (TypeError, ValueError):
            return "-"


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
