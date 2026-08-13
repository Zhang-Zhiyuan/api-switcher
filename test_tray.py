"""
测试系统托盘功能

使用方法:
1. 先安装依赖: pip install pystray
2. 运行此脚本: python test_tray.py
3. 测试功能:
   - 应用启动后会在系统托盘显示图标
   - 点击关闭按钮会最小化到托盘（而不是退出）
   - 右键托盘图标可以看到菜单
   - 菜单中可以快速切换配置
   - 选择"退出"才会真正关闭应用
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)

def main():
    try:
        import customtkinter as ctk
        from core.tray_manager import TrayManager

        logger.info("Testing tray functionality...")

        # Set appearance
        ctk.set_default_color_theme("blue")
        ctk.set_appearance_mode("dark")

        # Create a simple test window
        root = ctk.CTk()
        root.title("托盘功能测试")
        root.geometry("400x300")

        # Create label
        label = ctk.CTkLabel(
            root,
            text="托盘功能测试\n\n点击关闭按钮会最小化到托盘\n右键托盘图标查看菜单",
            font=("Arial", 14)
        )
        label.pack(expand=True, pady=20)

        # Create tray manager
        def show_window(icon=None, item=None):
            root.deiconify()
            root.lift()
            root.focus_force()
            logger.info("Window restored")

        def exit_app():
            logger.info("Exiting...")
            tray_manager.stop()
            root.quit()
            root.destroy()

        tray_manager = TrayManager(
            on_show_window=show_window,
            on_exit=exit_app
        )

        # Handle window close
        def on_closing():
            root.withdraw()
            logger.info("Window minimized to tray")

        root.protocol("WM_DELETE_WINDOW", on_closing)

        # Start tray
        tray_manager.start()
        logger.info("Tray icon started")

        # Add test button
        def test_update():
            tray_manager.update_menu()
            logger.info("Menu updated")

        btn = ctk.CTkButton(root, text="更新托盘菜单", command=test_update)
        btn.pack(pady=10)

        # Run
        root.mainloop()

    except ImportError as e:
        logger.error(f"Import error: {e}")
        logger.error("请先安装依赖: pip install pystray")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
