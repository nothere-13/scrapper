"""
novelbuddy.io — Web App Downloader
Supports TXT (zip), DOCX, and PDF output formats.
"""

import re
import os
import json
import time
import html as html_lib
import zipfile
import threading
import uuid
from urllib.request import urlopen, Request
from flask import Flask, render_template, request, jsonify, send_file

# ── Optional format libraries ─────────────────────────────────────────────────
try:
    from docx import Document as DocxDocument
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    DOCX_OK = True
except ImportError:
    DOCX_OK = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.enums import TA_LEFT, TA_CENTER
    PDF_OK = True
except ImportError:
    PDF_OK = False

app = Flask(__name__)
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

BASE_URL   = "https://novelbuddy.io"
DELAY      = 0.6
MAX_ERRORS = 5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
}

jobs = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_raw(url):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")

def fetch_json(url):
    return json.loads(fetch_raw(url))

def get_build_id(slug):
    html = fetch_raw(f"{BASE_URL}/{slug}")
    for pat in [r'"buildId"\s*:\s*"([^"]+)"',
                r'/_next/static/([^/]+)/_buildManifest\.js']:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    raise RuntimeError("Could not detect build ID.")

def fetch_props(build_id, slug, ch_slug):
    url = f"{BASE_URL}/_next/data/{build_id}/{slug}/{ch_slug}.json"
    try:
        return fetch_json(url).get("pageProps", {})
    except Exception:
        return None

def html_to_text(raw):
    text = html_lib.unescape(raw)
    text = re.sub(r'<br\s*/?>', '\n',   text, flags=re.IGNORECASE)
    text = re.sub(r'</p>',      '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>',    '\n',   text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>',   '',     text)
    text = re.sub(r'\n{3,}',    '\n\n', text)
    return text.strip()

def safe_filename(slug, title):
    m = re.search(r'chapter-(\d+)', slug)
    num = int(m.group(1)) if m else 0
    safe = re.sub(r'[\\/:*?"<>|]', '', title)
    safe = re.sub(r'\s+', '_', safe.strip())[:60]
    return f"{num:04d}_{safe}"

def parse_ch_num(name, slug):
    for pat, src in [(r'[Cc]hapter\s*(\d+)', name), (r'chapter-(\d+)', slug)]:
        m = re.search(pat, src)
        if m:
            return int(m.group(1))
    return None


# ── Format builders ───────────────────────────────────────────────────────────

def build_zip(job_dir, chapters, out_path):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, _ in chapters:
            zf.write(os.path.join(job_dir, fname + ".txt"), fname + ".txt")


def build_docx(novel_title, chapters, out_path):
    doc = DocxDocument()
    h = doc.add_heading(novel_title, level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_page_break()
    for fname, (ch_name, body) in chapters:
        doc.add_heading(ch_name, level=1)
        for para in body.split("\n\n"):
            para = para.strip()
            if para:
                p = doc.add_paragraph(para)
                p.style.font.size = Pt(11)
        doc.add_page_break()
    doc.save(out_path)


def build_pdf(novel_title, chapters, out_path):
    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title2", parent=styles["Title"],
                                 fontSize=20, spaceAfter=20, alignment=TA_CENTER)
    ch_style    = ParagraphStyle("ChHead", parent=styles["Heading1"],
                                 fontSize=13, spaceBefore=10, spaceAfter=8)
    body_style  = ParagraphStyle("Body2", parent=styles["Normal"],
                                 fontSize=10, leading=16, spaceAfter=6)

    story = [Paragraph(novel_title, title_style), PageBreak()]
    for _, (ch_name, body) in chapters:
        story.append(Paragraph(ch_name, ch_style))
        for para in body.split("\n\n"):
            para = para.strip()
            if para:
                # Escape XML special chars for reportlab
                para = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(para, body_style))
        story.append(PageBreak())
    doc.build(story)


# ── Background job ────────────────────────────────────────────────────────────

