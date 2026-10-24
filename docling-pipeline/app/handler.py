"""AWS Lambda handler: SQS message -> docling conversion -> DoclingDocument JSON in S3.

The output is what the Infochat uploader produces itself and llm-backend's
/embed expects: DoclingDocument JSON named "<name>.docling.json", without
embedded images, keys sorted (so identical inputs give identical bytes), and
origin.uri set to the document's link, which the backend cites.

Accepted SQS message bodies:

1. S3 event notification (S3 -> SQS, or S3 -> SNS -> SQS):
   {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": "k"}}}]}

2. Direct request:
   {"bucket": "b", "key": "path/file.pdf",
    "output_bucket": "optional", "output_key": "optional/file.docling.json",
    "source_url": "optional link to cite",
    "scraped": false, "content_type": "optional, for scraped pages",
    "context": {"opaque": "echoed back in the result"}}
   or {"s3_uri": "s3://b/path/file.pdf", ...}

"scraped": true marks a fetched web page stored without an extension: PDFs are
detected by content, anything else is cleaned like the uploader's scraper
(nav/footer/breadcrumbs/... removed) and converted as HTML.

Results published to RESULT_QUEUE_URL: every success (plus "content_hash", the
sha256 of the output bytes, and the job's "context"), and an error result
{"status": "error", "source", "error", "context", "message_id"} on a message's
final attempt (ApproximateReceiveCount >= MAX_RECEIVE_COUNT).

origin.uri comes from "source_url" in the message, else the source object's
"source-url" metadata (x-amz-meta-source-url), else, for a saved HTML page,
the address the page records for itself.

Environment:
   OUTPUT_BUCKET     default output bucket (defaults to the source bucket)
   OUTPUT_PREFIX     key prefix for outputs (default "converted/")
   OUTPUT_FORMAT     "docling" (default): <name>.docling.json; "markdown": <name>.md
   RESULT_QUEUE_URL  optional SQS queue to publish each result to
   MAX_RECEIVE_COUNT the jobs queue's redrive maxReceiveCount (default 3): attempt
                     on which a failure is published as an error result
   OCR_MODE          "auto" (default): OCR only PDFs lacking a text layer, and images
                     "always" / "never": force OCR on / off
   PRELOAD_MODELS    "true" to load the PDF pipeline during Lambda init
   AWS_ENDPOINT_URL  set for LocalStack (boto3 picks it up automatically)
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import unquote_plus, urlparse

import boto3
from bs4 import BeautifulSoup
from botocore.config import Config
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
from pydantic import AnyUrl

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "converted/")
RESULT_QUEUE_URL = os.environ.get("RESULT_QUEUE_URL")
OUTPUT_FORMAT = os.environ.get("OUTPUT_FORMAT", "docling").strip().lower()
DOCLING_SUFFIX = ".docling.json"
MAX_RECEIVE_COUNT = int(os.environ.get("MAX_RECEIVE_COUNT", "3"))

# Ways a saved web page records its own address, most specific first
# (same list as the uploader's decling_conversion.py)
_SOURCE_URL_PATTERNS = [
    rb"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->",                      # Chrome "save page"
    rb"<meta[^>]+property=[\"']og:url[\"'][^>]+content=[\"']([^\"']+)",  # og:url
    rb"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:url[\"']",
    rb"<link[^>]+rel=[\"']canonical[\"'][^>]+href=[\"']([^\"']+)",       # canonical
    rb"<link[^>]+href=[\"']([^\"']+)[\"'][^>]+rel=[\"']canonical[\"']",
]


# Scraped-page cleanup, same as the uploader's decling_conversion.convert_page
_NOISE_TAGS = ["header", "footer", "nav", "script", "style", "noscript", "aside"]
# Real breadcrumb classes ("breadcrumb", "breadcrumbs", "usda-breadcrumb-list"), but not
# layout modifiers like "with-breadcrumb" that some sites put on the main content area.
_BREADCRUMB_CLASS_RE = re.compile(r"^(?!(?:with|has)-).*breadcrumb", re.I)
_LOADER_RE = re.compile(r"loader|spinner", re.I)


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


# LocalStack needs path-style addressing; real AWS is fine either way.
_boto_config = Config(s3={"addressing_style": "path"}) if os.environ.get("AWS_ENDPOINT_URL") else None
s3 = boto3.client("s3", config=_boto_config)
sqs = boto3.client("sqs") if RESULT_QUEUE_URL else None


OCR_MODE = os.environ.get("OCR_MODE", "auto").strip().lower()
# auto mode: OCR a PDF if more than this share of pages have (almost) no text layer
TEXTLESS_PAGE_RATIO = 0.1
MIN_PAGE_CHARS = 50


def _build_converter(do_ocr: bool) -> DocumentConverter:
    pdf_options = PdfPipelineOptions(
        artifacts_path=os.environ.get("DOCLING_ARTIFACTS_PATH"),
        do_ocr=do_ocr,
        do_table_structure=True,
        accelerator_options=AcceleratorOptions(
            num_threads=os.cpu_count() or 2, device=AcceleratorDevice.CPU
        ),
    )
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pdf_options),
        }
    )


# Built lazily, once per container, so warm invocations reuse the loaded models.
_converters: dict[bool, DocumentConverter] = {}


def _get_converter(do_ocr: bool) -> DocumentConverter:
    if do_ocr not in _converters:
        _converters[do_ocr] = _build_converter(do_ocr)
    return _converters[do_ocr]


def _needs_ocr(path: Path) -> bool:
    if OCR_MODE in ("always", "never"):
        return OCR_MODE == "always"
    suffix = path.suffix.lower()
    if suffix != ".pdf":
        # Images need OCR to produce any text; other formats ignore the flag.
        return suffix in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            n_pages = len(pdf)
            textless = sum(
                1 for i in range(n_pages)
                if len(pdf[i].get_textpage().get_text_bounded().strip()) < MIN_PAGE_CHARS
            )
        finally:
            pdf.close()
    except Exception:
        logger.warning("Text-layer check failed for %s; using OCR", path.name, exc_info=True)
        return True
    logger.info("Text-layer check: %d/%d pages without text", textless, n_pages)
    return n_pages == 0 or textless / n_pages > TEXTLESS_PAGE_RATIO


if _env_flag("PRELOAD_MODELS", "false"):
    _t = time.time()
    _get_converter(OCR_MODE == "always").initialize_pipeline(InputFormat.PDF)
    logger.info("Preloaded PDF pipeline in %.1fs", time.time() - _t)


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"Not an s3:// URI: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def _jobs_from_body(body: dict) -> list[dict]:
    """Normalise one SQS message body into a list of conversion jobs."""
    # SNS envelope (S3 -> SNS -> SQS without raw delivery)
    if body.get("Type") == "Notification" and "Message" in body:
        return _jobs_from_body(json.loads(body["Message"]))

    # S3 event notification
    if "Records" in body:
        return [
            {
                "bucket": r["s3"]["bucket"]["name"],
                "key": unquote_plus(r["s3"]["object"]["key"]),
            }
            for r in body["Records"]
            if "s3" in r
        ]

    # S3 sends this once when a notification is configured
    if body.get("Event") == "s3:TestEvent":
        return []

    job = dict(body)
    if "s3_uri" in job:
        job["bucket"], job["key"] = _parse_s3_uri(job.pop("s3_uri"))
    if not job.get("bucket") or not job.get("key"):
        raise ValueError(f"Message needs bucket+key or s3_uri: {body}")
    return [job]


def _output_location(job: dict) -> tuple[str, str]:
    out_bucket = job.get("output_bucket") or OUTPUT_BUCKET or job["bucket"]
    suffix = ".md" if OUTPUT_FORMAT == "markdown" else DOCLING_SUFFIX
    out_key = job.get("output_key") or f"{OUTPUT_PREFIX}{Path(job['key']).with_suffix(suffix)}"
    return out_bucket, out_key


def find_source_url(html: bytes) -> str | None:
    """The original web address a saved HTML page recorded for itself, if any."""
    for pattern in _SOURCE_URL_PATTERNS:
        match = re.search(pattern, html, re.IGNORECASE)
        if match and match.group(1).startswith(b"http"):
            return match.group(1).decode()
    return None


def _is_pdf(content: bytes, content_type: str = "") -> bool:
    return "application/pdf" in content_type.lower() or content[:5] == b"%PDF-"


def _word_count(el) -> int:
    return len(el.get_text(" ", strip=True).split())


def _wraps_page_content(el, page_words: int) -> bool:
    """True if removing `el` would take the page's main content with it."""
    if el.name in ("html", "body", "main", "article"):
        return True
    if el.find(["main", "article"]):
        return True
    return page_words > 0 and _word_count(el) > page_words * 0.5


