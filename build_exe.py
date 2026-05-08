"""
打包应用程序为 EXE
使用 PyInstaller
"""
import os
import sys
import subprocess
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def check_pyinstaller():
    """检查 PyInstaller 是否已安装"""
    try:
        import PyInstaller
        print("✓ PyInstaller 已安装")
        return True
    except ImportError:
        print("✗ PyInstaller 未安装")
        print("正在安装 PyInstaller...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
            print("✓ PyInstaller 安装成功")
            return True
        except Exception as e:
            print(f"✗ PyInstaller 安装失败: {e}")
            return False

def create_spec_file():
    """创建 PyInstaller spec 文件"""
    datas = []
    for source, target in [
        ("config", "config"),
        ("assets", "assets"),
        ("PENDING_WORK.md", "."),
    ]:
        if Path(source).exists():
            datas.append((source, target))

    icon_line = "icon='icon.ico'," if Path("icon.ico").exists() else "icon=None,"

    spec_content = """# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=__DATAS__,
    hiddenimports=[
        'customtkinter',
        'PIL',
        'PIL._tkinter_finder',
        'tomli_w',
        'tomli',
        'tomllib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='API切换器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 不显示控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    __ICON_LINE__
)
""".replace("__DATAS__", repr(datas)).replace("__ICON_LINE__", icon_line)

    with open("API切换器.spec", "w", encoding="utf-8") as f:
        f.write(spec_content)

    print("✓ Spec 文件已创建: API切换器.spec")

def build_exe():
    """构建 EXE"""
    print("\n" + "="*80)
    print("开始打包...")
    print("="*80 + "\n")

    try:
        # 使用 spec 文件构建
        subprocess.check_call([
            sys.executable,
            "-m", "PyInstaller",
            "--clean",
            "--noconfirm",
            "API切换器.spec"
        ])

        print("\n" + "="*80)
        print("✓ 打包成功！")
        print("="*80)
        print(f"\nEXE 文件位置: {Path('dist/API切换器.exe').absolute()}")
        print("\n可以将 dist 文件夹中的内容分发给用户。")

    except subprocess.CalledProcessError as e:
        print(f"\n✗ 打包失败: {e}")
        return False

    return True

def main():
    """主函数"""
    print("="*80)
    print("API切换器 - 打包工具")
    print("="*80 + "\n")

    # 检查是否在正确的目录
    if not Path("main.py").exists():
        print("✗ 错误: 找不到 main.py")
        print("请在项目根目录运行此脚本")
        return

    # 检查图标是否存在
    if not Path("icon.ico").exists():
        print("正在创建图标...")
        try:
            import create_icon
            create_icon.create_icon()
        except Exception as e:
            print(f"✗ 创建图标失败: {e}")
            print("将使用默认图标")

    # 检查 PyInstaller
    if not check_pyinstaller():
        print("\n请手动安装 PyInstaller:")
        print("  pip install pyinstaller")
        return

    # 创建 spec 文件
    create_spec_file()

    # 构建 EXE
    if build_exe():
        print("\n" + "="*80)
        print("打包完成！")
        print("="*80)
        print("\n下一步:")
        print("1. 测试 dist/API切换器.exe")
        print("2. 将 dist 文件夹打包为 ZIP 分发")
        print("3. 或者使用 Inno Setup 创建安装程序")

if __name__ == "__main__":
    main()
