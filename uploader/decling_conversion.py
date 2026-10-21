import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as browser_requests

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import DocumentStream
from docling_core.types.doc import DoclingDocument
from pydantic import AnyUrl

from io import BytesIO

converter = DocumentConverter()

# Documents are sent to the RAG backend as DoclingDocument JSON under this suffix,
# which is how the backend tells them apart from raw text uploads.
DOCLING_SUFFIX = ".docling.json"

# Ways a saved web page records its own address, most specific first
_SOURCE_URL_PATTERNS = [
    rb"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->",                      # Chrome "save page"
    rb"<meta[^>]+property=[\"']og:url[\"'][^>]+content=[\"']([^\"']+)",  # og:url
    rb"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:url[\"']",
    rb"<link[^>]+rel=[\"']canonical[\"'][^>]+href=[\"']([^\"']+)",       # canonical
    rb"<link[^>]+href=[\"']([^\"']+)[\"'][^>]+rel=[\"']canonical[\"']",
]

# Fetch pages with a real browser's TLS/HTTP fingerprint and headers. Many sites
# (Cloudflare, Akamai, Varnish) reject Python's default client outright.
BROWSER_IMPERSONATE = "chrome"
_NOISE_TAGS = ["header", "footer", "nav", "script", "style", "noscript", "aside"]

# Real breadcrumb classes ("breadcrumb", "breadcrumbs", "usda-breadcrumb-list"), but not
# layout modifiers like "with-breadcrumb" that some sites put on the main content area.
_BREADCRUMB_CLASS_RE = re.compile(r"^(?!(?:with|has)-).*breadcrumb", re.I)
_LOADER_RE = re.compile(r"loader|spinner", re.I)


def _is_pdf(response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return "application/pdf" in content_type or response.content[:5] == b"%PDF-"


def _word_count(el) -> int:
    return len(el.get_text(" ", strip=True).split())


def _wraps_page_content(el, page_words: int) -> bool:
    """True if removing `el` would take the page's main content with it."""
    if el.name in ("html", "body", "main", "article"):
        return True
    if el.find(["main", "article"]):
        return True
    return page_words > 0 and _word_count(el) > page_words * 0.5


def docling_filename(name: str) -> str:
    """'report.pdf' -> 'report.docling.json'"""
    return Path(name).with_suffix(DOCLING_SUFFIX).name


def _url_filename(url: str, extension: str) -> str:
    """A file name for a fetched URL, so the document's title falls back to something readable."""
    stem = PurePosixPath(urlparse(url).path).stem or urlparse(url).netloc or "page"
    return re.sub(r"[^\w\-]", "_", stem) + extension


def scrape_website(url):
    """Fetch *url* and return it as DoclingDocument JSON with origin.uri set to *url*."""
    try:
        response = browser_requests.get(url, impersonate=BROWSER_IMPERSONATE, timeout=15)
    except Exception as e:
        raise ValueError(f"Request failed: {e}") from e
    if response.status_code >= 400:
        raise ValueError(f"Request failed: HTTP {response.status_code} from {url}")

    if _is_pdf(response):
        try:
            return convert_file(response.content, _url_filename(url, ".pdf"), source_url=url)
        except Exception as e:
            raise ValueError(f"Could not convert PDF: {e}") from e

    # Pass bytes so BeautifulSoup picks the charset from the page itself
    soup = BeautifulSoup(response.content, "html.parser")

    # If the page is essentially empty (JS redirect, login wall, etc.)
    # there is nothing to convert — bail early with a clear message.
    if len(soup.get_text(strip=True)) < 50:
        raise ValueError("Page returned no usable content (may require login or redirect)")

    # Strip noise tags (header/footer/nav + scripts/styles)
    for tag in soup.find_all(_NOISE_TAGS):
        tag.extract()

    # Forms, spinners and breadcrumbs are usually clutter, but some sites wrap the whole
    # page in them (ASP.NET's page-level <form>, class="with-breadcrumb" on <main>),
    # so never remove one that holds the page's main content.
    page_words = _word_count(soup)
    candidates = (
        soup.find_all("form")
        + soup.find_all(id=_LOADER_RE)
        + soup.find_all(class_=_LOADER_RE)
        + soup.find_all(class_=_BREADCRUMB_CLASS_RE)
    )
    for el in candidates:
        if el.parent is not None and not _wraps_page_content(el, page_words):
            el.extract()

    try:
        return convert_file(str(soup).encode("utf-8"), _url_filename(url, ".html"), source_url=url)
    except Exception as e:
        raise ValueError(f"Could not convert page content: {e}") from e


def find_source_url(html: bytes) -> str | None:
    """The original web address a saved HTML page recorded for itself, if any."""
    for pattern in _SOURCE_URL_PATTERNS:
        match = re.search(pattern, html, re.IGNORECASE)
        if match and match.group(1).startswith(b"http"):
            return match.group(1).decode()
    return None


def _has_content(doc: DoclingDocument) -> bool:
    return bool(doc.tables) or any(t.text.strip() for t in doc.texts)


def _to_json(doc: DoclingDocument) -> str:
    """Serialize without embedded image data: the backend only embeds text, and images
    (e.g. pictures inside a DOCX) can make the payload many MB. Keys are sorted so
    identical documents give identical strings, which scrape jobs hash to detect changes."""
    data = doc.export_to_dict()
    for picture in data.get("pictures", []):
        picture.pop("image", None)
    for page in data.get("pages", {}).values():
        page.pop("image", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def convert_file(file_bytes, file_name, source_url=None):
    """Convert a file to DoclingDocument JSON.

    origin.uri is what the backend cites as the document's link. Docling only sets it
    when converting straight from a URL, so it is set here from *source_url*, or for
    HTML files from the address the saved page recorded for itself.
    """
    stream = DocumentStream(name=file_name, stream=BytesIO(file_bytes))
    result = converter.convert(stream)
    doc = result.document
    if not _has_content(doc):
        raise ValueError("Document converted to empty content")

    if source_url is None and result.input.format == InputFormat.HTML:
        source_url = find_source_url(file_bytes)
    if source_url:
        doc.origin.uri = AnyUrl(source_url)

    return _to_json(doc)