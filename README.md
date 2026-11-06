# Certificate Generator

Fill hundreds of names onto a certificate template in one go — no more copy-pasting each name by hand.

## How it works

1. **Names** — upload an Excel (`.xlsx`), CSV, or a PDF containing a table, or type names one per line. The name column is found automatically from its heading (Name, Full Name, Candidate Name, Participant Name, Name of the Student …); headings like Institution Name or Father's Name are skipped. Entries that don't look like names (emails, digits) are flagged.
2. **Template** — upload the certificate as JPEG, PNG or PDF.
3. **Place the name** — click on the preview where the name should go, then fine-tune with the sliders. Pick a font (9 bundled, or upload your own `.ttf` / `.otf`), weight, colour, alignment and letter case. Preview with the longest name to check it fits; names too long for the box shrink automatically.
4. **Generate** — choose PDF, JPEG or PNG and press **Generate**. Files are numbered in list order and named after the person (`0001_Asha_Rao.pdf`). Download everything as one ZIP. When running locally, the files are also saved in `output/`.

Handles 1000 certificates in under a minute.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Try it with the files in `sample/` (a template and 1000 test names).

## Deploy

Deploy on [Streamlit Community Cloud](https://share.streamlit.io) with `app.py` as the main file. On the cloud, files saved to `output/` are temporary, so use the ZIP download.

To require a passkey, add this under the app's **Settings → Secrets** (leave it out for an open app):

```toml
APP_PASSWORD = "your-passkey"
```

## Fonts

Bundled fonts are from [Google Fonts](https://fonts.google.com) under the SIL Open Font License (see `fonts/OFL.txt`).
