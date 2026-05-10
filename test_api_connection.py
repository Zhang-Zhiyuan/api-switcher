"""
测试 API 连接功能

使用方法:
1. 运行此脚本: python test_api_connection.py
2. 测试功能:
   - 测试 Claude API 连接
   - 测试 OpenAI API 连接
   - 查看测试结果对话框
"""

import sys
import os

if __name__ != "__main__":
    import pytest

    pytest.skip(
        "Manual live API/GUI test; run python test_api_connection.py when needed.",
        allow_module_level=True,
    )

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)

def test_claude_api():
    """测试 Claude API"""
    from core.api_tester import APITester

    print("\n" + "="*60)
    print("测试 Claude API")
    print("="*60)

    # 测试参数（请替换为真实的 API Key）
    api_key = "sk-ant-api03-xxxxx"  # 替换为真实的 API Key
    base_url = "https://api.anthropic.com"
    model = "claude-opus-4-7"

    print(f"API Key: {api_key[:20]}...")
    print(f"Base URL: {base_url}")
    print(f"Model: {model}")
    print("\n正在测试...")

    result = APITester.test_claude_api(api_key, base_url, model, timeout=10)

    print(f"\n结果: {'✓ 成功' if result.success else '✗ 失败'}")
    print(f"消息: {result.message}")
    if result.response_time:
        print(f"响应时间: {result.response_time:.0f} ms")
    if result.status_code:
        print(f"状态码: {result.status_code}")
    if result.error_details:
        print(f"错误详情: {result.error_details}")

def test_openai_api():
    """测试 OpenAI API"""
    from core.api_tester import APITester

    print("\n" + "="*60)
    print("测试 OpenAI API")
    print("="*60)

    # 测试参数（请替换为真实的 API Key）
    api_key = "sk-xxxxx"  # 替换为真实的 API Key
    base_url = "https://api.openai.com"
    model = "gpt-4"

    print(f"API Key: {api_key[:20]}...")
    print(f"Base URL: {base_url}")
    print(f"Model: {model}")
    print("\n正在测试...")

    result = APITester.test_openai_api(api_key, base_url, model, timeout=10)

    print(f"\n结果: {'✓ 成功' if result.success else '✗ 失败'}")
    print(f"消息: {result.message}")
    if result.response_time:
        print(f"响应时间: {result.response_time:.0f} ms")
    if result.status_code:
        print(f"状态码: {result.status_code}")
    if result.error_details:
        print(f"错误详情: {result.error_details}")

def test_gui():
    """测试 GUI 对话框"""
    import customtkinter as ctk
    from core.api_tester import APITester, TestResult
    from ui.dialogs.api_test_result_dialog import APITestResultDialog

    print("\n" + "="*60)
    print("测试 GUI 对话框")
    print("="*60)

    ctk.set_default_color_theme("blue")
    ctk.set_appearance_mode("dark")

    root = ctk.CTk()
    root.title("API 测试")
    root.geometry("400x300")

    label = ctk.CTkLabel(
        root,
        text="点击按钮测试 API 连接",
        font=("Arial", 14)
    )
    label.pack(expand=True, pady=20)

    def show_success_result():
        result = TestResult(
            success=True,
            message="✓ 连接成功",
            response_time=234.5,
            status_code=200
        )
        APITestResultDialog(root, result, "测试配置")

    def show_error_result():
        result = TestResult(
            success=False,
            message="✗ 认证失败: API Key 无效",
            response_time=156.2,
            status_code=401,
            error_details="Invalid API key provided"
        )
        APITestResultDialog(root, result, "测试配置")

    btn_success = ctk.CTkButton(
        root,
        text="显示成功结果",
        command=show_success_result
    )
    btn_success.pack(pady=10)

    btn_error = ctk.CTkButton(
        root,
        text="显示失败结果",
        command=show_error_result
    )
    btn_error.pack(pady=10)

    root.mainloop()

def main():
    print("API 连接测试工具")
    print("="*60)
    print("1. 测试 Claude API（命令行）")
    print("2. 测试 OpenAI API（命令行）")
    print("3. 测试 GUI 对话框")
    print("4. 退出")
    print("="*60)

    while True:
        choice = input("\n请选择 (1-4): ").strip()

        if choice == "1":
            test_claude_api()
        elif choice == "2":
            test_openai_api()
        elif choice == "3":
            test_gui()
        elif choice == "4":
            print("退出")
            break
        else:
            print("无效选择，请重试")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已取消")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
