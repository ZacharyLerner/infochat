"""
Integration tests for the uploader beyond test_s3_storage.py — a live-backend
smoke set, workspace and scrape-job routes, and the parsing helpers.

test_s3_storage.py is the core offline suite for the S3-backed state
(originals, job claiming, run-due, job-run change tracking, reembed, copy).

The live tests hit the REAL LLM backend at config.API_URL (env RAG_API_URL)
and are skipped when it can't be reached; everything else runs offline.
Uploader state lives in the in-memory `fake_s3` bucket from conftest.py —
real S3 is never touched.

Test workspace (must already exist on the backend): env UPLOADER_TEST_SLUG,
default claude-ui-test-omox6ptor0l3z29x. Documents a test embeds there are
removed again afterwards.

Test classes
============
  TestLiveBackend          — upload → doc on backend → delete; bulk delete;
                             413 embeds nothing; real single-page run fetches + queues the page
  TestWorkspaceRoutes      — create / get / duplicate 409 / rename via S3 records
  TestWorkspaceDelete      — delete route (backend call stubbed): 200 / 404 / 500
  TestScrapeJobCRUD        — job lifecycle, delete cascades to pages,
                             mode="list" URL handling and validation
  TestParseUrlList         — website-list parsing (separators, dedupe, invalid entries)
  TestSanitizeUrlToFilename / TestComputeNextRun — helper unit tests

Page cleanup and DoclingDocument conversion are tested in docling-pipeline/tests.
"""

import functools
import io
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests as _req

from fastapi.testclient import TestClient

import anythingllm
import conversion
import main
import store
from config import API_URL, HEADERS, MAX_UPLOAD_BYTES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BACKEND = API_URL
TEST_SLUG = os.getenv("UPLOADER_TEST_SLUG", "claude-ui-test-omox6ptor0l3z29x")   # exists on the real backend


# ---------------------------------------------------------------------------
# Backend connectivity — checked once, only by tests that need the backend.
# Skips them with a clear message if it is unreachable or the test workspace
# slug doesn't exist there, rather than failing with confusing 404 errors.
# ---------------------------------------------------------------------------
@functools.cache
def _backend_skip_reason() -> str | None:
    if not BACKEND:
        return "RAG_API_URL is not set"
    try:
        r = _req.get(f"{BACKEND}/api/workspace/{TEST_SLUG}", headers=HEADERS, timeout=5)
    except Exception as exc:
        return f"Cannot reach LLM backend at {BACKEND}: {exc}"
    if r.status_code == 404:
        # Auto-discover available workspaces and surface them
        try:
            slugs = [w["slug"] for w in _req.get(f"{BACKEND}/api/workspaces", headers=HEADERS, timeout=5).json()]
        except Exception:
            slugs = ["(could not list workspaces)"]
        return (
            f"TEST_SLUG {TEST_SLUG!r} not found on backend {BACKEND}. "
            f"Available workspaces: {slugs}. Set UPLOADER_TEST_SLUG to match."
        )
    if r.status_code != 200:
        return f"LLM backend returned {r.status_code} for workspace {TEST_SLUG!r}: {r.text[:200]}"
    return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _txt(name: str = "test.txt", content: str = "Hello."):
    """Multipart tuple for a plain-text upload."""
    return ("uploaded_files", (name, io.BytesIO(content.encode()), "text/plain"))


def _backend_doc_ids() -> set[str]:
    """Return the set of doc_ids currently tracked on the real backend."""
    r = _req.get(f"{BACKEND}/api/docs/{TEST_SLUG}", headers=HEADERS, timeout=10)
    assert r.status_code == 200, f"Could not list backend docs: {r.text}"
    return {d["doc_id"] for d in r.json()}


def _upload_n(client: TestClient, n: int) -> list[str]:
    """Upload n text files to TEST_SLUG; return list of doc_ids."""
    files = [_txt(f"file_{i}.txt", f"content {i}") for i in range(n)]
    r = client.post(f"/api/v1/workspaces/{TEST_SLUG}/upload", files=files)
    assert r.status_code == 200, r.text
    return [rec["id"] for rec in r.json()]


