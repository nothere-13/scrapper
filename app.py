"""
novelbuddy.io — Web App Downloader
Flask backend with SSE progress streaming
"""

import re
import os
import json
import time
import html as html_lib
import zipfile
import threading
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from flask import Flask, render_template, request, Response, send_file, jsonify

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

# job_id -> {"status", "log": [], "zip_path", "done", "error"}
jobs = {}


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
    except Exception as e:
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
    return f"{num:04d}_{safe}.txt"

def parse_ch_num(name, slug):
    for pat, src in [(r'[Cc]hapter\s*(\d+)', name), (r'chapter-(\d+)', slug)]:
        m = re.search(pat, src)
        if m:
            return int(m.group(1))
    return None


def download_job(job_id, novel_slug, novel_title, first_slug):
    job = jobs[job_id]
    log = job["log"]

    def emit(msg):
        log.append(msg)

    try:
        emit(f"Starting: {novel_title}")
        build_id = get_build_id(novel_slug)
        emit(f"Build ID: {build_id}")

        props = fetch_props(build_id, novel_slug, first_slug)
        if props is None:
            build_id = get_build_id(novel_slug)
            props = fetch_props(build_id, novel_slug, first_slug)
        if props is None:
            raise RuntimeError("Cannot fetch first chapter. Check the slug.")

        # temp folder for this job
        job_dir = os.path.join(DOWNLOAD_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)

        current_slug    = first_slug
        consecutive_err = 0
        total_dl        = 0
        chapters        = []

        while current_slug:
            if props is None:
                props = fetch_props(build_id, novel_slug, current_slug)

            if props is None:
                consecutive_err += 1
                emit(f"⚠ Failed ({consecutive_err}/{MAX_ERRORS}): {current_slug}")
                if consecutive_err >= MAX_ERRORS:
                    emit("Too many errors — stopping.")
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

            fname    = safe_filename(ch_slug, ch_name)
            out_path = os.path.join(job_dir, fname)
            body     = html_to_text(raw) if raw else "(No content)"

            with open(out_path, "w", encoding="utf-8") as f:
                f.write(body + "\n")

            total_dl += 1
            chapters.append(fname)
            emit(f"✓ [{ch_num or '?'}] {ch_name} ({ch_words} words)")

            next_slug = next_info.get("slug", "") if isinstance(next_info, dict) else ""
            if not next_slug or next_slug == ch_slug:
                emit("Chain complete — no more chapters.")
                break

            current_slug = next_slug
            props = None
            time.sleep(DELAY)

        # zip everything
        zip_name = f"{re.sub(r'[^\\w\\-]', '_', novel_slug)}.zip"
        zip_path = os.path.join(DOWNLOAD_DIR, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in chapters:
                zf.write(os.path.join(job_dir, fname), fname)

        # cleanup txt files
        for fname in chapters:
            os.remove(os.path.join(job_dir, fname))
        os.rmdir(job_dir)

        job["zip_path"] = zip_path
        job["zip_name"] = zip_name
        job["total"]    = total_dl
        emit(f"DONE:{total_dl}:{zip_name}")

    except Exception as e:
        emit(f"ERROR:{e}")
    finally:
        job["done"] = True


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    data        = request.json
    novel_slug  = data.get("slug", "").strip()
    novel_title = data.get("title", "").strip()
    first_slug  = data.get("first", "chapter-1").strip() or "chapter-1"

    if not novel_slug or not novel_title:
        return jsonify({"error": "Slug and title are required."}), 400

    import uuid
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"log": [], "done": False, "zip_path": None}

    t = threading.Thread(target=download_job,
                         args=(job_id, novel_slug, novel_title, first_slug),
                         daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    """SSE endpoint — streams log lines to the browser."""
    if job_id not in jobs:
        return "Not found", 404

    def stream():
        sent = 0
        while True:
            job = jobs[job_id]
            log = job["log"]
            while sent < len(log):
                yield f"data: {log[sent]}\n\n"
                sent += 1
            if job["done"]:
                break
            time.sleep(0.3)

    return Response(stream(), mimetype="text/event-stream")


@app.route("/download/<job_id>")
def download(job_id):
    if job_id not in jobs:
        return "Not found", 404
    job = jobs[job_id]
    if not job.get("zip_path"):
        return "Not ready", 400
    return send_file(job["zip_path"],
                     as_attachment=True,
                     download_name=job["zip_name"])


if __name__ == "__main__":
    app.run(debug=True, threaded=True)
