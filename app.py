import hmac
import io
import tempfile
import time
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_image_coordinates import streamlit_image_coordinates

import certgen

st.set_page_config(page_title="Certificate Generator", page_icon="🎓", layout="wide")


def password_gate():
    """Shared passkey, on when APP_PASSWORD is set in the host's secrets.

    Locally there is no secrets file, so there is no gate.
    """
    try:
        expected = st.secrets.get("APP_PASSWORD", "")
    except Exception:
        expected = ""
    if not expected or st.session_state.get("authed"):
        return
    st.title("🎓 Certificate Generator")
    with st.form("gate"):
        given = st.text_input("Passkey", type="password")
        if st.form_submit_button("Enter"):
            if hmac.compare_digest(given.encode(), str(expected).encode()):
                st.session_state.authed = True
                st.rerun()
            st.error("Wrong passkey.")
    st.stop()


password_gate()
st.title("🎓 Certificate Generator")
st.caption("Names in, certificates out. Upload a list and a template, place the name once, generate them all.")

OUTPUT_ROOT = Path(__file__).parent / "output"
PREVIEW_W = 900  # px the preview is shown at


@st.cache_data(show_spinner=False)
def cached_template(data: bytes, filename: str, dpi: int):
    return certgen.load_template(data, filename, dpi)


def clean(values) -> list[str]:
    out = []
    for v in values:
        if pd.isna(v):
            continue
        s = str(v).strip()
        if s:
            out.append(s)
    return out


# ---------------------------------------------------------------- 1. names
st.header("1 · Names")
tab_file, tab_type = st.tabs(["Upload Excel / CSV", "Type names"])
names: list[str] = []

with tab_file:
    sheet = st.file_uploader("Excel (.xlsx) or CSV file", type=["xlsx", "csv"])
    if sheet:
        has_header = st.checkbox("First row is a header", value=True)
        hdr = 0 if has_header else None
        try:
            if sheet.name.lower().endswith(".csv"):
                df = pd.read_csv(sheet, header=hdr)
            else:
                df = pd.read_excel(sheet, header=hdr)
        except Exception as e:
            st.error(f"Couldn't read that file: {e}")
            df = None
        if df is not None and not df.empty:
            cols = [str(c) for c in df.columns]
            df.columns = cols
            guess = next((i for i, c in enumerate(cols) if "name" in c.lower()), 0)
            col = st.selectbox("Column with the names", cols, index=guess)
            names = clean(df[col])

with tab_type:
    typed = st.text_area("One name per line", height=180, placeholder="Asha Rao\nRahul Mehta\n…")
    if typed.strip() and not names:
        names = clean(typed.splitlines())
    elif typed.strip() and names:
        st.info("Using the uploaded file. Remove it to use the typed names instead.")

if names:
    st.success(f"**{len(names)}** names loaded.")
    with st.expander("See names"):
        st.dataframe(pd.DataFrame({"Name": names}), height=220, width="stretch")

# ---------------------------------------------------------------- 2. template
st.header("2 · Certificate template")
tpl_file = st.file_uploader("Template (JPEG, PNG or PDF)", type=["jpg", "jpeg", "png", "pdf"])
if not tpl_file:
    st.stop()

pdf_dpi = 300
if tpl_file.name.lower().endswith(".pdf"):
    pdf_dpi = st.select_slider(
        "PDF render quality (DPI)", options=[150, 200, 300], value=200,
        help="300 is print quality but files are ~2× bigger. 200 prints fine for most certificates.",
    )
template, dpi = cached_template(tpl_file.getvalue(), tpl_file.name, pdf_dpi)
W, H = template.size
st.caption(f"Template: {W} × {H} px")

# Reset the layout whenever a different template is uploaded.
tpl_id = (tpl_file.name, tpl_file.size, pdf_dpi)
if st.session_state.get("tpl_id") != tpl_id:
    st.session_state.update(
        tpl_id=tpl_id, pos_x=50.0, pos_y=50.0, box_w=60,
        font_size=max(12, int(H * 0.06)), last_click=None,
    )

# A click on the preview moves the box. Apply it before the sliders are drawn.
click = st.session_state.get("preview_click")
if click and click != st.session_state.get("last_click"):
    st.session_state.last_click = click
    st.session_state.pos_x = round(click["x"] / click["width"] * 100, 1)
    st.session_state.pos_y = round(click["y"] / click["height"] * 100, 1)