def _create_job(client: TestClient, **kwargs) -> dict:
    """Create a scrape job in TEST_SLUG and return its JSON response."""
    payload = {"name": "Test Job", "base_url": "https://example.com/", "mode": "single"}
    payload.update(kwargs)
    r = client.post(f"/{TEST_SLUG}/scrape/jobs", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(fake_s3):
    """A TestClient over an empty in-memory bucket holding just the test workspace."""
    store.create_workspace(TEST_SLUG, "Testing", [])
    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def live_backend(client):
    """Skip unless the real backend is reachable; afterwards remove any document
    the test embedded in TEST_SLUG that it didn't delete itself."""
    reason = _backend_skip_reason()
    if reason:
        pytest.skip(reason)
    yield
    # Only docs this test's (fresh, in-memory) uploader recorded, so documents
    # others add to the shared test workspace meanwhile are left alone.
    for doc_id in {f["id"] for f in store.list_files(TEST_SLUG)} & _backend_doc_ids():
        anythingllm.LLM_remove_document(TEST_SLUG, doc_id)


# ===========================================================================
# LIVE BACKEND SMOKE TESTS
# ===========================================================================

@pytest.mark.usefixtures("live_backend")
class TestLiveBackend:
    """
    Routes under test, against the real backend
      POST   /api/v1/workspaces/{id}/upload
      DELETE /{workspace_id}/delete/{file_id:path}
      POST   /{workspace_id}/delete-bulk
      POST   /{workspace_id}/scrape/jobs/{job_id}/run
    """

    def test_upload_then_delete(self, client: TestClient):
        """An upload lands on the backend with a record; delete removes both, then 404s."""
        r = client.post(f"/api/v1/workspaces/{TEST_SLUG}/upload", files=[_txt("NOTES.TXT", "Integration test.")])
        assert r.status_code == 200, r.text
        [rec] = r.json()
        assert (rec["workspace_id"], rec["filename"], rec["original_extension"], rec["category"]) == (
            TEST_SLUG, "NOTES.TXT", ".txt", "uploaded_file")
        assert rec["id"] in _backend_doc_ids()
        assert [f["id"] for f in store.list_files(TEST_SLUG)] == [rec["id"]]

        r = client.delete(f"/{TEST_SLUG}/delete/{rec['id']}")
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted": rec["id"]}
        assert store.list_files(TEST_SLUG) == []
        assert rec["id"] not in _backend_doc_ids()
        assert client.delete(f"/{TEST_SLUG}/delete/{rec['id']}").status_code == 404

    def test_bulk_delete_more_than_ten(self, client: TestClient):
        """Bulk delete 15 files (regression: only 10 were deleted); unknown ids are skipped."""
        doc_ids = _upload_n(client, 15)

        r = client.post(f"/{TEST_SLUG}/delete-bulk", json={"file_ids": doc_ids + ["ghost-id"]})
        assert r.status_code == 200, r.text
        assert set(r.json()["deleted"]) == set(doc_ids)
        assert store.list_files(TEST_SLUG) == []
        assert not _backend_doc_ids() & set(doc_ids)

    def test_upload_413_stops_on_first_oversized(self, client: TestClient):
        """When one file is too large, the request fails with 413 and nothing is embedded or recorded."""
        before = _backend_doc_ids()
        big = io.BytesIO(b"x" * (MAX_UPLOAD_BYTES + 1))
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[
                ("uploaded_files", ("ok.txt",   io.BytesIO(b"small"),  "text/plain")),
                ("uploaded_files", ("huge.txt",  big,                   "text/plain")),
                ("uploaded_files", ("ok2.txt",   io.BytesIO(b"small2"), "text/plain")),
            ],
        )
        assert r.status_code == 413
        assert store.list_files(TEST_SLUG) == []
        assert _backend_doc_ids() <= before

    def test_run_single_mode_fetches_and_queues_page(self, client: TestClient, monkeypatch):
        """A real single-page run fetches the page, stores it as the original and queues it for
        docling-pipeline (the conversion half is covered by test_s3_storage.py's fake pipeline)."""
        jobs = []
        monkeypatch.setattr(conversion, "send_job", lambda rec, name, **kw: jobs.append((rec, name, kw)))
        job = _create_job(client, mode="single", base_url="https://example.com/")

        with client.stream("POST", f"/{TEST_SLUG}/scrape/jobs/{job['id']}/run") as resp:
            assert resp.status_code == 200
            data_lines = [l for l in resp.iter_lines() if l.startswith("data:")]
        assert data_lines[-1] == "data: [DONE]"
        done = [json.loads(l[6:]) for l in data_lines[:-1] if json.loads(l[6:]).get("status") == "done"]
        assert len(done) == 1 and done[0]["page_count"] == 1

        [page] = store.list_files(TEST_SLUG)
        assert page["scrape_job_id"] == job["id"]
        assert page["source_url"] == "https://example.com/"
        assert page["status"] == "converting" and page["id"] is None
        assert b"Example Domain" in store.get_object(page["original"])
        [(rec, name, kw)] = jobs
        assert rec["key"] == page["key"] and name == "example_com_index.docling.json" and kw["scraped"] is True
        assert store.get_job(TEST_SLUG, job["id"])["is_running"] is False