def _url_filename(url: str, extension: str) -> str:
    """A file name for a fetched URL, so the document's title falls back to something readable."""
    stem = PurePosixPath(urlparse(url).path).stem or urlparse(url).netloc or "page"
    return re.sub(r"[^\w\-]", "_", stem) + extension


def clean_page(content: bytes) -> bytes:
    """Strip navigation and clutter from a scraped HTML page (uploader's convert_page)."""
    # Pass bytes so BeautifulSoup picks the charset from the page itself
    soup = BeautifulSoup(content, "html.parser")

    # If the page is essentially empty (JS redirect, login wall, etc.)
    # there is nothing to convert — bail early with a clear message.
    if len(soup.get_text(strip=True)) < 50:
        raise ValueError("Page returned no usable content (may require login or redirect)")

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
    return str(soup).encode("utf-8")


def _prepare_scraped(local_path: Path, job: dict) -> Path:
    """A scraped page is stored without an extension: give docling a named PDF or cleaned HTML."""
    content = local_path.read_bytes()
    url = job.get("source_url") or job["key"]
    if _is_pdf(content, job.get("content_type") or ""):
        target = local_path.with_name(_url_filename(url, ".pdf"))
    else:
        target = local_path.with_name(_url_filename(url, ".html"))
        content = clean_page(content)
    local_path.unlink()
    target.write_bytes(content)
    return target


