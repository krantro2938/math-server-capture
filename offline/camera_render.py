"""
Camera frame -> tile PNGs, for the offline GET /assignment/camera endpoint.

A Python port of evens/server/render/camera.ts's photo/ink pipeline (sharp)
onto PIL, the same substitution render.py already made for the document
tiles. Scope is narrower than the VPS version:

  - No reserved-rect compositing for the menu overlay. The client never
    actually requests it while the menu is open (camera.ts's previewTick
    bails before the fetch when the menu is up), so there is nothing to
    reserve for.
  - `photo` mode uses PIL's global autocontrast rather than sharp's windowed
    CLAHE. Locally-equalised contrast needs either a real CLAHE
    implementation or numpy, and this stack deliberately avoids adding a
    numpy dependency for the same reason render.py avoids one: Termux
    package installs, not pip C-extension builds. The result is a plausible
    but less locally-adaptive photo preview; `ink` mode (the default, and
    the one actually meant for reading a page) is unaffected — it needs no
    equalisation at all.

See render/camera.ts for the full rationale behind the ink/photo split and
every constant below; the numbers are copied from there so the two produce
comparable-looking output.
"""

import io

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat
    CAMERA_RENDER_AVAILABLE = True
except ImportError:
    CAMERA_RENDER_AVAILABLE = False

import render

# ── photo mode ───────────────────────────────────────────────────────────────

TARGET_MEAN = 80
MIN_GAIN, MAX_GAIN = 0.35, 2.0

# ── ink mode ─────────────────────────────────────────────────────────────────

INK_SIGMA = 4
INK_FLOOR = 3
INK_MIN_SPAN = 16
INK_PERCENTILE = 0.99
INK_GAMMA = 0.8
INK_DESPECKLE = 3

BORDER = 3

PREVIEW_MODES = ("ink", "photo")


def _fit(img: "Image.Image", w: int, h: int):
    """Resize preserving aspect ratio into w x h; returns (resized, drawn_w, drawn_h, left, top)."""
    src_w, src_h = img.size
    scale = min(w / src_w, h / src_h)
    drawn_w = max(1, round(src_w * scale))
    drawn_h = max(1, round(src_h * scale))
    resized = img.resize((drawn_w, drawn_h))
    left = (w - drawn_w) // 2
    top = (h - drawn_h) // 2
    return resized, drawn_w, drawn_h, left, top


def _photo_pass(grey: "Image.Image"):
    """PHOTO: equalised and pinned to TARGET_MEAN — see camera.ts's photoPass."""
    eq = ImageOps.autocontrast(grey, cutoff=1)
    mean = ImageStat.Stat(eq).mean[0] or TARGET_MEAN
    gain = min(MAX_GAIN, max(MIN_GAIN, TARGET_MEAN / mean))
    lut = [min(255, round(i * gain)) for i in range(256)]
    return eq.point(lut), None


def _ink_pass(grey: "Image.Image"):
    """INK: marks on black — subtract the frame's own local background from
    it and keep what is darker than its surroundings. See camera.ts's
    inkPass for the full rationale; the constants above are copied from there."""
    despeckled = grey.filter(ImageFilter.MedianFilter(INK_DESPECKLE))
    background = despeckled.filter(ImageFilter.GaussianBlur(INK_SIGMA))
    # clip(background - despeckled, 0, 255) — how far each pixel sits below
    # its own neighbourhood, i.e. how much of a mark it is.
    depth = ImageChops.subtract(background, despeckled)

    hist = depth.histogram()
    total = sum(hist)
    target = total * INK_PERCENTILE
    seen = 0
    top = 255
    for level, count in enumerate(hist):
        seen += count
        if seen >= target:
            top = level
            break

    span = max(INK_MIN_SPAN, top - INK_FLOOR)
    lut = []
    for level in range(256):
        t = min(1.0, max(0.0, (level - INK_FLOOR) / span))
        lut.append(round(255 * (t ** INK_GAMMA)))
    return depth.point(lut), top


def _draw_border(img: "Image.Image") -> "Image.Image":
    """A hairline box round the drawn frame — the only way to tell the edge
    of the camera's field of view from the letterbox (or a dark room) beyond it."""
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for t in range(BORDER):
        draw.rectangle([t, t, w - 1 - t, h - 1 - t], outline=255)
    return img


def _fitted(jpeg: bytes, w: int, h: int, rotate: int, mode: str):
    img = Image.open(io.BytesIO(jpeg))
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    if rotate:
        # sharp's .rotate(deg) turns clockwise for positive deg; PIL's turns
        # counter-clockwise, hence the sign flip.
        img = img.rotate(-rotate, expand=True, fillcolor=0)

    resized, drawn_w, drawn_h, left, top = _fit(img, w, h)
    pass_img, contrast = (_photo_pass if mode == "photo" else _ink_pass)(resized)
    pass_img = _draw_border(pass_img)

    canvas = Image.new("L", (w, h), 0)
    canvas.paste(pass_img, (left, top))
    return canvas, contrast


def render_camera_tiles(jpeg: bytes, size: int = 4, rotate: int = 0, mode: str = "ink") -> dict:
    """jpeg bytes -> {"tiles": [...], "size", "rotate", "mode", "contrast"}.

    Matches the shape of camera.ts's PreviewResponse (see evens/test/src/camera.ts).
    """
    if mode not in PREVIEW_MODES:
        mode = "ink"
    rotate = rotate % 360

    if size == 1:
        page, contrast = _fitted(jpeg, render.TILE_W, render.TILE_H, rotate, mode)
        tiles = [{"index": 0, "data": render._tile_png_b64(page, 0, 0)}]
        return {"tiles": tiles, "size": 1, "rotate": rotate, "mode": mode, "contrast": contrast}

    page, contrast = _fitted(jpeg, render.PAGE_W, render.PAGE_H, rotate, mode)
    tiles = []
    for ty in range(render.TILES_Y):
        for tx in range(render.TILES_X):
            tiles.append({
                "index": ty * render.TILES_X + tx,
                "data": render._tile_png_b64(page, tx, ty),
            })
    return {"tiles": tiles, "size": 4, "rotate": rotate, "mode": mode, "contrast": contrast}