# ===========================================================================
# WORKSPACES
# ===========================================================================

class TestWorkspaceRoutes:
    """
    Routes under test
      POST   /api/v1/workspaces/new           create workspace
      GET    /api/v1/workspaces/{id}          get workspace
      PATCH  /api/v1/workspaces/{id}          rename workspace
    """

    def test_create_get_duplicate_rename(self, client: TestClient):
        """A created workspace is readable with its files, can't be created twice, and renames persist."""
        r = client.post("/api/v1/workspaces/new", json={"id": "ws-crud", "name": "Old Name", "owners": ["alice"]})
        assert r.status_code == 200, r.text
        assert (r.json()["id"], r.json()["owners"]) == ("ws-crud", ["alice"])

        got = client.get("/api/v1/workspaces/ws-crud").json()
        assert (got["name"], got["files"]) == ("Old Name", [])
        assert client.post("/api/v1/workspaces/new", json={"id": "ws-crud", "name": "Again"}).status_code == 409

        assert client.patch("/api/v1/workspaces/ws-crud", json={"name": "New Name"}).status_code == 200
        assert client.get("/api/v1/workspaces/ws-crud").json()["name"] == "New Name"

    def test_unknown_workspace_returns_404(self, client: TestClient):
        assert client.get("/api/v1/workspaces/does-not-exist-xyz").status_code == 404
        assert client.patch("/api/v1/workspaces/does-not-exist-xyz", json={"name": "Ghost"}).status_code == 404


class TestWorkspaceDelete:
    """
    Route under test: DELETE /api/v1/workspaces/{workspace_id}
    LLM_delete_workspace is stubbed so no workspace on the real backend is touched.
    """

    @pytest.fixture(autouse=True)
    def backend_deletes(self, monkeypatch):
        deleted = []

        def fake_delete(workspace_id):
            deleted.append(workspace_id)
            return SimpleNamespace(status_code=200)

        monkeypatch.setattr(main, "LLM_delete_workspace", fake_delete)
        return deleted

    def test_delete_workspace_success(self, client: TestClient, backend_deletes):
        """DELETE removes the workspace from the backend and the uploader; GET then returns 404."""
        slug = "ws-delete-test-tmp"
        assert client.post("/api/v1/workspaces/new", json={"id": slug, "name": "Temp WS"}).status_code == 200

        r = client.delete(f"/api/v1/workspaces/{slug}")
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted": slug}
        assert backend_deletes == [slug]
        assert client.get(f"/api/v1/workspaces/{slug}").status_code == 404

    def test_delete_workspace_unknown_returns_404(self, client: TestClient, backend_deletes):
        """DELETE on a workspace the uploader doesn't know returns 404 without calling the backend."""
        assert client.delete("/api/v1/workspaces/no-such-workspace-del-xyz").status_code == 404
        assert backend_deletes == []

    def test_delete_workspace_backend_failure_returns_500(self, client: TestClient, monkeypatch):
        """If the backend refuses the delete, the uploader keeps the workspace and returns 500."""
        slug = "ws-del-fail-test"
        client.post("/api/v1/workspaces/new", json={"id": slug, "name": "Del Fail Test"})
        monkeypatch.setattr(main, "LLM_delete_workspace", lambda ws: SimpleNamespace(status_code=500))
        assert client.delete(f"/api/v1/workspaces/{slug}").status_code == 500
        assert client.get(f"/api/v1/workspaces/{slug}").status_code == 200