def to_docling_json(doc) -> str:
    """Serialize without embedded image data: the backend only embeds text, and images
    (e.g. pictures inside a DOCX) can make the payload many MB. Keys are sorted so
    identical documents give identical strings (scrape jobs hash them to detect changes)."""
    data = doc.export_to_dict()
    for picture in data.get("pictures", []):
        picture.pop("image", None)
    for page in data.get("pages", {}).values():
        page.pop("image", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _download(bucket: str, key: str, tmp: str) -> tuple[Path, dict]:
    """Stream an S3 object into tmp; returns (local path, get_object response)."""
    local_path = Path(tmp) / Path(key).name
    obj = s3.get_object(Bucket=bucket, Key=key)
    with open(local_path, "wb") as fh:
        for chunk in obj["Body"].iter_chunks(1024 * 1024):
            fh.write(chunk)
    logger.info("Downloaded s3://%s/%s (%d bytes)", bucket, key, local_path.stat().st_size)
    return local_path, obj


def _require_content(doc) -> None:
    if not doc.tables and not any(t.text.strip() for t in doc.texts):
        raise ValueError("Document converted to empty content")


def _convert(job: dict) -> dict:
    bucket, key = job["bucket"], job["key"]
    out_bucket, out_key = _output_location(job)

    # Folder markers (e.g. created in the S3 console) aren't documents.
    if key.endswith("/"):
        logger.info("Skipping folder marker s3://%s/%s", bucket, key)
        return {"status": "skipped", "source": f"s3://{bucket}/{key}"}

    # Avoid infinite loops when S3 notifications fire on our own output.
    if out_bucket == bucket and OUTPUT_PREFIX and key.startswith(OUTPUT_PREFIX):
        logger.info("Skipping s3://%s/%s (already under output prefix)", bucket, key)
        return {"status": "skipped", "source": f"s3://{bucket}/{key}"}

    started = time.time()
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        local_path, obj = _download(bucket, key, tmp)
        if job.get("scraped"):
            local_path = _prepare_scraped(local_path, job)

        do_ocr = _needs_ocr(local_path)
        result = _get_converter(do_ocr).convert(local_path)
        doc = result.document
        _require_content(doc)

        if OUTPUT_FORMAT == "markdown":
            body, content_type = doc.export_to_markdown(), "text/markdown; charset=utf-8"
        else:
            source_url = job.get("source_url") or obj.get("Metadata", {}).get("source-url")
            if source_url is None and result.input.format == InputFormat.HTML:
                source_url = find_source_url(local_path.read_bytes())
            if source_url:
                doc.origin.uri = AnyUrl(source_url)
            body, content_type = to_docling_json(doc), "application/json"

    data = body.encode("utf-8")
    s3.put_object(
        Bucket=out_bucket,
        Key=out_key,
        Body=data,
        ContentType=content_type,
        Metadata={"source": f"s3://{bucket}/{key}"},
    )
    output = {
        "status": "success",
        "source": f"s3://{bucket}/{key}",
        "output": f"s3://{out_bucket}/{out_key}",
        "output_bucket": out_bucket,
        "output_key": out_key,
        "ocr": do_ocr,
        "seconds": round(time.time() - started, 2),
        "content_hash": hashlib.sha256(data).hexdigest(),
        "context": job.get("context") or {},
    }
    logger.info("Converted: %s", json.dumps(output))
    return output


# Lambda caps a synchronous response at 6 MB; Markdown above this many characters is cut.
DEFAULT_MAX_CHARS = 1_000_000


def _convert_inline(job: dict) -> dict:
    """Direct invocation (lambda.invoke, e.g. universal-chat's attachments): convert
    {"bucket", "key"} and return the Markdown in the reply instead of writing to S3.

    Returns {"status": "success", "markdown", "truncated", "ocr", "seconds"} or
    {"status": "error", "error"}; errors are returned, not raised, so the caller
    gets a message it can show rather than a Lambda function error.
    """
    started = time.time()
    try:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            local_path, _ = _download(job["bucket"], job["key"], tmp)
            do_ocr = _needs_ocr(local_path)
            doc = _get_converter(do_ocr).convert(local_path).document
            _require_content(doc)
            markdown = doc.export_to_markdown()
    except Exception as exc:
        logger.exception("Inline conversion of s3://%s/%s failed", job.get("bucket"), job.get("key"))
        return {"status": "error", "error": str(exc)}
    max_chars = int(job.get("max_chars") or DEFAULT_MAX_CHARS)
    result = {
        "status": "success",
        "markdown": markdown[:max_chars],
        "truncated": len(markdown) > max_chars,
        "ocr": do_ocr,
        "seconds": round(time.time() - started, 2),
    }
    logger.info("Converted inline s3://%s/%s: %d chars in %.1fs",
                job["bucket"], job["key"], len(markdown), result["seconds"])
    return result


def lambda_handler(event, context):
    if "Records" not in (event or {}):
        return _convert_inline(event or {})

    results = []
    failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId")
        job = None
        try:
            body = json.loads(record["body"])
            for job in _jobs_from_body(body):
                result = _convert(job)
                result["message_id"] = message_id
                results.append(result)
                if sqs and result["status"] == "success":
                    sqs.send_message(QueueUrl=RESULT_QUEUE_URL, MessageBody=json.dumps(result))
        except Exception as exc:
            logger.exception("Failed to process message %s", message_id)
            failures.append({"itemIdentifier": message_id})
            error = {"status": "error", "message_id": message_id, "error": str(exc)}
            if job is not None:
                error["source"] = f"s3://{job['bucket']}/{job['key']}"
                error["context"] = job.get("context") or {}
                # Earlier attempts are retried by SQS; only the last one is reported.
                attempt = int(record.get("attributes", {}).get("ApproximateReceiveCount", 1))
                if sqs and attempt >= MAX_RECEIVE_COUNT:
                    sqs.send_message(QueueUrl=RESULT_QUEUE_URL, MessageBody=json.dumps(error))
            results.append(error)

    # batchItemFailures makes SQS retry only the failed messages
    # (requires ReportBatchItemFailures on the event source mapping).
    return {"batchItemFailures": failures, "results": results}
