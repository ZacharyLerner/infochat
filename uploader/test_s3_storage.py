"""
Offline tests for the S3-backed uploader state (store.py and the routes using it).

The RAG backend, page fetching and docling-pipeline are stubbed out (the
`pipeline` fixture plays the Lambda: it writes a converted document and hands
its result to conversion.handle_result), and S3 is the in-memory FakeS3 from
conftest.py, so these run without any network.
"""

import asyncio
import hashlib
import io
import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import anythingllm
import conversion
import main
import store

WS = "ws-test"
OTHER = "ws-other"


@pytest.fixture()
def backend(monkeypatch):
    """Fake RAG backend: tracks embedded documents by doc_id."""
    docs = {}

    def upload(file, name, workspace):
        doc_id = str(uuid.uuid4())
        docs[doc_id] = {"workspace": workspace, "name": name, "text": file.read()}
        return doc_id

    def remove(workspace, doc_id):
        docs.pop(doc_id, None)
        return True

    for module in (main, anythingllm):  # conversion.py calls anythingllm.* directly
        monkeypatch.setattr(module, "LLM_upload_document", upload)
        monkeypatch.setattr(module, "LLM_remove_document", remove)
    return docs


@pytest.fixture()
def pages(monkeypatch):
    """Fake web: url -> HTML body."""
    web = {}

    def fetch(url):
        if url not in web:
            raise ValueError(f"Request failed: HTTP 404 from {url}")
        return web[url].encode(), "text/html"

    monkeypatch.setattr(main, "fetch_page", fetch)
    return web


class FakePipeline:
    """Stands in for docling-pipeline: collects queued jobs; run() converts them the way
    the Lambda would (output written to S3, result applied by conversion.handle_result)."""

    def __init__(self):
        self.jobs = []

    def send_job(self, record, llm_name, *, scraped=False, content_type=""):
        output_key = store.converted_key(record["workspace_id"], record["key"], llm_name)
        self.jobs.append({"key": record["original"], "output_key": output_key, "scraped": scraped,
                          "source_url": record.get("source_url"),
                          "context": {"workspace_id": record["workspace_id"], "key": record["key"]}})
        return output_key

    def run(self, fail=None):
        """Convert every queued job; `fail` maps an original key's substring to an error."""
        jobs, self.jobs = self.jobs, []
        for job in jobs:
            error = next((e for k, e in (fail or {}).items() if k in job["key"]), None)
            source = f"s3://{store.S3_STATE_BUCKET}/{job['key']}"
            if error:
                conversion.handle_result({"status": "error", "error": error, "source": source, "context": job["context"]})
                continue
            if job["key"] not in store._s3().objects:  # original deleted: the Lambda's last attempt reports it
                conversion.handle_result({"status": "error", "error": "NoSuchKey", "source": source, "context": job["context"]})
                continue
            out = json.dumps({"origin": job["source_url"], "text": store.get_object(job["key"]).decode()}).encode()
            store.put_object(job["output_key"], out)
            conversion.handle_result({"status": "success", "source": source, "output_key": job["output_key"],
                                      "content_hash": hashlib.sha256(out).hexdigest(), "context": job["context"]})


@pytest.fixture()
def pipeline(monkeypatch):
    fake = FakePipeline()
    monkeypatch.setattr(conversion, "send_job", fake.send_job)
    return fake


@pytest.fixture()
def client(fake_s3, backend, monkeypatch):
    monkeypatch.setattr(main, "APP_API_KEY", "")
    store.create_workspace(WS, "Test", [])
    with TestClient(main.app) as c:
        yield c


def _upload(client, name="notes.txt", text="hello"):
    r = client.post(f"/api/v1/workspaces/{WS}/upload",
                    files=[("uploaded_files", (name, io.BytesIO(text.encode()), "text/plain"))])
    assert r.status_code == 200, r.text
    return r.json()[0]


def _run(client, job_id):
    r = client.post(f"/{WS}/scrape/jobs/{job_id}/run")
    assert r.status_code == 200, r.text
    return [json.loads(line[6:]) for line in r.text.splitlines()
            if line.startswith("data: {")]


# --- uploads and deletes -----------------------------------------------------

def _originals(fake_s3):
    return [k for k in fake_s3.objects if "/uploader/originals/" in k]