# ---------------------------------------------------------------- 3. layout
st.header("3 · Place the name")
left, right = st.columns([1, 2.2], gap="large")

with left:
    fonts = {k: str(v) for k, v in certgen.bundled_fonts().items()}
    custom = st.file_uploader("Your own font (optional, .ttf / .otf)", type=["ttf", "otf"])
    if custom:
        p = Path(tempfile.gettempdir()) / "certgen_fonts" / custom.name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(custom.getvalue())
        fonts = {f"⭐ {Path(custom.name).stem}": str(p), **fonts}
    font_name = st.selectbox("Font", list(fonts))
    font_path = fonts[font_name]

    weights = certgen.font_weights(font_path)
    weight = None
    if weights:
        weight = st.selectbox("Weight", weights, index=weights.index("Regular") if "Regular" in weights else 0)

    c1, c2 = st.columns(2)
    color = c1.color_picker("Colour", "#1a1a1a")
    align = c2.selectbox("Align", ["center", "left", "right"])
    case = st.selectbox("Letter case", ["As typed", "Title Case", "UPPERCASE"])

    st.slider("Font size (px)", 8, max(200, H // 4), key="font_size")
    st.slider("Horizontal position (%)", 0.0, 100.0, step=0.1, key="pos_x")
    st.slider("Vertical position (%)", 0.0, 100.0, step=0.1, key="pos_y")
    st.slider("Text box width (%)", 5, 100, key="box_w",
              help="Names longer than the box shrink automatically to fit.")

style = certgen.TextStyle(
    font_path=font_path, weight=weight, size=st.session_state.font_size, color=color,
    x=st.session_state.pos_x / 100, y=st.session_state.pos_y / 100,
    box_w=st.session_state.box_w / 100, align=align, case=case,
)

with right:
    sample_opts = names[:1] + ([max(names, key=len)] if len(names) > 1 else [])
    if not sample_opts:
        sample_opts = ["Your Name Here"]
    labels = {0: "First name", 1: "Longest name (check it fits)"}
    which = st.radio("Preview with", range(len(sample_opts)),
                     format_func=lambda i: f"{labels[i]}: {sample_opts[i]}", horizontal=True)
    preview = certgen.draw_name(template, sample_opts[which], style, outline=True)
    preview.thumbnail((PREVIEW_W, PREVIEW_W * 4))
    st.caption("👆 Click anywhere on the certificate to move the name there. The red box is the text area.")
    streamlit_image_coordinates(preview, key="preview_click", width="stretch",
                                image_format="JPEG", jpeg_quality=85, cursor="crosshair")

# ---------------------------------------------------------------- 4. generate
st.header("4 · Generate")
if not names:
    st.warning("Add some names in step 1 first.")
    st.stop()

c1, c2 = st.columns([1, 3])
fmt = c1.selectbox("Output format", ["PDF", "JPEG", "PNG"])
c2.write("")
c2.write("")
go = c2.button(f"Generate {len(names)} certificates", type="primary")

if go:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_ROOT / f"{Path(tpl_file.name).stem}_{stamp}"
    bar = st.progress(0.0, text="Starting…")
    t0 = time.time()

    def tick(done, total):
        bar.progress(done / total, text=f"{done} / {total}")

    files = certgen.generate(names, template, style, out_dir, fmt=fmt, dpi=dpi, progress=tick)
    bar.progress(1.0, text="Zipping…")
    zip_path = certgen.zip_files(files, OUTPUT_ROOT / f"{out_dir.name}.zip")
    bar.empty()
    st.session_state.result = {
        "zip": str(zip_path), "dir": str(out_dir), "count": len(files), "secs": time.time() - t0,
    }

res = st.session_state.get("result")
if res and Path(res["zip"]).exists():
    size_mb = Path(res["zip"]).stat().st_size / 1e6
    st.success(f"Done — {res['count']} certificates in {res['secs']:.0f}s ({size_mb:.0f} MB).")
    st.caption(f"Saved on this machine in `{res['dir']}`")
    with open(res["zip"], "rb") as f:
        st.download_button("⬇️ Download all (ZIP)", f, file_name=Path(res["zip"]).name,
                           mime="application/zip", on_click="ignore")
