"""
Markdown+math -> tile PNGs, rendered locally with Pillow + matplotlib mathtext.

This mirrors the VPS's server-side render (evens/server/render/tiles.ts:
headless Chromium + sharp) without needing a browser. matplotlib's mathtext
parser handles the LaTeX-like math the solver emits (\\frac, \\sqrt, ^, _,
greek letters, ...) and PIL lays out the surrounding markdown text.

This replaced an attempt at rendering client-side in the glasses app's
WebView (marked + KaTeX + html2canvas): that took minutes and often produced
nothing, because html2canvas reimplements CSS layout in JS instead of using
the browser's native renderer, and mobile WebViews choke on it. Rendering
here instead means the client never sees an empty /tiles response and never
has to fall back to that path.

Scope is deliberately narrow: FORMAT_PROMPT in solver.py only ever emits
`## N` headings, plain paragraphs, `**bold**`, and `$...$` / `$$...$$` math
("No tables" is one of its rules) — so that's what this renders. Anything
else (code fences, links, images) is treated as plain text.
"""

import base64
import os
import re

try:
    from PIL import Image, ImageDraw, ImageFont

    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import mathtext

    RENDER_AVAILABLE = True
except ImportError:
    RENDER_AVAILABLE = False

# ── geometry — must match evens/test/src/constants.ts ───────────────────────

TILE_W, TILE_H = 288, 126
TILES_X, TILES_Y = 2, 2
PAGE_W, PAGE_H = TILE_W * TILES_X, TILE_H * TILES_Y  # 576, 252
RENDER_WIDTH = PAGE_W
PAD_X, PAD_Y = 16, 12
MAX_HEIGHT = 20000  # generous scratch canvas; cropped to content afterward

FONT_BODY, FONT_H1, FONT_H2, FONT_H3 = 21, 26, 23, 21
LINE_GAP = 6  # extra pixels between wrapped lines, on top of font ascent+descent

WHITE, BLACK, DIM = 255, 0, 208
GRAY_LEVELS = 16

_fonts: dict = {}


def _font(style: str, size: int):
    key = (style, size)
    cached = _fonts.get(key)
    if cached is not None:
        return cached
    base = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
    name = {
        "regular": "DejaVuSans.ttf",
        "bold": "DejaVuSans-Bold.ttf",
    }[style]
    f = ImageFont.truetype(os.path.join(base, name), size)
    _fonts[key] = f
    return f


def _render_math(tex: str, fontsize: int, dpi: int = 110):
    """TeX -> (RGBA image, descent-from-baseline px), or None on parse failure.

    Reimplements mathtext.math_to_image() (the stable public entry point;
    the raw MathTextParser("bitmap")/to_rgba combo used in older matplotlib
    was removed) but with a transparent figure background — math_to_image
    always renders onto opaque white, which is invisible against white text.
    Depth comes back in 72-dpi points and is rescaled to match the image's
    actual pixels.
    """
    import io

    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties

    try:
        s = f"${tex}$"
        prop = FontProperties(size=fontsize)
        width, height, depth, _, _ = mathtext.MathTextParser("path").parse(s, dpi=72, prop=prop)

        fig = Figure(figsize=(width / 72.0, height / 72.0))
        fig.patch.set_alpha(0)
        fig.text(0, depth / height, s, fontproperties=prop, color="white")

        buf = io.BytesIO()
        fig.savefig(buf, dpi=dpi, format="png", transparent=True)
        buf.seek(0)
        img = Image.open(buf)
        img.load()
        depth_px = round(depth * dpi / 72)
        return img, depth_px
    except Exception:
        return None, 0


# ── markdown -> blocks ───────────────────────────────────────────────────────
# Same two-pass trick as evens/test/src/render/markdown.ts: pull math out
# first (so it can't be mangled), replace each span with an inert token,
# split into blocks, then resolve tokens back to rendered content per block.