def download_job(job_id, novel_slug, novel_title, first_slug, fmt):
    job = jobs[job_id]

    def log(msg):
        job["log"].append(msg)

    try:
        log(f"Starting: {novel_title}  [{fmt.upper()}]")
        build_id = get_build_id(novel_slug)
        log(f"Build ID: {build_id}")

        props = fetch_props(build_id, novel_slug, first_slug)
        if props is None:
            build_id = get_build_id(novel_slug)
            props = fetch_props(build_id, novel_slug, first_slug)
        if props is None:
            raise RuntimeError("Cannot fetch first chapter. Check the slug.")

        job_dir = os.path.join(DOWNLOAD_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)

        current_slug    = first_slug
        consecutive_err = 0
        total_dl        = 0
        # list of (base_filename, (ch_name, body_text))
        chapters = []

        while current_slug:
            if props is None:
                props = fetch_props(build_id, novel_slug, current_slug)

            if props is None:
                consecutive_err += 1
                log(f"⚠ Failed ({consecutive_err}/{MAX_ERRORS}): {current_slug}")
                if consecutive_err >= MAX_ERRORS:
                    log("Too many errors — stopping.")
                    break
                m = re.match(r'(chapter-)(\d+)', current_slug)
                if m:
                    current_slug = f"chapter-{int(m.group(2)) + 1}"
                    props = None
                    continue
                break

            consecutive_err = 0
            ch        = props.get("initialChapter", {})
            next_info = props.get("nextChapter") or {}
            ch_slug   = ch.get("slug", current_slug)
            ch_name   = ch.get("name", current_slug)
            ch_words  = ch.get("word_count", "")
            ch_num    = parse_ch_num(ch_name, ch_slug)
            raw       = ch.get("content", "")
            body      = html_to_text(raw) if raw else "(No content)"
            fname     = safe_filename(ch_slug, ch_name)

            # Always save txt (used by zip; also needed for docx/pdf assembly)
            with open(os.path.join(job_dir, fname + ".txt"), "w", encoding="utf-8") as f:
                f.write(body + "\n")

            total_dl += 1
            chapters.append((fname, (ch_name, body)))
            log(f"✓ [{ch_num or '?'}] {ch_name} ({ch_words} words)")

            next_slug = next_info.get("slug", "") if isinstance(next_info, dict) else ""
            if not next_slug or next_slug == ch_slug:
                log("Chain complete — no more chapters.")
                break

            current_slug = next_slug
            props = None
            time.sleep(DELAY)

        # Build output file
        slug_safe = re.sub(r'[^\w\-]', '_', novel_slug)
        log(f"Building {fmt.upper()} file …")

        if fmt == "txt":
            out_name = f"{slug_safe}.zip"
            out_path = os.path.join(DOWNLOAD_DIR, out_name)
            build_zip(job_dir, chapters, out_path)

        elif fmt == "docx":
            out_name = f"{slug_safe}.docx"
            out_path = os.path.join(DOWNLOAD_DIR, out_name)
            build_docx(novel_title, chapters, out_path)

        elif fmt == "pdf":
            out_name = f"{slug_safe}.pdf"
            out_path = os.path.join(DOWNLOAD_DIR, out_name)
            build_pdf(novel_title, chapters, out_path)

        # Cleanup txt files
        for fname, _ in chapters:
            try: os.remove(os.path.join(job_dir, fname + ".txt"))
            except: pass
        try: os.rmdir(job_dir)
        except: pass

        job["out_name"] = out_name
        job["total"]    = total_dl
        log(f"✅ Done! {total_dl} chapters — ready to download.")

    except Exception as e:
        job["error"] = str(e)
        log(f"❌ Error: {e}")
    finally:
        job["done"] = True


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html",
                           docx_ok=DOCX_OK, pdf_ok=PDF_OK)

@app.route("/start", methods=["POST"])
def start():
    data        = request.json
    novel_slug  = data.get("slug", "").strip()
    novel_title = data.get("title", "").strip()
    first_slug  = data.get("first", "chapter-1").strip() or "chapter-1"
    fmt         = data.get("format", "txt").strip().lower()

    if not novel_slug or not novel_title:
        return jsonify({"error": "Slug and title are required."}), 400
    if fmt == "docx" and not DOCX_OK:
        return jsonify({"error": "python-docx not installed on server."}), 400
    if fmt == "pdf" and not PDF_OK:
        return jsonify({"error": "reportlab not installed on server."}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"log": [], "done": False, "out_name": None, "error": None, "total": 0}

    threading.Thread(target=download_job,
                     args=(job_id, novel_slug, novel_title, first_slug, fmt),
                     daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/poll/<job_id>")
def poll(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Not found"}), 404
    job   = jobs[job_id]
    after = int(request.args.get("after", 0))
    return jsonify({
        "lines":    job["log"][after:],
        "done":     job["done"],
        "out_name": job["out_name"],
        "total":    job["total"],
        "error":    job["error"],
    })


@app.route("/download/<job_id>")
def download(job_id):
    if job_id not in jobs:
        return "Not found", 404
    job = jobs[job_id]
    if not job.get("out_name"):
        return "Not ready", 400
    out_path = os.path.join(DOWNLOAD_DIR, job["out_name"])
    return send_file(out_path, as_attachment=True, download_name=job["out_name"])


if __name__ == "__main__":
    app.run(debug=True, threaded=True)