# ===========================================================================
# SCRAPE JOBS CRUD
# ===========================================================================

class TestScrapeJobCRUD:
    """
    Routes under test
      POST   /{workspace_id}/scrape/jobs                        create job
      GET    /{workspace_id}/scrape/jobs                        list jobs
      GET    /{workspace_id}/scrape/jobs/{job_id}/pages         list job pages
      PATCH  /{workspace_id}/scrape/jobs/{job_id}              update job
      DELETE /{workspace_id}/scrape/jobs/{job_id}              delete job
    """

    def test_job_lifecycle(self, client: TestClient):
        """Create (scheduled) → list → patch (unschedule, rename) → delete → 404."""
        job = _create_job(client, schedule_interval="daily", max_pages=5)
        assert (job["workspace_id"], job["mode"], job["max_pages"], job["is_running"], job["page_count"]) == (
            TEST_SLUG, "single", 5, False, 0)
        assert job["next_scrape_at"] is not None

        [listed] = client.get(f"/{TEST_SLUG}/scrape/jobs").json()
        assert listed["id"] == job["id"]
        assert client.get(f"/{TEST_SLUG}/scrape/jobs/{job['id']}/pages").json() == []

        r = client.patch(f"/{TEST_SLUG}/scrape/jobs/{job['id']}", json={"name": "Renamed", "schedule_interval": None})
        assert r.status_code == 200, r.text
        assert (r.json()["name"], r.json()["schedule_interval"], r.json()["next_scrape_at"]) == ("Renamed", None, None)
        assert store.get_job(TEST_SLUG, job["id"])["name"] == "Renamed"

        assert client.delete(f"/{TEST_SLUG}/scrape/jobs/{job['id']}").json() == {"deleted": job["id"]}
        assert store.get_job(TEST_SLUG, job["id"]) is None
        assert client.patch(f"/{TEST_SLUG}/scrape/jobs/{job['id']}", json={"name": "x"}).status_code == 404
        assert client.get("/no-such-workspace-xyz/scrape/jobs").status_code == 404

    def test_delete_job_cascades_to_pages(self, client: TestClient, monkeypatch):
        """Deleting a job removes its pages from the backend and their file records, nothing else."""
        removed = []
        monkeypatch.setattr(main, "LLM_remove_document", lambda ws, doc_id: removed.append(doc_id) or True)
        job = _create_job(client)
        store.add_files(TEST_SLUG, [
            main._new_file_record(TEST_SLUG, id="job-page", filename="page.docling.json",
                                  category="scrape_Test Job", source_url="https://example.com/page",
                                  scrape_job_id=job["id"]),
            main._new_file_record(TEST_SLUG, id="unrelated-upload", filename="keep.txt"),
        ])
        pages = client.get(f"/{TEST_SLUG}/scrape/jobs/{job['id']}/pages").json()
        assert [p["id"] for p in pages] == ["job-page"]

        assert client.delete(f"/{TEST_SLUG}/scrape/jobs/{job['id']}").status_code == 200
        assert removed == ["job-page"]
        assert [f["id"] for f in store.list_files(TEST_SLUG)] == ["unrelated-upload"]

    def test_list_job_urls_parsed_and_updated(self, client: TestClient):
        """mode="list" jobs store parsed, deduped URLs; PATCH replaces them; renaming keeps them."""
        job = _create_job(client, name="List Job", mode="list",
                          urls="https://example.com/\nexample.org\nhttps://example.com/")
        assert job["urls"] == ["https://example.com/", "https://example.org/"]
        assert (job["base_url"], job["max_pages"]) == ("https://example.com/", 2)

        r = client.patch(f"/{TEST_SLUG}/scrape/jobs/{job['id']}", json={"urls": ["https://example.net/"]})
        assert (r.json()["urls"], r.json()["base_url"], r.json()["max_pages"]) == (
            ["https://example.net/"], "https://example.net/", 1)
        r = client.patch(f"/{TEST_SLUG}/scrape/jobs/{job['id']}", json={"name": "Renamed"})
        assert r.json()["urls"] == ["https://example.net/"]

    @pytest.mark.parametrize("urls", [
        ["https://example.com/", "not a url"],
        "   ",
        [f"https://example.com/p{i}" for i in range(main.MAX_LIST_URLS + 1)],
    ], ids=["invalid-entry", "empty", "too-many"])
    def test_list_job_rejects_bad_urls(self, client: TestClient, urls):
        """Creating or updating a list job with unusable URLs returns 400."""
        r = client.post(f"/{TEST_SLUG}/scrape/jobs", json={"name": "Bad", "mode": "list", "urls": urls})
        assert r.status_code == 400
        job = _create_job(client, mode="list", urls=["https://example.com/"])
        assert client.patch(f"/{TEST_SLUG}/scrape/jobs/{job['id']}", json={"urls": urls}).status_code == 400