_DISPLAY_MATH_RE = re.compile(r"\$\$([\s\S]+?)\$\$")
_INLINE_MATH_RE = re.compile(r"\$(?!\$)((?:\\.|[^$\\])+?)\$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


class Block:
    __slots__ = ("kind", "text", "level")

    def __init__(self, kind: str, text: str, level: int = 0):
        self.kind = kind  # "heading" | "paragraph" | "display_math" | "list_item"
        self.text = text
        self.level = level


def _extract_math(src: str):
    """Pull out $$...$$ and $...$, returning (text-with-tokens, {token: (tex, display)})."""
    spans: dict[str, tuple[str, bool]] = {}

    def stash_display(m):
        token = f"\x00MATH{len(spans)}\x00"
        spans[token] = (m.group(1).strip(), True)
        return f"\n\n{token}\n\n"

    text = _DISPLAY_MATH_RE.sub(stash_display, src)

    def stash_inline(m):
        token = f"\x00MATH{len(spans)}\x00"
        spans[token] = (m.group(1).strip(), False)
        return token

    text = _INLINE_MATH_RE.sub(stash_inline, text)
    return text, spans


def _split_blocks(text: str, math: dict) -> list[Block]:
    blocks: list[Block] = []
    for raw in re.split(r"\n\s*\n", text):
        raw = raw.strip("\n")
        if not raw.strip():
            continue
        stripped = raw.strip()

        if stripped in math and math[stripped][1]:
            blocks.append(Block("display_math", math[stripped][0]))
            continue

        m = re.match(r"^(#{1,3})\s+(.*)", stripped)
        if m:
            blocks.append(Block("heading", m.group(2).strip(), level=len(m.group(1))))
            continue

        lines = stripped.split("\n")
        if all(re.match(r"^[-*]\s+", ln) or not ln.strip() for ln in lines):
            for ln in lines:
                if ln.strip():
                    blocks.append(Block("list_item", re.sub(r"^[-*]\s+", "", ln.strip())))
            continue

        blocks.append(Block("paragraph", " ".join(ln.strip() for ln in lines)))
    return blocks


# ── inline layout: words and math atoms flowed with baseline alignment ──────


class Atom:
    __slots__ = ("kind", "text", "font", "image", "descent", "width")

    def __init__(self, kind, text="", font=None, image=None, descent=0):
        self.kind = kind  # "word" | "math"
        self.text = text
        self.font = font
        self.image = image
        self.descent = descent
        if kind == "word":
            bbox = font.getbbox(text)
            self.width = bbox[2] - bbox[0]
        else:
            self.width = image.width

    def ascent_descent(self):
        if self.kind == "word":
            asc, desc = self.font.getmetrics()
            return asc, desc
        return self.image.height - self.descent, self.descent


def _inline_atoms(text: str, math: dict, font_size: int) -> list[Atom]:
    """Tokenize a block's text into word/math atoms, honoring **bold** and math tokens."""
    atoms: list[Atom] = []

    def add_words(segment: str, font):
        for word in segment.split():
            atoms.append(Atom("word", text=word, font=font))

    # Split on bold spans first, then resolve math tokens within each piece.
    pos = 0
    pieces: list[tuple[str, bool]] = []  # (text, is_bold)
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            pieces.append((text[pos:m.start()], False))
        pieces.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        pieces.append((text[pos:], False))

    for piece, bold in pieces:
        font = _font("bold" if bold else "regular", font_size)
        cursor = 0
        for m in re.finditer(r"\x00MATH\d+\x00", piece):
            if m.start() > cursor:
                add_words(piece[cursor:m.start()], font)
            tex, _display = math.get(m.group(0), (None, False))
            if tex is not None:
                # dpi=90 + fontsize-3 empirically matches DejaVuSans's glyph
                # size at font_size (mathtext glyphs otherwise render visibly
                # larger than body text at the same nominal point size).
                img, depth = _render_math(tex, max(9, font_size - 3), dpi=90)
                if img is not None:
                    atoms.append(Atom("math", image=img, descent=depth))
                else:
                    add_words(tex, font)
            cursor = m.end()
        if cursor < len(piece):
            add_words(piece[cursor:], font)

    return atoms


def _paste_math(canvas: "Image.Image", img: "Image.Image", x: int, top: int) -> None:
    """Stamp a rendered-math RGBA image onto the canvas, white-on-black.

    The glyph shape lives entirely in the alpha channel (see _render_math);
    pasting solid white through it as a mask keeps mathtext's own
    anti-aliasing instead of collapsing it to a hard threshold.
    """
    canvas.paste(WHITE, (x, top), mask=img.getchannel("A"))


def _flow(canvas: "Image.Image", atoms: list[Atom], x0: int, y0: int, avail_w: int) -> int:
    """Draw baseline-aligned wrapped atoms starting at (x0, y0). Returns new y."""
    if not atoms:
        return y0

    draw = ImageDraw.Draw(canvas)
    space_w = _font("regular", FONT_BODY).getbbox(" ")[2]
    y = y0
    line: list[Atom] = []
    line_w = 0

    def flush():
        nonlocal y, line, line_w
        if not line:
            return
        max_asc = max(a.ascent_descent()[0] for a in line)
        max_desc = max(a.ascent_descent()[1] for a in line)
        x = x0
        for a in line:
            asc, _desc = a.ascent_descent()
            top = y + (max_asc - asc)
            if a.kind == "word":
                draw.text((x, top), a.text, font=a.font, fill=WHITE)
            else:
                _paste_math(canvas, a.image, x, top)
            x += a.width + space_w
        y += max_asc + max_desc + LINE_GAP
        line = []
        line_w = 0

    for atom in atoms:
        w = atom.width + space_w
        if line and line_w + w > avail_w:
            flush()
        line.append(atom)
        line_w += w
    flush()
    return y


def _flow_display_math(canvas: "Image.Image", tex: str, y0: int) -> int:
    img, _depth = _render_math(tex, FONT_BODY + 6)
    if img is None:
        return _flow(canvas, _inline_atoms(tex, {}, FONT_BODY), PAD_X, y0, RENDER_WIDTH - 2 * PAD_X)
    x = (RENDER_WIDTH - img.width) // 2
    _paste_math(canvas, img, x, y0)
    return y0 + img.height + 12


# ── top-level render ─────────────────────────────────────────────────────────


def render_markdown_to_image(markdown: str) -> "Image.Image":
    """Markdown -> a single tall grayscale ('L') image, RENDER_WIDTH wide, black bg."""
    text, math = _extract_math(markdown)
    blocks = _split_blocks(text, math)

    canvas = Image.new("L", (RENDER_WIDTH, MAX_HEIGHT), BLACK)

    y = PAD_Y
    for block in blocks:
        if block.kind == "heading":
            size = {1: FONT_H1, 2: FONT_H2, 3: FONT_H3}[block.level]
            atoms = _inline_atoms(block.text, math, size)
            for a in atoms:
                if a.kind == "word":
                    a.font = _font("bold", size)
            y += 6
            y = _flow(canvas, atoms, PAD_X, y, RENDER_WIDTH - 2 * PAD_X)
            y += 6
        elif block.kind == "display_math":
            y += 6
            y = _flow_display_math(canvas, block.text, y)
        elif block.kind == "list_item":
            atoms = _inline_atoms(block.text, math, FONT_BODY)
            bullet = Atom("word", text="•", font=_font("regular", FONT_BODY))
            y = _flow(canvas, [bullet] + atoms, PAD_X + 12, y, RENDER_WIDTH - 2 * PAD_X - 12)
        else:  # paragraph
            atoms = _inline_atoms(block.text, math, FONT_BODY)
            y = _flow(canvas, atoms, PAD_X, y, RENDER_WIDTH - 2 * PAD_X)
            y += 4

        if y > MAX_HEIGHT - 400:
            break  # bail rather than overrun the scratch canvas

    content_h = min(MAX_HEIGHT, y + PAD_Y)
    return canvas.crop((0, 0, RENDER_WIDTH, content_h))


def _quantize_gray(img: "Image.Image") -> "Image.Image":
    """Snap to GRAY_LEVELS evenly spaced shades — matches the VPS's 16-colour
    greyscale PNG (see encodeTile in evens/server/render/tiles.ts) so payload
    size over BLE stays comparable."""
    step = 255 // (GRAY_LEVELS - 1)
    lut = [round(round(i / step) * step) for i in range(256)]
    return img.point(lut)


def _tile_png_b64(page_img: "Image.Image", tx: int, ty: int) -> str:
    import io

    tile = Image.new("L", (TILE_W, TILE_H), BLACK)
    sx, sy = tx * TILE_W, ty * TILE_H
    crop = page_img.crop((sx, sy, sx + TILE_W, sy + TILE_H))
    tile.paste(crop, (0, 0))
    tile = _quantize_gray(tile)
    buf = io.BytesIO()
    tile.convert("P").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def render_markdown_to_tiles(markdown: str) -> list[dict]:
    """Markdown -> [{"tiles": [{"index": 0..3, "data": base64 png}, ...]}, ...]."""
    if not markdown.strip():
        return []

    content = render_markdown_to_image(markdown)
    page_count = max(1, -(-content.height // PAGE_H))  # ceil div

    padded = Image.new("L", (RENDER_WIDTH, page_count * PAGE_H), BLACK)
    padded.paste(content, (0, 0))

    pages = []
    for p in range(page_count):
        page_img = padded.crop((0, p * PAGE_H, RENDER_WIDTH, (p + 1) * PAGE_H))
        tiles = []
        for ty in range(TILES_Y):
            for tx in range(TILES_X):
                tiles.append({
                    "index": ty * TILES_X + tx,
                    "data": _tile_png_b64(page_img, tx, ty),
                })
        pages.append({"tiles": tiles})
    return pages
