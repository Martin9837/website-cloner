#!/usr/bin/env python3
"""Website cloner — crawls any website and saves a fully offline copy."""

import asyncio
import os
import re
import sys
import hashlib
import argparse
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from collections import deque
from typing import Optional

try:
    from playwright.async_api import async_playwright
    import httpx
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run:  pip install -r requirements.txt")
    print("Then:                        playwright install chromium")
    sys.exit(1)


class WebsiteCloner:
    def __init__(
        self,
        base_url: str,
        output_dir: str,
        max_depth: int = 3,
        max_pages: int = 500,
        js_render: bool = True,
        delay: float = 0.5,
        same_domain_only: bool = True,
        on_progress=None,
        timeout_seconds: int = 300,
    ):
        self.base_url = base_url.rstrip("/")
        self.parsed_base = urlparse(self.base_url)
        self.base_domain = self.parsed_base.netloc
        self.output_dir = Path(output_dir)
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.js_render = js_render
        self.delay = delay
        self.same_domain_only = same_domain_only
        self.on_progress = on_progress
        self.timeout_seconds = timeout_seconds

        self.visited_urls: set[str] = set()
        self.downloaded_assets: dict[str, Path] = {}
        self.queue: deque = deque()

        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── URL helpers ──────────────────────────────────────────────────────────

    def normalize_url(self, url: str, base: str | None = None) -> Optional[str]:
        if not url or url.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
            return None
        if url.startswith("//"):
            url = self.parsed_base.scheme + ":" + url

        absolute = urljoin(base or self.base_url, url)
        parsed = urlparse(absolute)

        if self.same_domain_only and parsed.netloc != self.base_domain:
            return None

        return urlunparse(parsed._replace(fragment=""))

    def url_to_local_path(self, url: str) -> Path:
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")

        if not path or path.endswith("/"):
            path = path + "index.html"
        elif "." not in Path(path).name:
            path = path + "/index.html"

        if parsed.query:
            qhash = hashlib.md5(parsed.query.encode()).hexdigest()[:8]
            p = Path(path)
            path = str(p.parent / f"{p.stem}_{qhash}{p.suffix or '.html'}")

        return self.output_dir / path

    def asset_local_path(self, url: str) -> Path:
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")
        domain = parsed.netloc

        if domain and domain != self.base_domain:
            base = self.output_dir / "_external" / domain / path
        else:
            base = self.output_dir / path if path else self.output_dir / "_assets" / hashlib.md5(url.encode()).hexdigest()

        return base

    def relative(self, from_file: Path, to_file: Path) -> str:
        try:
            return os.path.relpath(to_file, from_file.parent).replace("\\", "/")
        except ValueError:
            return str(to_file)

    def safe_mkdir(self, path: Path):
        """Create directory, resolving file/dir conflicts along the path."""
        parts = list(path.parts)
        current = Path(parts[0])
        for part in parts[1:]:
            current = current / part
            if current.exists() and current.is_file():
                # A file is blocking directory creation — move it to index.html inside a new dir
                tmp = current.with_suffix(".tmp_swap")
                current.rename(tmp)
                current.mkdir(parents=True, exist_ok=True)
                tmp.rename(current / "index.html")
        path.mkdir(parents=True, exist_ok=True)

    # ── Asset downloading ────────────────────────────────────────────────────

    async def download_asset(self, url: str, client: httpx.AsyncClient) -> Optional[Path]:
        if not url or url.startswith("data:"):
            return None

        if url.startswith("//"):
            url = self.parsed_base.scheme + ":" + url

        abs_url = urljoin(self.base_url, url) if not urlparse(url).scheme else url

        if abs_url in self.downloaded_assets:
            return self.downloaded_assets[abs_url]

        local = self.asset_local_path(abs_url)

        if local.exists() and local.is_file():
            self.downloaded_assets[abs_url] = local
            return local

        try:
            resp = await client.get(abs_url, follow_redirects=True, timeout=20)
            if resp.status_code == 200:
                self.safe_mkdir(local.parent)
                local.write_bytes(resp.content)
                self.downloaded_assets[abs_url] = local
                print(f"    ↓ {abs_url[:90]}")
                return local
        except Exception as exc:
            print(f"    ✗ asset {abs_url[:70]}: {exc}")

        return None

    async def process_css(
        self, content: str, css_url: str, css_local: Path, client: httpx.AsyncClient
    ) -> str:
        url_re = re.compile(r'url\(\s*["\']?([^"\')\s]+)["\']?\s*\)')
        result = content

        for match in url_re.finditer(content):
            raw = match.group(1)
            if raw.startswith("data:"):
                continue
            abs_url = urljoin(css_url, raw)
            local = await self.download_asset(abs_url, client)
            if local:
                rel = self.relative(css_local, local)
                result = result.replace(match.group(0), f'url("{rel}")')

        # Follow @import
        import_re = re.compile(r'@import\s+["\']([^"\']+)["\']')
        for match in import_re.finditer(content):
            raw = match.group(1)
            abs_url = urljoin(css_url, raw)
            local = await self.download_asset(abs_url, client)
            if local:
                # Recursively process imported CSS
                try:
                    sub_css = local.read_text(encoding="utf-8", errors="ignore")
                    processed = await self.process_css(sub_css, abs_url, local, client)
                    local.write_text(processed, encoding="utf-8")
                except Exception:
                    pass
                rel = self.relative(css_local, local)
                result = result.replace(match.group(0), f'@import "{rel}"')

        return result

    # ── HTML rewriting ───────────────────────────────────────────────────────

    async def rewrite_html(
        self, html: str, page_url: str, local_path: Path, client: httpx.AsyncClient
    ) -> str:
        soup = BeautifulSoup(html, "html.parser")

        # Tags whose src/href point to downloadable assets
        ASSET_TAGS: dict[str, list[str]] = {
            "script": ["src"],
            "img": ["src", "data-src", "data-lazy-src", "data-original"],
            "source": ["src"],
            "video": ["src", "poster"],
            "audio": ["src"],
            "track": ["src"],
            "embed": ["src"],
            "object": ["data"],
            "input": ["src"],
        }

        for tag_name, attrs in ASSET_TAGS.items():
            for tag in soup.find_all(tag_name):
                for attr in attrs:
                    raw = tag.get(attr)
                    if not raw or raw.startswith(("data:", "javascript:", "#")):
                        continue
                    abs_url = urljoin(page_url, raw)
                    local_asset = await self.download_asset(abs_url, client)
                    if local_asset:
                        tag[attr] = self.relative(local_path, local_asset)

        # <link> tags — stylesheets get CSS processing, others get downloaded
        for tag in soup.find_all("link", href=True):
            raw = tag["href"]
            if raw.startswith(("data:", "javascript:", "#")):
                continue
            abs_url = urljoin(page_url, raw)
            local_asset = await self.download_asset(abs_url, client)
            if local_asset:
                rel_list = tag.get("rel", [])
                if "stylesheet" in rel_list and local_asset.exists():
                    try:
                        css = local_asset.read_text(encoding="utf-8", errors="ignore")
                        processed = await self.process_css(css, abs_url, local_asset, client)
                        local_asset.write_text(processed, encoding="utf-8")
                    except Exception:
                        pass
                tag["href"] = self.relative(local_path, local_asset)

        # srcset attributes
        for tag in soup.find_all(attrs={"srcset": True}):
            parts = []
            for entry in tag["srcset"].split(","):
                entry = entry.strip()
                if not entry:
                    continue
                pieces = entry.split()
                raw = pieces[0]
                abs_url = urljoin(page_url, raw)
                local_asset = await self.download_asset(abs_url, client)
                if local_asset:
                    pieces[0] = self.relative(local_path, local_asset)
                parts.append(" ".join(pieces))
            tag["srcset"] = ", ".join(parts)

        # Inline style url()
        for tag in soup.find_all(style=True):
            style = tag["style"]
            url_re = re.compile(r'url\(\s*["\']?([^"\')\s]+)["\']?\s*\)')
            for match in url_re.finditer(style):
                raw = match.group(1)
                if raw.startswith("data:"):
                    continue
                abs_url = urljoin(page_url, raw)
                local_asset = await self.download_asset(abs_url, client)
                if local_asset:
                    rel = self.relative(local_path, local_asset)
                    style = style.replace(match.group(0), f'url("{rel}")')
            tag["style"] = style

        # <style> blocks
        for tag in soup.find_all("style"):
            if tag.string:
                processed = await self.process_css(tag.string, page_url, local_path, client)
                tag.string = processed

        # Rewrite internal <a> links
        for tag in soup.find_all("a", href=True):
            normalized = self.normalize_url(tag["href"], page_url)
            if normalized:
                link_local = self.url_to_local_path(normalized)
                tag["href"] = self.relative(local_path, link_local)

        # Remove existing <base> tags to prevent path conflicts
        for base_tag in soup.find_all("base"):
            base_tag.decompose()

        return str(soup)

    def extract_links(self, html: str, page_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links = []
        for tag in soup.find_all("a", href=True):
            normalized = self.normalize_url(tag["href"], page_url)
            if normalized and normalized not in self.visited_urls:
                links.append(normalized)
        return links

    # ── Playwright fetch ─────────────────────────────────────────────────────

    async def fetch_js(self, page, url: str, client: httpx.AsyncClient) -> str:
        html = ""
        try:
            await page.goto(url, wait_until="load", timeout=30000)
            await asyncio.sleep(1.5)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.5)
            await page.evaluate("window.scrollTo(0, 0)")
            html = await page.content()
        except Exception as exc:
            print(f"  ✗ playwright: {exc}")

        # Fallback to plain httpx if Playwright returned nothing
        if not html or len(html) < 200:
            print(f"  ↩ falling back to httpx for {url}")
            try:
                resp = await client.get(url, timeout=30)
                html = resp.text
            except Exception as exc:
                print(f"  ✗ httpx fallback: {exc}")

        return html

    # ── Main crawler ─────────────────────────────────────────────────────────

    async def clone(self):
        import time
        self._start_time = time.time()
        print(f"\n{'─'*60}")
        print(f"  Cloning : {self.base_url}")
        print(f"  Output  : {self.output_dir.resolve()}")
        print(f"  Depth   : {self.max_depth}   Max pages: {self.max_pages}")
        print(f"  JS      : {'yes (Playwright)' if self.js_render else 'no'}")
        print(f"{'─'*60}\n")

        async with async_playwright() as pw:
            browser = page = None
            if self.js_render:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
                )
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"
                    )
                )
                page = await ctx.new_page()

            async with httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0 (compatible; SiteCloner/1.0)"},
                follow_redirects=True,
                verify=False,
                timeout=30,
            ) as client:
                self.queue.append((self.base_url, 0))
                self.visited_urls.add(self.base_url)

                while self.queue and len(self.visited_urls) <= self.max_pages:
                    if time.time() - self._start_time > self.timeout_seconds:
                        print(f"  ⏱ Timeout reached ({self.timeout_seconds}s) — stopping crawl.")
                        break

                    url, depth = self.queue.popleft()
                    n = len(self.visited_urls)
                    print(f"[{n}/{self.max_pages}] depth={depth}  {url}")
                    if self.on_progress:
                        self.on_progress(n, url)

                    try:
                        if self.js_render and page:
                            html = await self.fetch_js(page, url, client)
                        else:
                            resp = await client.get(url, timeout=30)
                            html = resp.text
                    except Exception as exc:
                        print(f"  ✗ fetch error: {exc}")
                        html = ""

                    if not html:
                        continue

                    local_path = self.url_to_local_path(url)
                    self.safe_mkdir(local_path.parent)

                    rewritten = await self.rewrite_html(html, url, local_path, client)
                    local_path.write_text(rewritten, encoding="utf-8")

                    if depth < self.max_depth:
                        for link in self.extract_links(html, url):
                            if link not in self.visited_urls:
                                self.visited_urls.add(link)
                                self.queue.append((link, depth + 1))


                    await asyncio.sleep(self.delay)

            if browser:
                await browser.close()

        pages_done = len(self.visited_urls)
        assets_done = len(self.downloaded_assets)
        print(f"\n{'─'*60}")
        print(f"  Done!  {pages_done} pages  •  {assets_done} assets")
        print(f"  Saved to: {self.output_dir.resolve()}")
        print(f"\n  Serve locally:")
        print(f"    cd \"{self.output_dir.resolve()}\" && python3 -m http.server 8080")
        print(f"  Then open: http://localhost:8080")
        print(f"{'─'*60}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Clone any website — saves a fully offline copy with all assets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("url", help="URL of the website to clone")
    parser.add_argument("-o", "--output", default="cloned_site", help="Output directory")
    parser.add_argument("-d", "--depth", type=int, default=3, help="Max crawl depth")
    parser.add_argument("-p", "--pages", type=int, default=500, help="Max pages to clone")
    parser.add_argument("--no-js", action="store_true", help="Disable JS rendering (faster, less accurate)")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between requests")
    parser.add_argument("--allow-external", action="store_true", help="Follow links to other domains")
    args = parser.parse_args()

    cloner = WebsiteCloner(
        base_url=args.url,
        output_dir=args.output,
        max_depth=args.depth,
        max_pages=args.pages,
        js_render=not args.no_js,
        delay=args.delay,
        same_domain_only=not args.allow_external,
    )

    asyncio.run(cloner.clone())


if __name__ == "__main__":
    main()
