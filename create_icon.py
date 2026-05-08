"""
简化的图标生成脚本
如果 PIL 不可用，创建一个基本的 ICO 文件
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def create_simple_icon():
    """创建简单的图标"""
    try:
        from PIL import Image, ImageDraw, ImageFont

        # 创建多个尺寸的图标
        sizes = [16, 32, 48, 64, 128, 256]
        images = []

        for size in sizes:
            # 创建图像
            img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # 绘制渐变背景
            for i in range(size):
                # 从深蓝到浅蓝的渐变
                r = int(41 + (100 - 41) * i / size)
                g = int(128 + (181 - 128) * i / size)
                b = int(185 + (255 - 185) * i / size)
                draw.rectangle([0, i, size, i+1], fill=(r, g, b, 255))

            # 绘制圆角矩形边框
            border_width = max(2, size // 32)
            draw.rounded_rectangle(
                [border_width, border_width, size-border_width, size-border_width],
                radius=size//8,
                outline=(255, 255, 255, 200),
                width=border_width
            )

            # 绘制文字 "API"
            try:
                font_size = size // 3
                try:
                    font = ImageFont.truetype("arial.ttf", font_size)
                except:
                    try:
                        font = ImageFont.truetype("C:\\Windows\\Fonts\\arial.ttf", font_size)
                    except:
                        font = ImageFont.load_default()

                text = "API"
                bbox = draw.textbbox((0, 0), text, font=font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]

                x = (size - text_width) // 2
                y = (size - text_height) // 2 - bbox[1]

                # 绘制文字阴影
                draw.text((x+1, y+1), text, fill=(0, 0, 0, 100), font=font)
                # 绘制文字
                draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)

            except Exception as e:
                print(f"Warning: Could not add text to {size}x{size} icon: {e}")

            images.append(img)

        # 保存为 ICO 文件
        output_path = "icon.ico"
        images[0].save(
            output_path,
            format='ICO',
            sizes=[(img.width, img.height) for img in images],
            append_images=images[1:]
        )

        print(f"✓ 图标已创建: {output_path}")

        # 同时保存一个 PNG 版本用于预览
        images[-1].save("icon.png", format='PNG')
        print(f"✓ PNG 预览已创建: icon.png")

        return True

    except ImportError:
        print("✗ PIL/Pillow 未安装")
        print("正在尝试安装...")
        try:
            import subprocess
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pillow"])
            print("✓ Pillow 安装成功，请重新运行此脚本")
            return False
        except:
            print("✗ 无法安装 Pillow")
            print("请手动安装: pip install pillow")
            return False
    except Exception as e:
        print(f"✗ 创建图标失败: {e}")
        return False


def create_icon():
    """Compatibility wrapper used by build_exe.py."""
    return create_simple_icon()

if __name__ == "__main__":
    if create_icon():
        print("\n图标创建成功！")
    else:
        print("\n图标创建失败，打包时将使用默认图标。")
