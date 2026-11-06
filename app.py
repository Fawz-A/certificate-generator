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


@st.cache_data(show_spinner=False)
def cached_pdf_table(data: bytes):
    return certgen.read_pdf_table(data)


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
tab_file, tab_type = st.tabs(["Upload Excel / CSV / PDF", "Type names"])
names: list[str] = []

with tab_file:
    sheet = st.file_uploader("Excel (.xlsx), CSV, or a PDF with a table of names", type=["xlsx", "csv", "pdf"])
    if sheet:
        is_pdf = sheet.name.lower().endswith(".pdf")
        hdr = 0
        if not is_pdf:
            hdr = 0 if st.checkbox("First row is a header", value=True) else None
        try:
            if is_pdf:
                with st.spinner("Reading the table from the PDF…"):
                    df = cached_pdf_table(sheet.getvalue())
            elif sheet.name.lower().endswith(".csv"):
                df = pd.read_csv(sheet, header=hdr)
            else:
                df = pd.read_excel(sheet, header=hdr)
        except Exception as e:
            st.error(f"Couldn't read that file: {e}")
            df = None
        if df is not None and df.empty:
            st.error("No table found in that file.")
        elif df is not None:
            cols = [str(c) for c in df.columns]
            df.columns = cols
            found = certgen.find_name_column(df)
            col = st.selectbox(
                "Column with the names", cols, index=cols.index(found) if found else 0,
                help="Picked automatically from headings like Name, Full Name, Candidate Name, Participant Name.",
            )
            if found:
                st.caption(f"Detected the name column: **{found}**")
            else:
                st.warning("Couldn't spot a name column. Please choose it above.")
            names = clean(df[col])
            odd = [n for n in names if not certgen.looks_like_name(n)]
            if odd:
                with st.expander(f"⚠️ {len(odd)} entries don't look like names. Check them before generating"):
                    st.write(odd)

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

FIELDS = {"name": "Name", "date": "Issue date", "ref": "Reference no."}
DEFAULTS = {  # x, y, box width (%), font size (fraction of height), align
    "name": (50.0, 50.0, 60, 0.06, "center"),
    "date": (80.0, 30.0, 18, 0.022, "left"),
    "ref": (90.0, 34.0, 10, 0.022, "left"),
}

# Reset the layout whenever a different template is uploaded.
tpl_id = (tpl_file.name, tpl_file.size, pdf_dpi)
if st.session_state.get("tpl_id") != tpl_id:
    st.session_state.tpl_id = tpl_id
    st.session_state.last_click = None
    for fid, (x, y, w, size, align) in DEFAULTS.items():
        st.session_state.update({
            f"{fid}_x": x, f"{fid}_y": y, f"{fid}_w": w,
            f"{fid}_size": max(8, int(H * size)), f"{fid}_align": align,
        })

# A click on the preview moves the selected field. Apply it before the sliders are drawn.
click = st.session_state.get("preview_click")
if click and click != st.session_state.get("last_click"):
    st.session_state.last_click = click
    fid = st.session_state.get("click_target", "name")
    st.session_state[f"{fid}_x"] = round(click["x"] / click["width"] * 100, 1)
    st.session_state[f"{fid}_y"] = round(click["y"] / click["height"] * 100, 1)


