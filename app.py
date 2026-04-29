"""Flask web interface for the website cloner — SiteCloner Pro."""

import asyncio
import os
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

from cloner import WebsiteCloner

app = Flask(__name__)

JOBS: dict[str, dict] = {}
WORK_DIR = Path("/tmp/cloner_jobs")
WORK_DIR.mkdir(parents=True, exist_ok=True)


def build_zip(job_id: str):
    out_dir = WORK_DIR / job_id / "site"
    zip_path = WORK_DIR / job_id / "site.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in out_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(out_dir))
    return zip_path


def run_clone_job(job_id: str, url: str, depth: int, pages: int, js: bool):
    job = JOBS[job_id]
    job["status"] = "running"
    job["pages_done"] = 0
    job["current_url"] = url

    out_dir = WORK_DIR / job_id / "site"
    out_dir.mkdir(parents=True, exist_ok=True)

    def on_progress(pages_done: int, current_url: str):
        job["pages_done"] = pages_done
        job["current_url"] = current_url

    try:
        cloner = WebsiteCloner(
            base_url=url,
            output_dir=str(out_dir),
            max_depth=depth,
            max_pages=pages,
            js_render=js,
            delay=0.2,
            on_progress=on_progress,
        )
        asyncio.run(cloner.clone())

        saved_files = list(out_dir.rglob("*.html"))
        if not saved_files:
            job["status"] = "error"
            job["error"] = "No pages could be saved — the site may be blocking crawlers. Try disabling JavaScript Rendering and retry."
            return

        zip_path = build_zip(job_id)
        job["status"] = "done"
        job["zip"] = str(zip_path)
        job["pages"] = len(cloner.visited_urls)
        job["assets"] = len(cloner.downloaded_assets)
        job["original_title"] = cloner.site_title or ""
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/clone", methods=["POST"])
def clone():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return jsonify(error="Please enter a valid URL starting with http:// or https://"), 400

    depth = min(int(data.get("depth", 3)), 6)
    pages = min(int(data.get("pages", 30)), 500)
    js = bool(data.get("js", True))

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "id": job_id,
        "url": url,
        "status": "queued",
        "created": datetime.utcnow().isoformat(),
    }

    threading.Thread(
        target=run_clone_job,
        args=(job_id, url, depth, pages, js),
        daemon=True,
    ).start()

    return jsonify(job_id=job_id)


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404
    return jsonify(job)


@app.route("/detect/<job_id>")
def detect(job_id):
    """Auto-detect business name, phone, email, address from cloned HTML."""
    import re
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="Not ready"), 404

    out_dir = WORK_DIR / job_id / "site"
    index_file = out_dir / "index.html"
    if not index_file.exists():
        # Try any HTML file
        html_files = list(out_dir.rglob("*.html"))
        if not html_files:
            return jsonify(name="", phone="", email="", address="")
        index_file = html_files[0]

    try:
        from bs4 import BeautifulSoup
        content = index_file.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(content, "html.parser")

        # Business name from title or h1
        name = ""
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            name = title_tag.string.strip().split("|")[0].split("-")[0].strip()
        if not name:
            h1 = soup.find("h1")
            if h1:
                name = h1.get_text(strip=True)

        text = soup.get_text(" ", strip=True)

        # Phone
        phone_match = re.search(r'(\+?\d[\d\s\-().]{8,}\d)', text)
        phone = phone_match.group(1).strip() if phone_match else ""

        # Email
        email_match = re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text)
        email = email_match.group(0) if email_match else ""

        return jsonify(name=name, phone=phone, email=email)
    except Exception as exc:
        return jsonify(name="", phone="", email="", error=str(exc))


@app.route("/customize/<job_id>", methods=["POST"])
def customize(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="Job not found or not complete"), 404

    data = request.json or {}
    replacements: dict = data.get("replacements", {})

    out_dir = WORK_DIR / job_id / "site"
    changed = 0

    for html_file in out_dir.rglob("*.html"):
        try:
            content = html_file.read_text(encoding="utf-8", errors="ignore")
            new_content = content
            for old, new in replacements.items():
                if old and new and old.strip() and old != new:
                    new_content = new_content.replace(old, new)
            if new_content != content:
                html_file.write_text(new_content, encoding="utf-8")
                changed += 1
        except Exception:
            pass

    # Rebuild ZIP with customized files
    build_zip(job_id)
    return jsonify(success=True, files_changed=changed)


# ── Serve the cloned site ─────────────────────────────────────────────────────

@app.route("/sites/<job_id>/")
@app.route("/sites/<job_id>/<path:filepath>")
def serve_site(job_id, filepath="index.html"):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return "Site not ready or not found.", 404

    site_dir = WORK_DIR / job_id / "site"
    target = site_dir / filepath

    if target.is_dir():
        target = target / "index.html"
        filepath = str(Path(filepath) / "index.html")

    if not target.exists():
        return "Page not found.", 404

    return send_from_directory(site_dir, filepath)


# ── ZIP download ──────────────────────────────────────────────────────────────

@app.route("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="Not ready"), 404

    zip_path = job.get("zip")
    if not zip_path or not Path(zip_path).exists():
        return jsonify(error="File missing"), 404

    domain = job["url"].replace("https://", "").replace("http://", "").split("/")[0]
    return send_file(
        zip_path,
        as_attachment=True,
        download_name=f"{domain}_clone.zip",
        mimetype="application/zip",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
