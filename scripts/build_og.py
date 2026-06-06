"""Build OG preview: marketing mockup with real dashboard screenshot inside."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(
    r"C:\Users\ex\.cursor\projects\c-Users-ex-Desktop-exsenderV2-main\assets"
    r"\c__Users_ex_AppData_Roaming_Cursor_User_workspaceStorage_4db4e2c5c780358635baedd23dd80c5e"
    r"_images_image-886b13ee-d095-4fe7-a286-8113aa9110f1.png"
)
SRC_FALLBACK = ROOT / "frontend" / "og" / "source-dashboard.png"
OUT = ROOT / "frontend" / "og" / "og-image.png"
W, H = 1200, 630


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"]
        if bold
        else ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]
    )
    for path in names:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    return mask


def paste_rounded(base: Image.Image, img: Image.Image, xy: tuple[int, int], radius: int) -> None:
    w, h = img.size
    mask = rounded_mask((w, h), radius)
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.paste(img, xy, mask)
    base.alpha_composite(layer)


def draw_gradient_bg() -> Image.Image:
    canvas = Image.new("RGBA", (W, H), (5, 5, 5, 255))
    draw = ImageDraw.Draw(canvas)

    for i in range(H):
        t = i / H
        r = int(8 + 18 * (1 - t))
        g = int(8 + 10 * (1 - t))
        b = int(10 + 28 * (1 - t))
        draw.line([(0, i), (W, i)], fill=(r, g, b, 255))

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse((680, -120, 1280, 420), fill=(255, 255, 255, 18))
    gdraw.ellipse((-80, 320, 520, 760), fill=(120, 120, 255, 12))
    glow = glow.filter(ImageFilter.GaussianBlur(60))
    canvas = Image.alpha_composite(canvas, glow)

    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(grid)
    step = 48
    for x in range(0, W, step):
        g.line([(x, 0), (x, H)], fill=(255, 255, 255, 6))
    for y in range(0, H, step):
        g.line([(0, y), (W, y)], fill=(255, 255, 255, 6))
    return Image.alpha_composite(canvas, grid)


def draw_browser_mockup(screenshot: Image.Image) -> Image.Image:
    frame_x, frame_y = 430, 52
    frame_w, frame_h = 730, 520
    chrome_h = 46
    radius = 18

    frame = Image.new("RGBA", (frame_w, frame_h), (0, 0, 0, 0))
    fdraw = ImageDraw.Draw(frame)

    shadow = Image.new("RGBA", (frame_w + 80, frame_h + 80), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle(
        (30, 30, frame_w + 30, frame_h + 30),
        radius=radius + 4,
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))

    fdraw.rounded_rectangle((0, 0, frame_w - 1, frame_h - 1), radius=radius, fill=(18, 18, 20, 255))
    fdraw.rounded_rectangle((0, 0, frame_w - 1, frame_h - 1), radius=radius, outline=(55, 55, 58, 255), width=2)

    fdraw.rectangle((0, 0, frame_w, chrome_h + 8), fill=(24, 24, 26, 255))
    fdraw.rounded_rectangle((0, 0, frame_w - 1, chrome_h + 14), radius=radius, fill=(24, 24, 26, 255))

    dots = [(18, 23), (38, 23), (58, 23)]
    colors = [(255, 95, 86, 255), (255, 189, 46, 255), (39, 201, 63, 255)]
    for (dx, dy), col in zip(dots, colors):
        fdraw.ellipse((dx - 6, dy - 6, dx + 6, dy + 6), fill=col)

    url_x, url_y, url_w, url_h = 92, 12, 360, 24
    fdraw.rounded_rectangle((url_x, url_y, url_x + url_w, url_y + url_h), radius=12, fill=(12, 12, 14, 255))
    fdraw.rounded_rectangle(
        (url_x, url_y, url_x + url_w, url_y + url_h),
        radius=12,
        outline=(45, 45, 48, 255),
        width=1,
    )

    font = load_font(13)
    fdraw.text((url_x + 12, url_y + 4), "exsender.top/app", fill=(170, 170, 175, 255), font=font)

    content_w = frame_w - 16
    content_h = frame_h - chrome_h - 12
    sw, sh = screenshot.size
    scale = min(content_w / sw, content_h / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    shot = screenshot.resize((nw, nh), Image.Resampling.LANCZOS)
    paste_x = (frame_w - nw) // 2
    paste_y = chrome_h + (content_h - nh) // 2

    content = Image.new("RGBA", (frame_w, frame_h), (0, 0, 0, 0))
    content_mask = rounded_mask((nw, nh), 10)
    content.paste(shot, (paste_x, paste_y), content_mask)
    frame.alpha_composite(content)

    fdraw.line([(12, chrome_h + 2), (frame_w - 12, chrome_h + 2)], fill=(40, 40, 44, 255), width=1)

    return shadow, frame, (frame_x, frame_y)


def draw_branding(canvas: Image.Image) -> None:
    draw = ImageDraw.Draw(canvas)
    title = load_font(52, bold=True)
    subtitle = load_font(24, bold=True)
    body = load_font(18)

    draw.rounded_rectangle((52, 58, 92, 98), radius=10, fill=(255, 255, 255, 255))
    draw.text((66, 64), "e", fill=(5, 5, 5, 255), font=load_font(22, bold=True))

    draw.text((108, 62), "exsender", fill=(255, 255, 255, 255), font=load_font(28, bold=True))

    draw.text((52, 150), "Панель", fill=(255, 255, 255, 255), font=title)
    draw.text((52, 210), "управления", fill=(255, 255, 255, 255), font=title)
    draw.text((52, 270), "рассылками", fill=(220, 220, 220, 255), font=title)

    draw.text((54, 350), "Telegram · VDS · прокси · расписание", fill=(150, 150, 158, 255), font=body)

    draw.text((52, 520), "exsender.top", fill=(110, 110, 118, 255), font=subtitle)


def resolve_source() -> Path:
    if SRC.is_file():
        return SRC
    if SRC_FALLBACK.is_file():
        return SRC_FALLBACK
    raise SystemExit(f"Screenshot not found: {SRC}")


def main() -> None:
    src_path = resolve_source()
    screenshot = Image.open(src_path).convert("RGB")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if src_path != SRC_FALLBACK:
        screenshot.save(SRC_FALLBACK, "PNG", optimize=True)

    canvas = draw_gradient_bg()
    shadow, frame, (fx, fy) = draw_browser_mockup(screenshot)
    canvas.alpha_composite(shadow, (fx - 30, fy - 30))
    canvas.alpha_composite(frame, (fx, fy))
    draw_branding(canvas)

    canvas.convert("RGB").save(OUT, "PNG", optimize=True)
    print(f"OK {OUT} ({W}x{H}) mockup from {screenshot.size[0]}x{screenshot.size[1]}")


if __name__ == "__main__":
    main()