def style_controls(fid: str, fonts: dict, default_font: str) -> certgen.TextStyle:
    font_names = list(fonts)
    font_name = st.selectbox("Font", font_names, key=f"{fid}_font",
                             index=font_names.index(default_font) if default_font in font_names else 0)
    weights = certgen.font_weights(fonts[font_name])
    weight = None
    if weights:
        default_w = "Bold" if fid != "name" and "Bold" in weights else "Regular"
        weight = st.selectbox("Weight", weights, key=f"{fid}_weight",
                              index=weights.index(default_w) if default_w in weights else 0)
    c1, c2 = st.columns(2)
    color = c1.color_picker("Colour", "#1a1a1a", key=f"{fid}_color")
    align = c2.selectbox("Align", ["center", "left", "right"], key=f"{fid}_align")
    case = "As typed"
    if fid == "name":
        case = st.selectbox("Letter case", ["As typed", "Title Case", "UPPERCASE"], key="name_case")
    st.slider("Font size (px)", 8, max(200, H // 4), key=f"{fid}_size")
    st.slider("Horizontal position (%)", 0.0, 100.0, step=0.1, key=f"{fid}_x")
    st.slider("Vertical position (%)", 0.0, 100.0, step=0.1, key=f"{fid}_y")
    st.slider("Text box width (%)", 2, 100, key=f"{fid}_w",
              help="Text longer than the box shrinks automatically to fit.")
    ss = st.session_state
    return certgen.TextStyle(
        font_path=fonts[font_name], weight=weight, size=ss[f"{fid}_size"], color=color,
        x=ss[f"{fid}_x"] / 100, y=ss[f"{fid}_y"] / 100, box_w=ss[f"{fid}_w"] / 100,
        align=align, case=case,
    )


# ---------------------------------------------------------------- 3. layout
st.header("3 · Place the text")
left, right = st.columns([1, 2.2], gap="large")
n_names = len(names)

with left:
    fonts = {k: str(v) for k, v in certgen.bundled_fonts().items()}
    custom = st.file_uploader("Your own font (optional, .ttf / .otf)", type=["ttf", "otf"])
    if custom:
        p = Path(tempfile.gettempdir()) / "certgen_fonts" / custom.name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(custom.getvalue())
        fonts = {f"⭐ {Path(custom.name).stem}": str(p), **fonts}

    tab_name, tab_date, tab_ref = st.tabs(list(FIELDS.values()))
    with tab_name:
        styles = {"name": style_controls("name", fonts, next(iter(fonts)))}

    with tab_date:
        use_date = st.checkbox("Add an issue date", key="use_date")
        date_text = st.text_input("Date text (the same on every certificate)",
                                  value=time.strftime("%-d/%-m/%Y"), key="date_text",
                                  help="Printed exactly as typed, e.g. 2/11/2026 or Issue Date: 2 Nov 2026.")
        styles["date"] = style_controls("date", fonts, "Lora")

    with tab_ref:
        use_ref = st.checkbox("Add a reference number", key="use_ref")
        ref_mode = st.radio("Type", ["Dynamic (counts up)", "Static (same on all)"], key="ref_mode",
                            horizontal=True)
        if ref_mode.startswith("Dynamic"):
            ref_prefix = st.text_input("Text before the number (optional)", key="ref_prefix",
                                       help="Leave empty if the template already prints e.g. SNo.KUJSSLCVBCL-")
            c1, c2, c3 = st.columns(3)
            ref_start = c1.number_input("Start", min_value=0, value=1, step=1, key="ref_start")
            # Default end = start + number of names - 1; reset when the list or start changes.
            if st.session_state.get("ref_end_for") != (n_names, ref_start):
                st.session_state.ref_end_for = (n_names, ref_start)
                st.session_state.ref_end = int(ref_start + max(n_names, 1) - 1)
            ref_end = c2.number_input("End", min_value=0, step=1, key="ref_end")
            ref_pad = c3.number_input("Min. digits", min_value=0, max_value=8, value=0, step=1, key="ref_pad",
                                      help="3 gives 001, 002 … Leave 0 for 1, 2 …")
        else:
            ref_static = st.text_input("Reference number (the same on every certificate)", key="ref_static")
        styles["ref"] = style_controls("ref", fonts, "Lora")

# Which certificates get made, and the reference number on each.
dynamic_ref = use_ref and ref_mode.startswith("Dynamic")
n_certs = n_names
if dynamic_ref:
    n_certs = max(0, min(n_names, int(ref_end - ref_start + 1)))


def ref_for(i: int) -> str:  # i = 0-based position in the list
    if dynamic_ref:
        return f"{ref_prefix}{str(int(ref_start) + i).zfill(int(ref_pad))}"
    return ref_static


def items_for(i: int, name: str):
    items = [(name, styles["name"])]
    if use_date:
        items.append((date_text, styles["date"]))
    if use_ref:
        items.append((ref_for(i), styles["ref"]))
    return items


with right:
    if names:
        longest = max(range(n_names), key=lambda i: len(names[i]))
        samples = [0] + ([longest] if longest else [])
    else:
        samples = [0]
    labels = ["First name", "Longest name (check it fits)"]
    which = st.radio("Preview with", range(len(samples)), horizontal=True,
                     format_func=lambda k: f"{labels[k]}: {names[samples[k]] if names else 'Your Name Here'}")
    idx = samples[which]
    items = items_for(idx, names[idx] if names else "Your Name Here")

    targets = ["name"] + (["date"] if use_date else []) + (["ref"] if use_ref else [])
    if st.session_state.get("click_target") not in targets:
        st.session_state.click_target = "name"
    target = st.radio("👆 Clicking the certificate moves", targets, key="click_target",
                      format_func=FIELDS.get, horizontal=True)
    preview = certgen.draw_certificate(template, items, outline=True, active=targets.index(target))
    preview.thumbnail((PREVIEW_W, PREVIEW_W * 4))
    st.caption("The red box is the field you're moving; grey boxes are the others.")
    streamlit_image_coordinates(preview, key="preview_click", width="stretch",
                                image_format="JPEG", jpeg_quality=85, cursor="crosshair")

# ---------------------------------------------------------------- 4. generate
st.header("4 · Generate")
if not names:
    st.warning("Add some names in step 1 first.")
    st.stop()
if dynamic_ref:
    if ref_end < ref_start:
        st.error("The reference number End is lower than Start.")
        st.stop()
    span = int(ref_end - ref_start + 1)
    if span < n_names:
        st.warning(f"Numbers {ref_start}–{ref_end} cover only {span} people, so only the first "
                   f"{span} of {n_names} names get certificates.")
    elif span > n_names:
        st.info(f"There are {n_names} names, so numbers stop at {ref_for(n_names - 1)}; "
                f"the rest of the range up to {ref_end} goes unused.")
    else:
        st.caption(f"Reference numbers {ref_for(0)} → {ref_for(n_names - 1)}, in list order.")

c1, c2 = st.columns([1, 3])
fmt = c1.selectbox("Output format", ["PDF", "JPEG", "PNG"])
c2.write("")
c2.write("")
go = c2.button(f"Generate {n_certs} certificates", type="primary")

if go:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_ROOT / f"{Path(tpl_file.name).stem}_{stamp}"
    bar = st.progress(0.0, text="Starting…")
    t0 = time.time()

    def tick(done, total):
        bar.progress(done / total, text=f"{done} / {total}")

    batch = names[:n_certs]
    extras = []
    if use_date:
        extras.append((styles["date"], [date_text] * n_certs))
    if use_ref:
        extras.append((styles["ref"], [ref_for(i) for i in range(n_certs)]))
    files = certgen.generate(batch, template, styles["name"], out_dir, fmt=fmt, dpi=dpi,
                             progress=tick, extras=extras)
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
