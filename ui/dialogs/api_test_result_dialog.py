"""Dialog for displaying API test results."""
import customtkinter as ctk
from ui.theme import COLORS, button_style, center_window, font


class APITestResultDialog(ctk.CTkToplevel):
    """Dialog showing API connection test results."""

    def __init__(self, parent, test_result, profile_name: str = ""):
        super().__init__(parent)

        self.title("API 连接测试")
        self.geometry("500x400")
        self.resizable(False, False)
        self.configure(fg_color=COLORS["app_bg"])

        # Make modal
        self.transient(parent)
        self.grab_set()

        self._build_ui(test_result, profile_name)
        center_window(self, parent)

    def _build_ui(self, result, profile_name: str):
        """Build the dialog UI."""
        # Container
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)

        # Header
        header = ctk.CTkFrame(container, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        # Icon and title
        if result.success:
            icon = "✓"
            icon_color = COLORS["success"]
            title_text = "连接成功"
        else:
            icon = "✗"
            icon_color = COLORS["danger"]
            title_text = "连接失败"

        icon_label = ctk.CTkLabel(
            header,
            text=icon,
            font=font(48, "bold"),
            text_color=icon_color
        )
        icon_label.pack()

        title_label = ctk.CTkLabel(
            header,
            text=title_text,
            font=font(20, "bold"),
            text_color=COLORS["text"]
        )
        title_label.pack(pady=(5, 0))

        if profile_name:
            profile_label = ctk.CTkLabel(
                header,
                text=f"配置: {profile_name}",
                font=font(12),
                text_color=COLORS["muted"]
            )
            profile_label.pack(pady=(5, 0))

        # Details frame
        details_frame = ctk.CTkFrame(
            container,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"]
        )
        details_frame.pack(fill="both", expand=True, pady=(0, 20))

        # Scrollable frame for details
        scroll_frame = ctk.CTkScrollableFrame(
            details_frame,
            fg_color="transparent"
        )
        scroll_frame.pack(fill="both", expand=True, padx=15, pady=15)

        # Message
        self._add_detail_row(scroll_frame, "状态", result.message,
                            COLORS["success"] if result.success else COLORS["danger"])

        # Response time
        if result.response_time is not None:
            time_text = f"{result.response_time:.0f} ms"
            time_color = COLORS["success"] if result.response_time < 1000 else COLORS["warning"]
            self._add_detail_row(scroll_frame, "响应时间", time_text, time_color)

        # Status code
        if result.status_code is not None:
            code_color = COLORS["success"] if result.status_code == 200 else COLORS["warning"]
            self._add_detail_row(scroll_frame, "HTTP 状态码", str(result.status_code), code_color)

        # Error details
        if result.error_details:
            self._add_detail_row(scroll_frame, "错误详情", result.error_details, COLORS["muted"])

        # Recommendations
        if not result.success:
            separator = ctk.CTkFrame(scroll_frame, height=1, fg_color=COLORS["border_soft"])
            separator.pack(fill="x", pady=10)

            rec_label = ctk.CTkLabel(
                scroll_frame,
                text="💡 建议",
                font=font(12, "bold"),
                text_color=COLORS["text"],
                anchor="w"
            )
            rec_label.pack(fill="x", pady=(5, 5))

            recommendations = self._get_recommendations(result)
            for rec in recommendations:
                rec_item = ctk.CTkLabel(
                    scroll_frame,
                    text=f"• {rec}",
                    font=font(11),
                    text_color=COLORS["muted"],
                    anchor="w",
                    wraplength=420,
                    justify="left"
                )
                rec_item.pack(fill="x", pady=2, padx=(10, 0))

        # Buttons
        button_frame = ctk.CTkFrame(container, fg_color="transparent")
        button_frame.pack(fill="x")

        close_btn = ctk.CTkButton(
            button_frame,
            text="关闭",
            width=120,
            command=self.destroy,
            **button_style("primary")
        )
        close_btn.pack(side="right")

    def _add_detail_row(self, parent, label: str, value: str, value_color: str = None):
        """Add a detail row to the dialog."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)

        label_widget = ctk.CTkLabel(
            row,
            text=f"{label}:",
            font=font(11, "bold"),
            text_color=COLORS["text"],
            anchor="w",
            width=100
        )
        label_widget.pack(side="left")

        value_widget = ctk.CTkLabel(
            row,
            text=value,
            font=font(11),
            text_color=value_color or COLORS["text"],
            anchor="w",
            wraplength=350,
            justify="left"
        )
        value_widget.pack(side="left", fill="x", expand=True)

    def _get_recommendations(self, result) -> list:
        """Get recommendations based on test result."""
        recommendations = []

        if "认证失败" in result.message or "API Key 无效" in result.message:
            recommendations.append("检查 API Key 是否正确")
            recommendations.append("确认 API Key 未过期")
            recommendations.append("验证 API Key 的权限")

        elif "端点不存在" in result.message:
            recommendations.append("检查 Base URL 是否正确")
            recommendations.append("确认 API 版本是否匹配")
            recommendations.append("验证模型名称是否正确")

        elif "网络错误" in result.message:
            recommendations.append("检查网络连接")
            recommendations.append("确认防火墙未阻止连接")
            recommendations.append("尝试使用代理或 VPN")

        elif "超时" in result.message:
            recommendations.append("检查网络速度")
            recommendations.append("增加超时时间")
            recommendations.append("稍后重试")

        elif "速率限制" in result.message:
            recommendations.append("降低请求频率")
            recommendations.append("等待一段时间后重试")
            recommendations.append("考虑升级 API 套餐")

        elif "服务器错误" in result.message:
            recommendations.append("API 服务器可能正在维护")
            recommendations.append("稍后重试")
            recommendations.append("检查 API 状态页面")

        else:
            recommendations.append("检查所有配置项是否正确")
            recommendations.append("查看日志获取更多信息")
            recommendations.append("联系 API 提供商支持")

        return recommendations
