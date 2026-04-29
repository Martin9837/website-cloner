"""Flask web interface for the website cloner."""

import asyncio
import os
import shutil
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from cloner import WebsiteCloner

app = Flask(__name__)

JOBS: dict[str, dict] = {}
WORK_DIR = Path("/tmp/cloner_jobs")
WORK_DIR.mkdir(parents=True, exist_ok=True)


def run_clone_job(job_id: str, url: str, depth: int, pages: int, js: bool):
    job = JOBS[job_id]
    job["status"] = "running"
    job["log"] = []

    out_dir = WORK_DIR / job_id / "site"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        cloner = WebsiteCloner(
            base_url=url,
            output_dir=str(out_dir),
            max_depth=depth,
            max_pages=pages,
            js_render=js,
            delay=0.3,
        )
        asyncio.run(cloner.clone())

        # Zip the result
        zip_path = WORK_DIR / job_id / "site.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in out_dir.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(out_dir))

        job["status"] = "done"
        job["zip"] = str(zip_path)
        job["pages"] = len(cloner.visited_urls)
        job["assets"] = len(cloner.downloaded_assets)
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
    pages = min(int(data.get("pages", 100)), 500)
    js = bool(data.get("js", True))

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "id": job_id,
        "url": url,
        "status": "queued",
        "created": datetime.utcnow().isoformat(),
    }

    t = threading.Thread(
        target=run_clone_job,
        args=(job_id, url, depth, pages, js),
        daemon=True,
    )
    t.start()

    return jsonify(job_id=job_id)


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404
    return jsonify(job)


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
