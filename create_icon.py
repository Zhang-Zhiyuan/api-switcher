"""Generate the API Switcher application icon.

The icon is drawn from vector-like primitives at high resolution so the ICO
contains crisp small sizes for the taskbar, tray, and Explorer.
"""

from __future__ import annotations

import sys
from pathlib import Path


PREVIEW_SIZE = 1024
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
ROOT = Path(__file__).resolve().parent


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _mix(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def _vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]):
    from PIL import Image

    img = Image.new("RGBA", (size, size))
    px = img.load()
    for y in range(size):
        t = y / max(1, size - 1)
        color = (
            _mix(top[0], bottom[0], t),
            _mix(top[1], bottom[1], t),
            _mix(top[2], bottom[2], t),
            255,
        )
        for x in range(size):
            px[x, y] = color
    return img


def _rounded_gradient(size: int, radius: int):
    from PIL import Image, ImageDraw

    gradient = _vertical_gradient(size, (9, 34, 58), (24, 126, 151))
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(gradient, (0, 0), mask)
    return out


def _arrow(
    draw,
    start: tuple[int, int],
    end: tuple[int, int],
    width: int,
    fill: tuple[int, int, int, int],
    head_length: int,
    head_width: int,
) -> None:
    x1, y = start
    x2, _ = end
    direction = 1 if x2 >= x1 else -1
    body_end = x2 - direction * head_length
    half = max(1, width // 2)

    left = min(x1, body_end)
    right = max(x1, body_end)
    draw.rounded_rectangle([left, y - half, right, y + half], radius=half, fill=fill)
    draw.ellipse([x1 - half, y - half, x1 + half, y + half], fill=fill)
    draw.polygon(
        [
            (x2, y),
            (body_end, y - head_width // 2),
            (body_end, y + head_width // 2),
        ],
        fill=fill,
    )


def _draw_switch_mark(draw, size: int) -> None:
    # Two opposing arrows read clearly at 16px and still feel detailed at 256px.
    left = round(size * 0.25)
    right = round(size * 0.76)
    top_y = round(size * 0.40)
    bottom_y = round(size * 0.62)

    under_width = max(10, round(size * 0.135))
    main_width = max(7, round(size * 0.085))
    head_length = max(16, round(size * 0.155))
    head_width = max(20, round(size * 0.215))

    accent = (42, 229, 192, 255)
    accent_shadow = (2, 24, 36, 115)
    white = (248, 253, 255, 255)

    offset = max(2, round(size * 0.018))
    _arrow(
        draw,
        (left, top_y + offset),
        (right, top_y + offset),
        under_width,
        accent_shadow,
        head_length,
        head_width,
    )
    _arrow(
        draw,
        (right, bottom_y + offset),
        (left, bottom_y + offset),
        under_width,
        accent_shadow,
        head_length,
        head_width,
    )

    _arrow(draw, (left, top_y), (right, top_y), under_width, accent, head_length, head_width)
    _arrow(draw, (right, bottom_y), (left, bottom_y), under_width, accent, head_length, head_width)
    _arrow(draw, (left, top_y), (right, top_y), main_width, white, head_length, head_width)
    _arrow(draw, (right, bottom_y), (left, bottom_y), main_width, white, head_length, head_width)

    node_radius = max(5, round(size * 0.045))
    for x, y in ((left, top_y), (right, bottom_y)):
        draw.ellipse(
            [x - node_radius, y - node_radius, x + node_radius, y + node_radius],
            fill=(255, 255, 255, 255),
        )
        inner = max(2, round(node_radius * 0.48))
        draw.ellipse([x - inner, y - inner, x + inner, y + inner], fill=(13, 70, 92, 255))


def render_icon(size: int):
    from PIL import Image, ImageDraw

    scale = 8 if size <= 128 else 4 if size <= 256 else 2
    canvas_size = size * scale
    radius = round(canvas_size * 0.22)
    img = _rounded_gradient(canvas_size, radius)
    draw = ImageDraw.Draw(img)

    inset = max(4, round(canvas_size * 0.035))
    border = max(3, round(canvas_size * 0.026))
    draw.rounded_rectangle(
        [inset, inset, canvas_size - inset - 1, canvas_size - inset - 1],
        radius=round(canvas_size * 0.185),
        outline=(255, 255, 255, 72),
        width=border,
    )

    glow_pad = round(canvas_size * 0.08)
    draw.rounded_rectangle(
        [
            glow_pad,
            round(canvas_size * 0.10),
            canvas_size - glow_pad,
            round(canvas_size * 0.83),
        ],
        radius=round(canvas_size * 0.16),
        outline=(63, 224, 196, 42),
        width=max(2, round(canvas_size * 0.018)),
    )

    _draw_switch_mark(draw, canvas_size)

    return img.resize((size, size), Image.Resampling.LANCZOS)


def create_icon() -> bool:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Pillow is required to generate the icon. Install it with: pip install pillow")
        return False

    preview = render_icon(PREVIEW_SIZE)
    png_path = ROOT / "icon.png"
    ico_path = ROOT / "icon.ico"

    preview.save(png_path, format="PNG", optimize=True)
    preview.save(ico_path, format="ICO", sizes=[(size, size) for size in ICO_SIZES])

    print(f"Icon PNG written: {png_path}")
    print(f"Icon ICO written: {ico_path}")
    print(f"ICO sizes: {', '.join(str(size) for size in ICO_SIZES)}")
    return True


def create_simple_icon() -> bool:
    """Compatibility wrapper for older callers."""
    return create_icon()


if __name__ == "__main__":
    raise SystemExit(0 if create_icon() else 1)
