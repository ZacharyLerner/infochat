"""
conversion.py
=============
Document conversion through docling-pipeline (a Lambda), so uploader servers
don't run docling themselves:

1. The uploader saves the original at workspaces/<slug>/uploader/originals/<key>/...
   and sends a job to DOCLING_QUEUE_URL (send_job). Originals are temporary: only
   the converted document is kept.
2. The Lambda writes DoclingDocument JSON to .../converted/<key>/<name>.docling.json
   and posts the result to DOCLING_RESULTS_QUEUE_URL. Errors are posted too, on
   the job's last attempt.
3. Every uploader server polls the results queue (consume_results); SQS hands
   each message to one of them. handle_result embeds the converted document in
   the RAG backend and updates the file record (status converting -> embedding
   -> ready, or failed). A message is deleted only once that worked, so a failed embed is
   retried from the results queue without converting again.

The job's "context" carries {workspace_id, key} to find the record again.
"""

import asyncio
import io
import json

import boto3

import anythingllm
import store
from config import AWS_REGION, DOCLING_QUEUE_URL, DOCLING_RESULTS_QUEUE_URL, S3_STATE_BUCKET

_client = None


def _sqs():
    global _client
    if _client is None:
        _client = boto3.client("sqs", region_name=AWS_REGION or None)
    return _client


def send_job(record: dict, llm_name: str, *, scraped: bool = False, content_type: str = "") -> str:
    """Queue conversion of a record's original; returns the key the output will be written to."""
    if not DOCLING_QUEUE_URL:
        raise RuntimeError("DOCLING_QUEUE_URL is not set, so documents can't be converted")
    workspace_id = record["workspace_id"]
    output_key = store.converted_key(workspace_id, record["key"], llm_name)
    _sqs().send_message(QueueUrl=DOCLING_QUEUE_URL, MessageBody=json.dumps({
        "bucket": S3_STATE_BUCKET,
        "key": record["original"],
        "output_bucket": S3_STATE_BUCKET,
        "output_key": output_key,
        "source_url": record.get("source_url"),
        "scraped": scraped,
        "content_type": content_type,
        "context": {"workspace_id": workspace_id, "key": record["key"]},
    }))
    return output_key


def _converted_original(result: dict, workspace_id: str, key: str) -> str | None:
    """The original this result was converted from, if it is one of this record's
    (never act on a key outside its originals/ folder)."""
    source = result.get("source") or ""
    s3_key = source.split("/", 3)[3] if source.startswith("s3://") and source.count("/") >= 3 else ""
    return s3_key if s3_key.startswith(store.original_key(workspace_id, key, "")) else None


def handle_result(result: dict) -> None:
    """Apply one pipeline result to its file record. Raises to have the message retried.

    Originals are only kept until they are converted: once a result is applied
    (success, or the job's final failure) its original is deleted.
    """
    context = result.get("context") or {}
    workspace_id, key = context.get("workspace_id"), context.get("key")
    if not workspace_id or not key:
        return  # not a job this uploader sent
    original = _converted_original(result, workspace_id, key)

    def forget_original(f: dict) -> dict:
        return {**f, "original": None} if f.get("original") == original else f

    record = next((f for f in store.list_files(workspace_id) if f["key"] == key), None)
    if record is None:
        # Deleted while converting: drop the orphaned files.
        store.delete_objects([original, result.get("output_key")])
        return

    if result.get("status") != "success":
        error = result.get("error") or "conversion failed"
        print(f"[conversion] {workspace_id}/{record['filename']}: {error}")
        # A document that is already embedded keeps its current version.
        store.update_file(workspace_id, key, lambda f: forget_original({
            **f, "error": error, **({} if f.get("id") else {"status": "failed"}),
        }))
        store.delete_objects([original])
        return

    if record.get("id") and record.get("content_hash") == result["content_hash"]:
        # Unchanged page (or a repeat delivery): nothing to embed.
        store.update_file(workspace_id, key, lambda f: forget_original({
            **f, "status": "ready", "error": None, "converted": result["output_key"],
            "last_checked_at": store.now(),
        }))
        store.delete_objects([original])
        return

    store.update_file(workspace_id, key, lambda f: {**f, "status": "embedding"})  # shown as a progress step
    body = store.get_object(result["output_key"]).decode("utf-8")
    name = result["output_key"].rsplit("/", 1)[-1]
    doc_id = anythingllm.LLM_upload_document(io.StringIO(body), name, workspace_id)
    old_id = record.get("id")
    updated = store.update_file(workspace_id, key, lambda f: forget_original({
        **f, "id": doc_id, "status": "ready", "error": None, "content_hash": result["content_hash"],
        "converted": result["output_key"], "last_checked_at": store.now(),
    }))
    store.delete_objects([original])
    if updated is None:  # deleted while embedding
        anythingllm.LLM_remove_document(workspace_id, doc_id)
    elif old_id and old_id != doc_id:
        anythingllm.LLM_remove_document(workspace_id, old_id)


async def consume_results() -> None:
    """Poll the results queue forever (long polling: one request per 20 s when idle)."""
    print(f"[conversion] consuming results from {DOCLING_RESULTS_QUEUE_URL}")
    while True:
        try:
            resp = await asyncio.to_thread(
                _sqs().receive_message, QueueUrl=DOCLING_RESULTS_QUEUE_URL,
                MaxNumberOfMessages=10, WaitTimeSeconds=20,
            )
        except Exception as e:
            print(f"[conversion] receive failed: {e}")
            await asyncio.sleep(5)
            continue
        for message in resp.get("Messages", []):
            try:
                await asyncio.to_thread(handle_result, json.loads(message["Body"]))
                await asyncio.to_thread(
                    _sqs().delete_message, QueueUrl=DOCLING_RESULTS_QUEUE_URL,
                    ReceiptHandle=message["ReceiptHandle"],
                )
            except Exception as e:
                print(f"[conversion] result not applied, will retry: {e}")