# ===========================================================================
# HELPER / UTILITY FUNCTIONS
# ===========================================================================

class TestParseUrlList:
    """Unit tests for the _parse_url_list helper in main.py."""

    @pytest.mark.parametrize("raw, expected", [
        ("https://a.com/x\nhttps://b.com/y\n", ["https://a.com/x", "https://b.com/y"]),
        ("https://a.com/, https://b.com/\thttps://c.com/ https://d.com/",
         ["https://a.com/", "https://b.com/", "https://c.com/", "https://d.com/"]),
        ("https://a.com/1,https://b.com/2,c.org", ["https://a.com/1", "https://b.com/2", "https://c.org/"]),
        ('1. https://a.com/\n- <https://b.com/>\n* "https://c.com/",\n• (https://d.com/).',
         ["https://a.com/", "https://b.com/", "https://c.com/", "https://d.com/"]),
        ("https://en.wikipedia.org/wiki/Python_(programming_language)\nhttps://a.com/p?id=1\nhttps://a.com/p?id=2",
         ["https://en.wikipedia.org/wiki/Python_(programming_language)", "https://a.com/p?id=1", "https://a.com/p?id=2"]),
        ("Docs page: example.com/docs", ["https://example.com/docs"]),
    ], ids=["lines", "commas-spaces-tabs", "commas-no-spaces", "markers-wrappers", "parens-queries", "words-no-scheme"])
    def test_valid_urls_extracted(self, raw, expected):
        urls, invalid, dupes = main._parse_url_list(raw)
        assert (urls, invalid, dupes) == (expected, [], 0)

    def test_duplicates_removed_preserving_order(self):
        urls, _, dupes = main._parse_url_list(["https://b.com/", "https://a.com/", "HTTPS://B.COM/#section", "b.com", ""])
        assert urls == ["https://b.com/", "https://a.com/"]
        assert dupes == 2

    def test_invalid_entries_reported(self):
        urls, invalid, _ = main._parse_url_list(
            "https://ok.com/\nnot a url\nftp://files.example.com/x\njavascript:alert(1)"
        )
        assert urls == ["https://ok.com/"]
        assert invalid == ["not a url", "ftp://files.example.com/x", "javascript:alert(1)"]

    @pytest.mark.parametrize("raw", ["", None, "  \n\n "])
    def test_empty_input(self, raw):
        assert main._parse_url_list(raw) == ([], [], 0)


class TestSanitizeUrlToFilename:
    """Unit tests for the _sanitize_url_to_filename helper in main.py."""

    @pytest.mark.parametrize("url, expected", [
        ("https://example.com/foo/bar/", "example_com_foo_bar"),
        ("https://example.com/", "example_com_index"),
        ("https://example.com/a//b?q=1&x=2", "example_com_a_b"),
        ("https://its.uri.edu/services/zoom/getting-started", "its_uri_edu_services_zoom_getting-started"),
    ])
    def test_filename(self, url, expected):
        assert main._sanitize_url_to_filename(url) == expected


class TestComputeNextRun:
    """Unit tests for the _compute_next_run scheduler helper in main.py."""

    BASE = datetime(2025, 1, 1, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    @pytest.mark.parametrize("interval, delta", [
        ("hourly", timedelta(hours=1)),
        ("daily", timedelta(days=1)),
        ("weekly", timedelta(weeks=1)),
        (None, None),
        ("monthly", None),
    ])
    def test_next_run(self, interval, delta):
        expected = self.BASE + delta if delta else None
        assert main._compute_next_run(interval, from_time=self.BASE) == expected
