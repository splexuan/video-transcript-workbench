"""重新导出应用图标：assets/icon.png（1024）与 assets/icon.ico（多尺寸）。

设计稿是 assets/icon.svg（矢量源文件）；改动图形后同步改这里再执行：

    python assets/build_icon.py

依赖 Pillow（`pip install pillow`）。实现上按 4 倍超采样绘制再降采样，
让圆角与直边在 16px 下也不发毛。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent
MASTER = 1024
SUPERSAMPLE = 4
S = MASTER * SUPERSAMPLE

# 配色取自前端 token：--primary #a9502e / --primary-hover #92411f，纸色 #faf3ea
TILE_TOP = (190, 100, 62)
TILE_BOTTOM = (142, 60, 26)
GLYPH = (250, 243, 234, 255)
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def sc(value: float) -> float:
    return value * SUPERSAMPLE


def vertical_gradient(size: int) -> Image.Image:
    strip = Image.new("RGB", (1, size))
    for y in range(size):
        ratio = y / (size - 1)
        strip.putpixel((0, y), tuple(int(a + (b - a) * ratio) for a, b in zip(TILE_TOP, TILE_BOTTOM)))
    return strip.resize((size, size))


def build_master() -> Image.Image:
    """陶土圆角底 + 暖纸色的播放三角与三行文本（视频 → 文案）。"""

    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    tile_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tile_mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=sc(232), fill=255)
    canvas.paste(vertical_gradient(S).convert("RGBA"), (0, 0), tile_mask)

    glyph = Image.new("L", (S, S), 0)
    draw = ImageDraw.Draw(glyph)
    # 播放三角保持锐角：锐角处做圆角会外凸，缩到 16px 反而像「分享」图标
    draw.polygon([(sc(x), sc(y)) for x, y in ((342, 142), (342, 442), (682, 292))], fill=255)
    # 三行文本：宽 440 / 440 / 280，行高 88，行距 56
    for y, width in ((506, 440), (650, 440), (794, 280)):
        draw.rounded_rectangle(
            [sc(292), sc(y), sc(292 + width), sc(y + 88)], radius=sc(44), fill=255
        )
    canvas.paste(Image.new("RGBA", (S, S), GLYPH), (0, 0), glyph)

    return canvas.resize((MASTER, MASTER), Image.LANCZOS)


def main() -> None:
    icon = build_master()
    icon.save(ASSETS / "icon.png", format="PNG")
    icon.save(ASSETS / "icon.ico", format="ICO", sizes=ICO_SIZES)
    print(f"已生成：{ASSETS / 'icon.png'}、{ASSETS / 'icon.ico'}")


if __name__ == "__main__":
    main()
