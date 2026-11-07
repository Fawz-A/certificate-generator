"""Core certificate rendering — no Streamlit here, so it can be tested on its own."""

import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).parent / "fonts"


def bundled_fonts() -> dict[str, Path]:
    """Display name -> font file for every font shipped in fonts/."""
    out = {}
    for p in sorted(FONT_DIR.glob("*.[ot]tf")):
        name = re.sub(r"-Regular$", "", p.stem)
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)  # GreatVibes -> Great Vibes
        out[name] = p
    return out


def font_weights(font_path: str | Path) -> list[str]:
    """Named weights of a variable font, or [] for a static font."""
    try:
        f = ImageFont.truetype(str(font_path), 20)
        return [w.decode() for w in f.get_variation_names()]
    except Exception:
        return []


# ------------------------------------------------------------------ name lists

# Headings that mean "the person's name": Name, Full Name, Candidate Name,
# Participant's Name, Name of the Student, Delegate Name ...
_PERSON = r"(full|candidate|participant|student|delegate|attendee|recipient|awardee|member|employee|person|winner|volunteer)"
_NAME_HEADER = re.compile(
    rf"^\s*({_PERSON}\s*'?s?\s*)?(full\s*)?name\s*$"
    rf"|^\s*name\s+of\s+(the\s+)?{_PERSON}\s*$",
    re.I,
)
# "name" headings that are about something else.
_NOT_PERSON = re.compile(
    r"father|mother|guardian|parent|spouse|husband|wife|institut|college|school|universit"
    r"|company|organi[sz]ation|file|user|team|event|country|course|project|program|e-?mail|guide|mentor",
    re.I,
)
# Things that don't belong in a person's name: digits, emails, underscores, odd symbols.
_NOT_A_NAME = re.compile(r"[\d@_#/\\|<>{}\[\]=+*&%$!?;:]")


def looks_like_name(value: str) -> bool:
    v = value.strip()
    return bool(re.search(r"[^\W\d_]", v)) and not _NOT_A_NAME.search(v)


def find_name_column(df) -> str | None:
    """Pick the column holding people's names, by heading first, then by content."""
    cols = [str(c) for c in df.columns]

    def header_score(c: str) -> int:
        if _NAME_HEADER.match(c) and not _NOT_PERSON.search(c):
            return 3
        if re.search(r"\bname\b", c, re.I) and not _NOT_PERSON.search(c):
            return 2
        if re.fullmatch(rf"\s*{_PERSON}s?\s*", c, re.I):
            return 1
        return 0

    scored = sorted(((header_score(c), -i, c) for i, c in enumerate(cols)), reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][2]

    # No helpful heading: the column whose values most look like names.
    def content_score(c: str) -> float:
        vals = [str(v).strip() for v in df[c].dropna() if str(v).strip()][:200]
        return sum(looks_like_name(v) and " " in v for v in vals) / len(vals) if vals else 0

    best = max(cols, key=content_score, default=None)
    return best if best is not None and content_score(best) > 0.3 else None


# Headings for what the person is/was on the certificate, strongest match first.
_DESIG_HEADERS = [
    re.compile(r"designation|portfolio", re.I),
    re.compile(r"\b(position|role|post|title|rank|award)\b", re.I),
    re.compile(r"allot|country|committee|department|dept\b|category|track", re.I),
]


def find_designation_column(df, exclude: str | None = None) -> str | None:
    """Pick the designation/portfolio column by its heading (e.g. Designation,
    Portfolio, Role, Position, Allotted Country), never the name column."""
    cols = [str(c) for c in df.columns if str(c) != exclude]
    for pattern in _DESIG_HEADERS:
        for c in cols:
            if pattern.search(c):
                return c
    return None


_INST_HEADER = re.compile(
    r"institut|college|universit|school|organi[sz]ation|company|affiliation|employer|academy|campus", re.I)


def find_institution_column(df, exclude: str | None = None) -> str | None:
    """Pick the institution column by its heading (Institution, College, University ...)."""
    for c in (str(c) for c in df.columns):
        if c != exclude and _INST_HEADER.search(c):
            return c
    return None


def read_pdf_table(data: bytes):
    """Read the table(s) in a PDF into a DataFrame. The first row is the header.

    Rows that a page break split in two (blank first cell, mostly empty) are
    joined back onto the row above.
    """
    import pandas as pd

    doc = pymupdf.open(stream=data, filetype="pdf")
    rows: list[list[str]] = []
    for page in doc:
        for table in page.find_tables().tables:
            for r in table.rows:
                # Clip text per cell: table.extract() drops the spaces in this kind of PDF.
                rows.append([" ".join(page.get_text("text", clip=c).split()) if c else "" for c in r.cells])
    if not rows:
        return pd.DataFrame()

    header, body = rows[0], []
    for row in rows[1:]:
        row = (row + [""] * len(header))[: len(header)]
        if row == header:  # header repeated on a later page
            continue
        filled = sum(bool(c) for c in row)
        if body and not row[0] and filled <= len(row) / 2:
            body[-1] = [f"{a} {b}".strip() for a, b in zip(body[-1], row)]
        else:
            body.append(row)
    cols = [h or f"Column {i + 1}" for i, h in enumerate(header)]
    return pd.DataFrame(body, columns=cols)


