"""Handler tests. Run inside the image (models baked in, no network), see README."""

import hashlib
import json

import pytest

import handler

BUCKET = "state"
ORIG = "workspaces/s/uploader/originals/K/"
OUT_KEY = "workspaces/s/uploader/converted/K/report.docling.json"
CONTEXT = {"workspace_id": "s", "key": "K", "extra": [1, 2]}

PAGE = b"""<html><head><title>Admissions</title></head><body>
<header>Site header SHOULD_NOT_APPEAR</header>
<nav><a href="/">Home NAV_LINK</a></nav>
<form id="aspnetForm">
  <ul class="breadcrumb"><li>Home</li><li>BREADCRUMB_TRAIL</li></ul>
  <main><h1>Undergraduate Admissions</h1>
  <p>Applications for the fall semester are due on February first. Submit transcripts,
  test scores and two letters of recommendation through the online portal.</p></main>
</form>
<footer>Copyright FOOTER_TEXT</footer>
</body></html>"""


class FakeBody:
    def __init__(self, data):
        self.data = data

    def iter_chunks(self, size):
        yield self.data


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        return {"Body": FakeBody(self.objects[(Bucket, Key)]), "Metadata": {}}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = Body


class FakeSQS:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append(json.loads(MessageBody))


@pytest.fixture
def aws(monkeypatch):
    s3, sqs = FakeS3(), FakeSQS()
    monkeypatch.setattr(handler, "s3", s3)
    monkeypatch.setattr(handler, "sqs", sqs)
    monkeypatch.setattr(handler, "RESULT_QUEUE_URL", "http://results")
    return s3, sqs


def run(job, receive_count=1):
    record = {"messageId": "m1", "body": json.dumps(job),
              "attributes": {"ApproximateReceiveCount": str(receive_count)}}
    return handler.lambda_handler({"Records": [record]}, None)


def written_doc(s3, key=OUT_KEY):
    return json.loads(s3.objects[(BUCKET, key)])


def convert(aws, data, name, **job):
    """Store *data* as an original, convert it, return (result, written doc)."""
    s3, sqs = aws
    s3.objects[(BUCKET, ORIG + name)] = data
    out = run({"bucket": BUCKET, "key": ORIG + name, "output_bucket": BUCKET, "output_key": OUT_KEY, **job})
    assert out["batchItemFailures"] == [], out
    return sqs.sent[-1], written_doc(s3)


def doc_text(doc):
    return " ".join(t["text"] for t in doc["texts"])


ARTICLE = " ".join(f"Nutrition sentence number {i} about healthy eating." for i in range(40))


def test_direct_job_writes_output_and_publishes_success(aws):
    s3, sqs = aws
    s3.objects[(BUCKET, ORIG + "report.html")] = PAGE
    job = {"bucket": BUCKET, "key": ORIG + "report.html", "output_bucket": BUCKET,
           "output_key": OUT_KEY, "source_url": "https://example.edu/report", "context": CONTEXT}
    out = run(job)
    assert out["batchItemFailures"] == []
    [result] = sqs.sent
    assert result["status"] == "success"
    assert result["output"] == f"s3://{BUCKET}/{OUT_KEY}"
    assert result["context"] == CONTEXT
    assert result["content_hash"] == hashlib.sha256(s3.objects[(BUCKET, OUT_KEY)]).hexdigest()
    assert written_doc(s3)["origin"]["uri"] == "https://example.edu/report"


def test_missing_context_echoes_empty_dict(aws):
    s3, sqs = aws
    s3.objects[(BUCKET, ORIG + "report.html")] = PAGE
    run({"bucket": BUCKET, "key": ORIG + "report.html", "output_key": OUT_KEY})
    assert sqs.sent[0]["context"] == {}


