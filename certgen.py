"""Core certificate rendering — no Streamlit here, so it can be tested on its own."""

import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
    x: float             # box centre, fraction of image width
    y: float             # box centre, fraction of image height
    box_w: float         # box width, fraction of image width
    align: str           # "left" | "center" | "right"
    case: str            # "As typed" | "UPPERCASE" | "Title Case"


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
    W, H = img_size
    bw, bh = style.box_w * W, style.size * 1.4
    cx, cy = style.x * W, style.y * H
    return int(cx - bw / 2), int(cy - bh / 2), int(cx + bw / 2), int(cy + bh / 2)


def draw_name(template: Image.Image, name: str, style: TextStyle, outline: bool = False) -> Image.Image:
    """Draw one name onto a copy of the template. Long names shrink to fit the box."""
    img = template.copy()
    d = ImageDraw.Draw(img)
    text = apply_case(name, style.case)
    left, top, right, bottom = box_rect(img.size, style)
    max_w = right - left

    size = style.size
    font = _font(style, size)
    while size > 8 and d.textlength(text, font=font) > max_w:
        size = int(size * 0.95)
        font = _font(style, size)

    cy = (top + bottom) / 2
    x, anchor = {"left": (left, "lm"), "right": (right, "rm")}.get(style.align, ((left + right) / 2, "mm"))
    d.text((x, cy), text, font=font, fill=style.color, anchor=anchor)

    if outline:
        lw = max(2, img.width // 500)
        d.rectangle((left, top, right, bottom), outline="#ff2d55", width=lw)
    return img


def safe_filename(name: str) -> str:
    """The person's name as a file name: only characters Windows/macOS forbid are removed."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s[:120] or "certificate"


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


def generate(names, template, style, out_dir: Path, fmt="PDF", dpi=300, progress=None, workers=4):
    """Render every certificate into out_dir and return the list of files written.

    Files are written straight to disk as they are made, so memory use stays flat
    whether there are 10 names or 1000.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = {"PDF": "pdf", "JPEG": "jpg", "PNG": "png"}[fmt]
    # File name = the person's name. Same name twice -> "Garima (2)".
    # Compared case-insensitively because macOS/Windows treat "garima" and "Garima" as one file.
    seen: dict[str, int] = {}
    jobs = []
    for name in names:
        base = safe_filename(name)
        n = seen[base.lower()] = seen.get(base.lower(), 0) + 1
        if n > 1:
            base = f"{base} ({n})"
        jobs.append((name, out_dir / f"{base}.{ext}"))

    def work(job):
        name, path = job
        _save(draw_name(template, name, style), path, fmt, dpi)
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
