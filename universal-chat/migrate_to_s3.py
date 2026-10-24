"""
migrate_to_s3.py
================
One-time copy of the chat server's old SQLite workspace settings into S3
(workspaces/<slug>/chat.json, see store.py). Run it once on each server that
held data, from the universal-chat/ folder, with the same AWS credentials and
S3_STATE_BUCKET as the RAG backend.

    python migrate_to_s3.py            # dry run: print what would be copied
    python migrate_to_s3.py --apply    # copy

Only workspaces that exist in the RAG backend (workspaces/<slug>/settings.json in
the bucket) are copied; the rest are reported and dropped. Workspaces already
in S3 are left alone, so re-running is safe.
"""

import argparse
import json
import sqlite3
import sys

import store


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="universal_chat.db")
    parser.add_argument("--apply", action="store_true", help="write to S3 (default is a dry run)")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    dropped = []

    for row in db.execute("SELECT * FROM workspaces"):
        slug = row["slug"]
        if store._get_json(f"workspaces/{slug}/settings.json")[0] is None:
            dropped.append(slug)
            continue
        ws = {
            "slug": slug,
            "name": row["name"],
            "welcome_text": row["welcome_text"],
            "followup_enabled": bool(row["followup_enabled"]),
            "followup_count": row["followup_count"],
            "email_enabled": bool(row["email_enabled"]),
            "default_questions": json.loads(row["default_questions"] or "[]"),
        }
        if not args.apply:
            print(f"would copy {slug}" + (" (already in S3, skipped)" if store.get_workspace(slug) else ""))
        elif store.create_workspace(ws):
            print(f"copied {slug}")
        else:
            print(f"skip   {slug}: already in S3")

    if dropped:
        print(f"dropped {len(dropped)} workspace(s) not in the RAG backend: {', '.join(dropped)}")
    if not args.apply:
        print("dry run: nothing written (pass --apply to copy)")


if __name__ == "__main__":
    sys.exit(main())