def test_text_upload_is_stored_as_converted_document(client, fake_s3, backend):
    rec = _upload(client, "notes.txt", "hello")
    assert rec["id"] in backend
    [stored] = store.list_files(WS)
    assert stored["id"] == rec["id"] and stored["original_extension"] == ".txt"
    assert store.get_object(stored["converted"]) == b"hello"
    assert stored["original"] is None and _originals(fake_s3) == []


def test_delete_removes_record_and_document(client, fake_s3, backend):
    rec = _upload(client)
    converted = store.list_files(WS)[0]["converted"]
    r = client.delete(f"/{WS}/delete/{rec['id']}")
    assert r.status_code == 200
    assert store.list_files(WS) == []
    assert converted not in fake_s3.objects
    assert rec["id"] not in backend


def test_bulk_delete_only_touches_this_workspace(client, backend):
    a, b = _upload(client, "a.txt"), _upload(client, "b.txt")
    r = client.post(f"/{WS}/delete-bulk", json={"file_ids": [a["id"], "not-a-file"]})
    assert r.json() == {"deleted": [a["id"]]}
    assert [f["id"] for f in store.list_files(WS)] == [b["id"]]


def test_failed_embed_leaves_nothing_stored(client, fake_s3, monkeypatch):
    def boom(*a):
        raise RuntimeError("backend down")
    monkeypatch.setattr(main, "LLM_upload_document", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/api/v1/workspaces/{WS}/upload",
                    files=[("uploaded_files", ("x.txt", io.BytesIO(b"x"), "text/plain"))])
    assert not [k for k in fake_s3.objects if "/uploader/originals/" in k or "/uploader/converted/" in k]


def test_home_renders_records_from_s3(client):
    _upload(client, "report.txt")
    r = client.get(f"/{WS}")
    assert r.status_code == 200 and "report.txt" in r.text


def test_converting_pdf_row_warns_it_can_take_minutes(client, pipeline):
    _upload(client, "scan.pdf", "%PDF-fake")
    _upload(client, "notes.docx", "PK-fake")
    html = client.get(f"/{WS}").text
    assert html.count("PDFs and scanned documents can take a few minutes.") == 1
    assert html.count("Converting…") == 2
    assert html.count('class="progress-bar-fill" style="width: 50%"') == 2  # step-by-step bar kept


# --- job claiming ---------------------------------------------------------------

