"""Page fetching and DoclingDocument naming.

Conversion itself happens in docling-pipeline (see conversion.py); the uploader
only fetches pages for scrape jobs and names the documents it sends.
"""

from pathlib import Path

from curl_cffi import requests as browser_requests

# Documents are sent to the RAG backend as DoclingDocument JSON under this suffix,
# which is how the backend tells them apart from raw text uploads.
DOCLING_SUFFIX = ".docling.json"

# Fetch pages with a real browser's TLS/HTTP fingerprint and headers. Many sites
# (Cloudflare, Akamai, Varnish) reject Python's default client outright.
BROWSER_IMPERSONATE = "chrome"


def docling_filename(name: str) -> str:
    """'report.pdf' -> 'report.docling.json'"""
    return Path(name).with_suffix(DOCLING_SUFFIX).name


def fetch_page(url) -> tuple[bytes, str]:
    """Fetch *url* like a browser; return (body, content type)."""
    try:
        response = browser_requests.get(url, impersonate=BROWSER_IMPERSONATE, timeout=15)
    except Exception as e:
        raise ValueError(f"Request failed: {e}") from e
    if response.status_code >= 400:
        raise ValueError(f"Request failed: HTTP {response.status_code} from {url}")
    return response.content, response.headers.get("content-type", "")