@pytest.mark.parametrize("receive_count, published", [(1, False), (2, False), (3, True), (4, True)])
def test_failure_published_only_on_final_attempt(aws, receive_count, published):
    s3, sqs = aws  # object missing -> get_object raises
    job = {"bucket": BUCKET, "key": ORIG + "gone.pdf", "output_key": OUT_KEY, "context": CONTEXT}
    out = run(job, receive_count)
    assert out["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    if not published:
        assert sqs.sent == []
        return
    [result] = sqs.sent
    assert result["status"] == "error"
    assert result["source"] == f"s3://{BUCKET}/{ORIG}gone.pdf"
    assert result["context"] == CONTEXT
    assert result["message_id"] == "m1"
    assert result["error"]


def test_unparseable_body_fails_without_publishing(aws):
    _, sqs = aws
    record = {"messageId": "m1", "body": "not json", "attributes": {"ApproximateReceiveCount": "9"}}
    out = handler.lambda_handler({"Records": [record]}, None)
    assert out["batchItemFailures"] == [{"itemIdentifier": "m1"}]
    assert sqs.sent == []


def test_scraped_html_is_cleaned(aws):
    s3, sqs = aws
    key = ORIG + "https_example_edu_admissions_apply"  # no extension, like the uploader stores it
    s3.objects[(BUCKET, key)] = PAGE
    url = "https://example.edu/admissions/apply"
    run({"bucket": BUCKET, "key": key, "output_bucket": BUCKET, "output_key": OUT_KEY,
         "source_url": url, "scraped": True})
    assert sqs.sent[0]["status"] == "success"
    doc = written_doc(s3)
    text = " ".join(t["text"] for t in doc["texts"])
    assert "Applications for the fall semester" in text  # inside the page-level <form>
    assert "Undergraduate Admissions" in text
    for noise in ("SHOULD_NOT_APPEAR", "NAV_LINK", "BREADCRUMB_TRAIL", "FOOTER_TEXT"):
        assert noise not in text
    assert doc["origin"]["uri"] == url
    assert doc["name"] == "apply"


def test_scraped_clutter_removed_main_content_kept(aws):
    """Small forms, loaders and breadcrumbs go; a with-breadcrumb <main> and its article stay."""
    html = (
        '<html><body><div class="page-spinner">Loading...</div>'
        '<main class="usa-content with-breadcrumb">'
        '<ol class="usda-breadcrumb-list"><li class="usda-breadcrumb-item">Home Crumb</li></ol>'
        f"<h1>Meal Planning</h1><p>{ARTICLE}</p></main>"
        '<form><label>Subscribe to our newsletter today</label><input name="email"></form></body></html>'
    ).encode()
    _, doc = convert(aws, html, "https_example_gov_a", source_url="https://example.gov/a", scraped=True)
    text = doc_text(doc)
    assert "Meal Planning" in text and "sentence number 39" in text
    for clutter in ("Loading", "Home Crumb", "newsletter"):
        assert clutter not in text


def test_scraped_output_is_stable(aws):
    """Identical input gives identical bytes, so unchanged pages hash the same."""
    html = f"<html><head><title>Healthy Eating</title></head><body><p>{ARTICLE}</p></body></html>".encode()
    job = {"source_url": "https://example.com/food/healthy-eating", "scraped": True}
    first, doc = convert(aws, html, "page", **job)
    second, _ = convert(aws, html, "page", **job)
    assert first["content_hash"] == second["content_hash"]
    assert doc["origin"]["filename"] == "healthy-eating.html"


def test_scraped_page_empty_after_cleanup_fails(aws):
    s3, _ = aws
    s3.objects[(BUCKET, ORIG + "page")] = f"<html><body><nav>{ARTICLE}</nav></body></html>".encode()
    out = run({"bucket": BUCKET, "key": ORIG + "page", "output_key": OUT_KEY, "scraped": True})
    assert out["batchItemFailures"] and "empty content" in out["results"][0]["error"]


@pytest.mark.parametrize("content, content_type", [
    (b"%PDF-1.7 not really a pdf", None),
    (b"no magic here", "application/pdf"),
], ids=["magic-bytes", "content-type"])
def test_scraped_pdf_detected(aws, monkeypatch, content, content_type):
    s3, _ = aws
    key = ORIG + "https_example_edu_files_handbook"
    s3.objects[(BUCKET, key)] = content
    seen = {}

    class Stop(Exception):
        pass

    class FakeConverter:
        def convert(self, path):
            seen["name"], seen["data"] = path.name, path.read_bytes()
            raise Stop

    monkeypatch.setattr(handler, "_get_converter", lambda do_ocr: FakeConverter())
    monkeypatch.setattr(handler, "_needs_ocr", lambda path: False)
    run({"bucket": BUCKET, "key": key, "source_url": "https://example.edu/files/handbook",
         "scraped": True, "content_type": content_type})
    assert seen == {"name": "handbook.pdf", "data": content}


def test_scraped_empty_page_raises():
    with pytest.raises(ValueError, match="no usable content"):
        handler.clean_page(b"<html><body><script>location='/login'</script>Hi</body></html>")


SAVED_BODY = b"<body><h1>Adobe</h1><p>How to request an Adobe license for faculty and staff.</p></body></html>"
SAVED_URL = "https://sites.google.com/uri.edu/wiki/adobe"


@pytest.mark.parametrize("head, expected", [
    (b"<!-- saved from url=(0049)https://sites.google.com/uri.edu/wiki/adobe -->\n<html><head></head>", SAVED_URL),
    (b'<html><head><meta property="og:url" content="https://sites.google.com/uri.edu/wiki/adobe"></head>', SAVED_URL),
    (b'<html><head><meta content="https://sites.google.com/uri.edu/wiki/adobe" property="og:url"></head>', SAVED_URL),
    (b'<html><head><link rel="canonical" href="https://sites.google.com/uri.edu/wiki/adobe"></head>', SAVED_URL),
    (b'<html><head><link href="https://sites.google.com/uri.edu/wiki/adobe" rel="canonical"></head>', SAVED_URL),
    (b'<html><head><link rel="canonical" href="/wiki/adobe"></head>', None),
    (b"<html><head></head>", None),
], ids=["saved-from", "og-url", "og-url-reversed", "canonical", "canonical-reversed", "relative", "none"])
def test_saved_html_source_url(aws, head, expected):
    _, doc = convert(aws, head + SAVED_BODY, "Adobe.html")
    assert doc["origin"].get("uri") == expected


def test_explicit_source_url_wins(aws):
    head = b'<html><head><link rel="canonical" href="https://other.example/x"></head>'
    _, doc = convert(aws, head + SAVED_BODY, "Adobe.html", source_url="https://uri.edu/adobe")
    assert doc["origin"]["uri"] == "https://uri.edu/adobe"


def test_markdown_keeps_structure(aws):
    _, doc = convert(aws, b"# Wi-Fi\n\n## Gaming Devices\n\nRegister the MAC address.", "wifi.md")
    assert {t["label"] for t in doc["texts"]} & {"section_header", "title"}


# --- direct invocation (universal-chat attachments) -------------------------------

CHAT_KEY = "workspaces/s/chat/uploads/U/notes.md"


def test_inline_returns_markdown_and_writes_nothing(aws):
    s3, sqs = aws
    s3.objects[(BUCKET, CHAT_KEY)] = b"# Parking\n\n## Visitors\n\nVisitors park in lot 21."
    out = handler.lambda_handler({"bucket": BUCKET, "key": CHAT_KEY}, None)
    assert out["status"] == "success" and out["truncated"] is False
    assert "Visitors park in lot 21." in out["markdown"] and "# Parking" in out["markdown"]
    assert list(s3.objects) == [(BUCKET, CHAT_KEY)] and sqs.sent == []


def test_inline_truncates_to_max_chars(aws):
    s3, _ = aws
    s3.objects[(BUCKET, CHAT_KEY)] = ("# Long\n\n" + ARTICLE).encode()
    out = handler.lambda_handler({"bucket": BUCKET, "key": CHAT_KEY, "max_chars": 50}, None)
    assert out["truncated"] is True and len(out["markdown"]) == 50


def test_inline_error_is_returned_not_raised(aws):
    out = handler.lambda_handler({"bucket": BUCKET, "key": "workspaces/s/chat/uploads/U/missing.pdf"}, None)
    assert out["status"] == "error" and out["error"]