def _job(client, **kw):
    body = {"name": "docs", "mode": "list", "urls": ["https://a.example/one"], **kw}
    r = client.post(f"/{WS}/scrape/jobs", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_claim_is_exclusive_until_released(client):
    job = _job(client)
    assert store.claim_job(WS, job["id"])["is_running"] is True
    with pytest.raises(store.JobBusy):
        store.claim_job(WS, job["id"])
    assert client.post(f"/{WS}/scrape/jobs/{job['id']}/run").status_code == 409

    store.release_job(WS, job["id"], main._next_run_iso)
    assert store.claim_job(WS, job["id"])


def test_stale_claim_expires(client):
    job = _job(client)
    old = (datetime.fromisoformat(store.now()) - store.RUN_LEASE - timedelta(minutes=1)).isoformat()
    store.update_job(WS, job["id"], lambda j: {**j, "is_running": True, "run_started_at": old})
    assert store.claim_job(WS, job["id"])


# --- conversion through docling-pipeline --------------------------------------------

def test_non_text_upload_converts_through_pipeline(client, pipeline, backend, fake_s3):
    rec = _upload(client, "report.pdf", "%PDF-fake")
    assert rec["status"] == "converting" and rec["id"] is None
    [job] = pipeline.jobs
    assert job["key"].endswith(f"/uploader/originals/{rec['key']}/report.pdf")
    assert job["output_key"].endswith(f"/uploader/converted/{rec['key']}/report.docling.json")
    assert backend == {} and _originals(fake_s3) == [job["key"]]

    pipeline.run()
    [stored] = store.list_files(WS)
    assert stored["status"] == "ready" and stored["id"] in backend
    assert backend[stored["id"]]["name"] == "report.docling.json"
    assert stored["converted"] == job["output_key"]
    assert stored["original"] is None and _originals(fake_s3) == []  # only the conversion is kept


def test_failed_conversion_marks_record_failed(client, pipeline, backend, fake_s3):
    _upload(client, "broken.pdf", "%PDF-bad")
    pipeline.run(fail={"broken.pdf": "Document converted to empty content"})
    [stored] = store.list_files(WS)
    assert stored["status"] == "failed" and stored["error"] == "Document converted to empty content"
    assert backend == {} and _originals(fake_s3) == []


def test_delete_while_converting_drops_late_result(client, pipeline, backend, fake_s3):
    rec = _upload(client, "report.pdf", "%PDF-fake")
    assert client.delete(f"/{WS}/delete/{rec['key']}").status_code == 200
    pipeline.run()
    assert store.list_files(WS) == [] and backend == {}
    assert not [k for k in fake_s3.objects if f"/{rec['key']}/" in k]


def test_success_for_deleted_record_removes_output(client, backend, fake_s3):
    output_key = store.converted_key(WS, "gone", "report.docling.json")
    store.put_object(output_key, b"{}")
    conversion.handle_result({"status": "success", "output_key": output_key, "content_hash": "x",
                              "context": {"workspace_id": WS, "key": "gone"}})
    assert output_key not in fake_s3.objects and backend == {}


def test_repeated_result_does_not_embed_twice(client, pipeline, backend):
    _upload(client, "report.pdf", "%PDF-fake")
    job = pipeline.jobs[0]
    pipeline.run()
    pipeline.jobs.append(job)  # SQS can deliver a message more than once
    pipeline.run()
    assert len(backend) == 1


# --- job runs ---------------------------------------------------------------------

def test_run_tracks_new_changed_unchanged_and_removed_pages(client, pages, pipeline, backend, fake_s3):
    pages["https://a.example/one"] = "<p>one v1</p>"
    pages["https://a.example/two"] = "<p>two</p>"
    job = _job(client, urls=["https://a.example/one", "https://a.example/two"])

    events = _run(client, job["id"])
    assert [e["status"] for e in events if "url" in e and e["status"] != "fetching"] == ["queued", "queued"]
    assert all(j["scraped"] for j in pipeline.jobs)
    files = {f["source_url"]: f for f in store.list_files(WS)}
    assert store.get_object(files["https://a.example/one"]["original"]) == b"<p>one v1</p>"
    pipeline.run()
    files = {f["source_url"]: f for f in store.list_files(WS)}
    assert {f["status"] for f in files.values()} == {"ready"}
    assert _originals(fake_s3) == []
    first_one, first_two = files["https://a.example/one"]["id"], files["https://a.example/two"]["id"]

    # page one changes, page two stays the same
    pages["https://a.example/one"] = "<p>one v2</p>"
    _run(client, job["id"])
    pipeline.run()
    files = {f["source_url"]: f for f in store.list_files(WS)}
    assert files["https://a.example/one"]["id"] != first_one and first_one not in backend
    assert files["https://a.example/two"]["id"] == first_two  # unchanged: not embedded again
    assert len(backend) == 2 and len(files) == 2
    assert _originals(fake_s3) == []

    # page two is dropped from the job
    client.patch(f"/{WS}/scrape/jobs/{job['id']}", json={"urls": ["https://a.example/one"]})
    events = _run(client, job["id"])
    pipeline.run()
    assert [e["url"] for e in events if e.get("status") == "removed"] == ["https://a.example/two"]
    assert [f["source_url"] for f in store.list_files(WS)] == ["https://a.example/one"]
    assert first_two not in backend
    assert not any("a_example_two" in k for k in fake_s3.objects)

    finished = store.get_job(WS, job["id"])
    assert finished["is_running"] is False and finished["last_scraped_at"]
    assert events[-1]["status"] == "done" and events[-1]["page_count"] == 1


def test_overlapping_runs_keep_each_others_originals(client, pages, pipeline, backend, fake_s3):
    pages["https://a.example/one"] = "<p>v1</p>"
    job = _job(client)
    _run(client, job["id"])
    pages["https://a.example/one"] = "<p>v2</p>"
    _run(client, job["id"])  # second run queued before the first one's result arrives
    pipeline.run()
    [page] = store.list_files(WS)
    assert page["status"] == "ready" and page["error"] is None
    assert "v2" in store.get_object(page["converted"]).decode() and _originals(fake_s3) == []


def test_result_never_deletes_outside_the_records_originals(client, pipeline, fake_s3):
    rec = _upload(client, "report.pdf", "%PDF-fake")
    other = store.converted_key(WS, "someone-else", "keep.docling.json")
    store.put_object(other, b"{}")
    conversion.handle_result({"status": "error", "error": "x", "source": f"s3://b/{other}",
                              "context": {"workspace_id": WS, "key": rec["key"]}})
    assert other in fake_s3.objects


def test_run_due_starts_each_due_job_once(client, pages, pipeline, monkeypatch):
    pages["https://a.example/one"] = "<p>one</p>"
    due = _job(client, schedule_interval="hourly")
    later = _job(client, schedule_interval="daily")
    past = (datetime.fromisoformat(store.now()) - timedelta(minutes=1)).isoformat()
    store.update_job(WS, due["id"], lambda j: {**j, "next_scrape_at": past})

    started_runs = []
    monkeypatch.setattr(main, "_run_job_in_background", lambda job: asyncio.sleep(0, started_runs.append(job["id"])))

    assert client.post("/api/v1/scrape/run-due").json() == {"started": [due["id"]]}
    # still claimed (the fake run never released it), so a second tick starts nothing
    assert client.post("/api/v1/scrape/run-due").json() == {"started": []}
    assert started_runs == [due["id"]]
    assert store.get_job(WS, later["id"])["is_running"] is False


def test_run_due_requires_api_key(client, monkeypatch):
    monkeypatch.setattr(main, "APP_API_KEY", "secret")
    assert client.post("/api/v1/scrape/run-due").status_code == 401
    assert client.post("/api/v1/scrape/run-due", headers={"X-API-Key": "secret"}).status_code == 200


# --- reembed / copy / workspace delete ---------------------------------------------

def test_reembed_uses_stored_conversions(client, pages, pipeline, backend):
    pages["https://a.example/one"] = "<p>page</p>"
    _run(client, _job(client)["id"])
    pipeline.run()
    _upload(client, "notes.txt", "uploaded text")
    _upload(client, "pending.pdf", "%PDF-fake")  # still converting
    store.add_files(WS, [main._new_file_record(WS, id="legacy", filename="old.pdf")])  # nothing stored
    backend.clear()  # backend wiped

    r = client.post(f"/api/v1/workspaces/{WS}/reembed")
    assert r.json()["reembedded"] == 2
    assert {f["file"]: f["error"] for f in r.json()["failed"]} == {
        "pending.pdf": "still converting", "old.pdf": "no converted document stored for this file"}
    texts = {d["name"]: d["text"] for d in backend.values()}
    assert '"origin": "https://a.example/one"' in texts["a_example_one.docling.json"]
    assert texts["notes.txt"] == "uploaded text"
    assert len(pipeline.jobs) == 1  # only pending.pdf's own upload job; reembed converts nothing


def test_migrated_conversion_is_embedded_as_is(client, backend):
    rec = main._new_file_record(WS, id="old", filename="report.pdf", original_extension=".pdf")
    rec["converted"] = store.converted_key(WS, rec["key"], "report.docling.json")
    store.put_object(rec["converted"], b'{"schema_name": "DoclingDocument"}')
    store.add_files(WS, [rec])

    assert client.post(f"/api/v1/workspaces/{WS}/reembed").json()["reembedded"] == 1
    [doc] = backend.values()
    assert doc["name"] == "report.docling.json"


def test_copy_embeds_into_destination(client, pages, pipeline, backend):
    store.create_workspace(OTHER, "Other", [])
    pages["https://a.example/one"] = "<p>page</p>"
    _run(client, _job(client)["id"])
    pipeline.run()
    _upload(client, "notes.txt", "text")

    r = client.post(f"/api/v1/workspaces/{OTHER}/copy", json={"source_workspace": WS})
    assert r.json() == {"copied": 2, "failed": []}
    copies = store.list_files(OTHER)
    assert {f["category"] for f in copies} == {"copied", "uploaded_file"}
    assert all(f["workspace_id"] == OTHER and f["converted"].startswith(f"workspaces/{OTHER}/") for f in copies)
    assert sum(d["workspace"] == OTHER for d in backend.values()) == 2
    assert pipeline.jobs == []


def test_delete_workspace_removes_all_uploader_objects(client, fake_s3, monkeypatch):
    _upload(client)
    _job(client)
    monkeypatch.setattr(main, "LLM_delete_workspace", lambda ws: type("R", (), {"status_code": 200})())
    assert client.delete(f"/api/v1/workspaces/{WS}").status_code == 200
    assert not [k for k in fake_s3.objects if k.startswith(f"workspaces/{WS}/uploader/")]
