"""
搜索栏组件
"""
import customtkinter as ctk
from typing import Callable, Optional
from ui.theme import COLORS, font, input_style


class SearchBar(ctk.CTkFrame):
    """搜索栏组件"""

    def __init__(
        self,
        master,
        placeholder: str = "搜索...",
        on_search: Optional[Callable[[str], None]] = None,
        **kwargs
    ):
        super().__init__(master, fg_color="transparent", **kwargs)

        self.on_search = on_search
        self._search_history: list[str] = []
        self._search_after_id = None

        self._build_ui(placeholder)

    def destroy(self):
        self._cancel_pending_search()
        super().destroy()

    def _build_ui(self, placeholder: str):
        """构建 UI"""
        # 搜索图标（可选）
        # search_icon = ctk.CTkLabel(self, text="🔍", font=font(14))
        # search_icon.pack(side="left", padx=(0, 5))

        # 搜索输入框
        self.search_entry = ctk.CTkEntry(
            self,
            placeholder_text=placeholder,
            **input_style(),
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        # 绑定事件
        self.search_entry.bind("<Return>", self._on_enter)
        self.search_entry.bind("<KeyRelease>", self._on_key_release)

        # 清除按钮
        self.clear_button = ctk.CTkButton(
            self,
            text="✕",
            width=32,
            height=32,
            command=self._clear_search,
            fg_color=COLORS["surface"],
            hover_color=COLORS["surface_hover"],
            text_color=COLORS["muted"],
            font=font(14)
        )
        self.clear_button.pack(side="left")
        self.clear_button.pack_forget()  # 初始隐藏

    def _on_enter(self, event):
        """回车键触发搜索"""
        self._cancel_pending_search()
        query = self.search_entry.get().strip()
        if query:
            self._add_to_history(query)
            if self.on_search:
                self.on_search(query)

    def _on_key_release(self, event):
        """按键释放时实时搜索"""
        query = self.search_entry.get().strip()

        # 显示/隐藏清除按钮
        if query:
            self.clear_button.pack(side="left")
        else:
            self.clear_button.pack_forget()

        # 实时搜索（延迟触发）
        if self.on_search:
            # 取消之前的延迟调用
            self._cancel_pending_search()

            # 延迟 300ms 后触发搜索
            def fire_search():
                self._search_after_id = None
                if self.on_search:
                    self.on_search(query)

            self._search_after_id = self.after(300, fire_search)

    def _clear_search(self):
        """清除搜索"""
        self._cancel_pending_search()
        self.search_entry.delete(0, "end")
        self.clear_button.pack_forget()
        if self.on_search:
            self.on_search("")

    def _cancel_pending_search(self):
        if not self._search_after_id:
            return
        try:
            self.after_cancel(self._search_after_id)
        except Exception:
            pass
        self._search_after_id = None

    def _add_to_history(self, query: str):
        """添加到搜索历史"""
        if query and query not in self._search_history:
            self._search_history.insert(0, query)
            # 最多保留 10 条历史
            self._search_history = self._search_history[:10]

    def get_query(self) -> str:
        """获取当前搜索查询"""
        return self.search_entry.get().strip()

    def set_query(self, query: str):
        """设置搜索查询"""
        self.search_entry.delete(0, "end")
        self.search_entry.insert(0, query)

    def focus(self):
        """聚焦到搜索框"""
        self.search_entry.focus()

    def get_history(self) -> list[str]:
        """获取搜索历史"""
        return self._search_history.copy()


def fuzzy_match(query: str, text: str) -> bool:
    """
    模糊匹配

    Args:
        query: 搜索查询
        text: 要匹配的文本

    Returns:
        是否匹配
    """
    if not query:
        return True

    query = query.lower()
    text = text.lower()

    # 简单的子串匹配
    if query in text:
        return True

    # 模糊匹配：查询中的每个字符都按顺序出现在文本中
    query_index = 0
    for char in text:
        if query_index < len(query) and char == query[query_index]:
            query_index += 1
        if query_index == len(query):
            return True

    return query_index == len(query)


def highlight_match(text: str, query: str) -> str:
    """
    高亮匹配的文本（返回带标记的文本）

    Args:
        text: 原始文本
        query: 搜索查询

    Returns:
        带高亮标记的文本
    """
    if not query:
        return text

    # 简单实现：返回原文本
    # 实际高亮需要在显示层面处理
    return text
