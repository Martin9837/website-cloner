#!/bin/bash
set -e

echo "Installing dependencies..."
pip install -r requirements.txt

echo "Installing Playwright browser..."
playwright install chromium

echo ""
echo "Done! Usage:"
echo "  python3 cloner.py https://example.com -o my_clone"
echo "  python3 cloner.py https://example.com -o my_clone --depth 5 --pages 1000"
echo "  python3 cloner.py https://example.com --no-js   # faster, skip JS rendering"
echo ""
echo "Options:"
echo "  -o, --output     Output directory (default: cloned_site)"
echo "  -d, --depth      How many link-levels deep to crawl (default: 3)"
echo "  -p, --pages      Max pages to clone (default: 500)"
echo "  --no-js          Skip JS rendering (much faster)"
echo "  --delay          Seconds between requests (default: 0.5)"
echo "  --allow-external Follow links to other domains too"
