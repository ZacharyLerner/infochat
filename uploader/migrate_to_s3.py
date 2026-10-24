"""
migrate_to_s3.py
================
One-time copy of the uploader's old SQLite database (and debug_uploads folder)
into the S3 layout used by store.py. Run it once on each server that held data,
from the uploader/ folder, with the same AWS credentials and S3_STATE_BUCKET
as the RAG backend.

    python migrate_to_s3.py                     # dry run: print what would be copied
    python migrate_to_s3.py --apply             # copy

Only workspaces that exist in the RAG backend (workspaces/<slug>/settings.json in
the bucket) are copied; the rest are reported and dropped. A workspace the
uploader already has in S3 is skipped, so re-running is safe.

What debug_uploads/<slug>/ kept for each file (a .docling.json, a pre-Docling .md,
or the text file itself) becomes the file's converted document, which is all the
uploader keeps and what /reembed rebuilds embeddings from. Files without one are
copied without a document and are reported by /reembed until uploaded again.
"""

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import store
from config import TEXT_EXTENSIONS
from decling_conversion import docling_filename

NY = ZoneInfo("America/New_York")


def _iso(value):
    """SQLite stored naive New York times as text; return ISO-8601 with the offset."""
    if not value:
        return None
    dt = datetime.fromisoformat(str(value))
    return (dt if dt.tzinfo else dt.replace(tzinfo=NY)).isoformat()


def _debug_copy(debug_dir: Path, slug: str, filename: str) -> Path | None:
    """The converted copy debug_uploads kept for a file, if any (same rules the old code used)."""
    if Path(filename).suffix.lower() in TEXT_EXTENSIONS or filename.endswith(".docling.json"):
        names = [filename]
    else:
        names = [docling_filename(filename), Path(filename).with_suffix(".md").name]
    return next((debug_dir / slug / n for n in names if (debug_dir / slug / n).is_file()), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="uploader_files.db")
    parser.add_argument("--debug-dir", default="debug_uploads")
    parser.add_argument("--apply", action="store_true", help="write to S3 (default is a dry run)")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    debug_dir = Path(args.debug_dir)
    dropped = []

    for ws in db.execute("SELECT * FROM workspaces"):
        slug = ws["id"]
        if store._get_json(f"workspaces/{slug}/settings.json")[0] is None:
            dropped.append(slug)
            continue
        if store.get_workspace(slug):
            print(f"skip   {slug}: already in S3")
            continue

        jobs = [
            {
                "id": j["id"],
                "workspace_id": slug,
                "name": j["name"],
                "base_url": j["base_url"],
                "mode": j["mode"] or "depth",
                "urls": json.loads(j["urls"]) if j["urls"] else None,
                "max_depth": j["max_depth"],
                "max_pages": j["max_pages"],
                "allow_offsite": bool(j["allow_offsite"]),
                "schedule_interval": j["schedule_interval"],
                "last_scraped_at": _iso(j["last_scraped_at"]),
                "next_scrape_at": _iso(j["next_scrape_at"]),
                "is_running": False,
                "run_started_at": None,
                "created_at": _iso(j["created_at"]) or store.now(),
            }
            for j in db.execute("SELECT * FROM scrape_jobs WHERE workspace_id = ?", (slug,))
        ]

        files, objects = [], []
        for f in db.execute("SELECT * FROM files WHERE workspace_id = ?", (slug,)):
            key = uuid.uuid4().hex
            copy = _debug_copy(debug_dir, slug, f["filename"])
            converted = store.converted_key(slug, key, copy.name) if copy else None
            if copy:
                objects.append((converted, copy))
            files.append({
                "key": key,
                "id": f["id"],
                "filename": f["filename"],
                "original_extension": f["original_extension"],
                "workspace_id": slug,
                "category": f["category"],
                "source_url": f["source_url"],
                "scrape_job_id": f["scrape_job_id"],
                "content_hash": f["content_hash"],
                "last_checked_at": _iso(f["last_checked_at"]),
                "uploaded_at": _iso(f["uploaded_at"]) or store.now(),
                "status": "ready",
                "error": None,
                "original": None,
                "converted": converted,
            })

        print(f"{'copy' if args.apply else 'would copy'} {slug}: {len(files)} files "
              f"({len(objects)} with a stored copy), {len(jobs)} scrape jobs")
        if not args.apply:
            continue
        for s3_key, path in objects:
            store.put_object(s3_key, path.read_bytes())
        store.add_files(slug, files)
        for job in jobs:
            store.add_job(slug, job)
        # Written last: its presence is what marks the workspace as migrated.
        store.create_workspace(slug, ws["name"], json.loads(ws["owners"]) if ws["owners"] else [])

    if dropped:
        print(f"dropped {len(dropped)} workspace(s) not in the RAG backend: {', '.join(dropped)}")
    if not args.apply:
        print("dry run: nothing written (pass --apply to copy)")


if __name__ == "__main__":
    sys.exit(main())