def load_template(data: bytes, filename: str, pdf_dpi: int = 200) -> tuple[Image.Image, int]:
    """Return (RGB image, dpi). PDFs are rendered from their first page."""
    if filename.lower().endswith(".pdf"):
        doc = pymupdf.open(stream=data, filetype="pdf")
        pix = doc[0].get_pixmap(dpi=pdf_dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return img, pdf_dpi
    img = Image.open(io.BytesIO(data))
    dpi = int(img.info.get("dpi", (300, 300))[0]) or 300
    return img.convert("RGB"), dpi


@dataclass
class TextStyle:
    font_path: str
    weight: str | None   # variable-font weight name, e.g. "Bold"
    size: int            # font size in template pixels
    color: str           # "#rrggbb"
    x: float             # anchor (left edge / centre / right edge, per align), fraction of width
    y: float             # box centre, fraction of image height
    box_w: float         # box width, fraction of image width
    align: str           # "left" | "center" | "right"
    case: str = "As typed"  # "As typed" | "UPPERCASE" | "Title Case"


def _font(style: TextStyle, size: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(style.font_path, size)
    if style.weight:
        try:
            f.set_variation_by_name(style.weight)
        except Exception:
            pass
    return f


def apply_case(name: str, case: str) -> str:
    if case == "UPPERCASE":
        return name.upper()
    if case == "Title Case":
        return name.title()
    return name


def box_rect(img_size: tuple[int, int], style: TextStyle) -> tuple[int, int, int, int]:
    """The text box. (x, y) is where the text is anchored: its left edge, centre or
    right edge depending on the alignment, so a click puts the text where you clicked."""
    W, H = img_size
    bw, bh = style.box_w * W, style.size * 1.4
    ax, cy = style.x * W, style.y * H
    left = {"left": ax, "right": ax - bw}.get(style.align, ax - bw / 2)
    return int(left), int(cy - bh / 2), int(left + bw), int(cy + bh / 2)


def _draw_text(d: ImageDraw.ImageDraw, size_wh, text: str, style: TextStyle):
    """Draw one piece of text in its box. Text wider than the box shrinks to fit."""
    text = apply_case(text, style.case)
    left, top, right, bottom = box_rect(size_wh, style)
    size = style.size
    font = _font(style, size)
    while size > 8 and d.textlength(text, font=font) > right - left:
        size = int(size * 0.95)
        font = _font(style, size)
    cy = (top + bottom) / 2
    x, anchor = {"left": (left, "lm"), "right": (right, "rm")}.get(style.align, ((left + right) / 2, "mm"))
    d.text((x, cy), text, font=font, fill=style.color, anchor=anchor)


def text_sprite(img_size: tuple[int, int], text: str, style: TextStyle, scale: float = 1.0) -> Image.Image:
    """Just this field's text on a transparent background, sized to its box (plus
    half a box of headroom above and below for tall script letters), for the editor."""
    left, top, right, bottom = box_rect(img_size, style)
    bw, bh = max(1, right - left), max(1, bottom - top)
    layer = Image.new("RGBA", (bw, bh * 2), (0, 0, 0, 0))
    local = replace(style, x={"left": 0.0, "right": 1.0}.get(style.align, 0.5), y=0.5, box_w=1.0)
    _draw_text(ImageDraw.Draw(layer), layer.size, text, local)
    if scale != 1.0:
        layer = layer.resize((max(1, round(bw * scale)), max(1, round(bh * 2 * scale))), Image.LANCZOS)
    return layer


def draw_certificate(template: Image.Image, items, outline: bool = False, active: int = 0) -> Image.Image:
    """Draw every (text, style) in items onto a copy of the template.

    With outline=True each text box is outlined: the active one red, the others grey.
    """
    img = template.copy()
    d = ImageDraw.Draw(img)
    for text, style in items:
        _draw_text(d, img.size, text, style)
    if outline:
        lw = max(2, img.width // 500)
        for i, (_, style) in enumerate(items):
            d.rectangle(box_rect(img.size, style), outline="#ff2d55" if i == active else "#9aa0a6", width=lw)
    return img


def draw_name(template: Image.Image, name: str, style: TextStyle, outline: bool = False) -> Image.Image:
    return draw_certificate(template, [(name, style)], outline=outline)


def safe_filename(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip().strip(".")
    return re.sub(r"\s+", "_", s)[:80] or "certificate"


def _save(img: Image.Image, path: Path, fmt: str, dpi: int):
    if fmt == "PNG":
        img.save(path, "PNG", optimize=False, compress_level=3)
        return
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92, dpi=(dpi, dpi))
    if fmt == "JPEG":
        path.write_bytes(buf.getvalue())
        return
    # PDF: one page, original physical size, JPEG embedded (keeps files small).
    w_pt, h_pt = img.width / dpi * 72, img.height / dpi * 72
    doc = pymupdf.open()
    page = doc.new_page(width=w_pt, height=h_pt)
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(path, deflate=True)
    doc.close()


def generate(names, template, style, out_dir: Path, fmt="PDF", dpi=300, progress=None, workers=4, extras=()):
    """Render every certificate into out_dir and return the list of files written.

    extras: other fields as (style, texts), with texts[i] going on certificate i
    (e.g. the date repeated, or a running reference number).

    Files are written straight to disk as they are made, so memory use stays flat
    whether there are 10 names or 1000.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = {"PDF": "pdf", "JPEG": "jpg", "PNG": "png"}[fmt]
    width = max(3, len(str(len(names))))

    seen: dict[str, int] = {}
    jobs = []
    for i, name in enumerate(names, 1):
        base = safe_filename(name)
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            base = f"{base}_{seen[base]}"
        items = [(name, style)] + [(texts[i - 1], st) for st, texts in extras]
        jobs.append((items, out_dir / f"{i:0{width}d}_{base}.{ext}"))

    def work(job):
        items, path = job
        _save(draw_certificate(template, items), path, fmt, dpi)
        return path

    done = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path in pool.map(work, jobs):
            done.append(path)
            if progress:
                progress(len(done), len(jobs))
    return done


def zip_files(files, zip_path: Path) -> Path:
    # Stored, not deflated: JPEG/PDF/PNG are already compressed.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(f, f.name)
    return zip_path
