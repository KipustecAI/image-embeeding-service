#!/usr/bin/env python
"""Step 2 — copy private embedding frames into the ``public/`` prefix (server-side).

WHY
    storage-service decides public vs private SOLELY by the upload ``folder``.
    The embeddings:results consumer uploaded every frame to a bare
    ``embeddings/{camera}/{evidence}/``, so every image was PRIVATE and the UI got
    403 on all of them. The consumer fix (``_PUBLIC_PREFIX``) heals NEW uploads;
    this script heals the ones already written.

    Ported from etl-service's scripts/backfill_public_assets.py, which healed
    34,923 objects with 0 failures. The full procedure, and the traps, are in
    etl-service docs/runbooks/public-asset-migration-runbook.md.

WHAT IT DOES
    Day by day (via embedding_requests.created_at, which is indexed —
    evidence_embeddings.created_at is NOT), for every evidence folder:
      1. list the destination ``public/<folder>/`` once
      2. server-side ``copy_object`` each frame not already there
         (no bytes move through this machine)
      3. list the destination again and confirm every frame is present
    It does NOT touch the database or Qdrant and does NOT delete the originals:
    the URL rewrite is the separate step 3, and keeping the originals makes
    rollback free. Run this to completion BEFORE step 3, or the UI turns a 403
    into a 404, which is worse.

⚠️ VERIFICATION LIES ON THIS GATEWAY — list_objects_v2 IS THE ONLY TRUTH.
    head_object 403s always; get_object throws SignatureDoesNotMatch under
    concurrency; an anonymous GET on a private path 403s whether the object
    exists or not; an anonymous GET right after a copy 404s while the gateway
    lags. Every existence check here is a list. Do not "optimise" it.

SCOPE
    --since defaults to 2026-09-01. Frames referenced before that are GONE from
    the bucket (sampled 2026-10-12: 0/24 present for May and August, 24/24 for
    September and October) — nothing can heal them.

SAFE BY DEFAULT
    Dry run unless --apply. Idempotent and resumable: completed folders are
    appended to --progress and skipped on a re-run; a frame already present at
    the destination is never copied twice.

CREDENTIALS
    Read from the environment first, then --env-file. This repo's .env has a
    STALE Neon password and no MinIO keys (runbook §3), so pass them explicitly:
        DATABASE_URL=postgresql://...image-embedding-db?sslmode=require \\
        MINIO_ACCESS_KEY=... MINIO_SECRET_KEY=... \\
        python scripts/backfill_public_assets.py

USAGE
    python scripts/backfill_public_assets.py                          # dry run, Sep 1 → now
    python scripts/backfill_public_assets.py --limit 20 --apply       # 20-folder rehearsal
    python scripts/backfill_public_assets.py --apply                  # the real run
    python scripts/backfill_public_assets.py --apply --since 2026-10-01 --until 2026-10-05
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import boto3
    import botocore
    import httpx
    import psycopg2
except ImportError as exc:  # pragma: no cover - operator-facing
    sys.exit(f"missing dependency: {exc}. pip install boto3 httpx psycopg2-binary")

ENDPOINT = "https://storage.lookia.mx"
HOST = "storage.lookia.mx"
PUBLIC_PREFIX = "public/"
DEFAULT_SINCE = "2026-09-01"


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    env.update(
        {
            k: v
            for k, v in os.environ.items()
            if k in ("DATABASE_URL", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")
        }
    )
    return env


def pg_dsn(raw: str) -> str:
    return raw.replace("postgresql+asyncpg://", "postgresql://").replace(
        "?ssl=require", "?sslmode=require"
    )


def fetch_day(cur, bucket: str, day: dt.date) -> dict[str, list[str]]:
    """{folder: [key, ...]} for one day's private frames on the live host."""
    cur.execute(
        """
        SELECT e.image_url
        FROM embedding_requests r
        JOIN evidence_embeddings e ON e.request_id = r.id
        WHERE r.created_at >= %s AND r.created_at < %s
          AND e.image_url LIKE %s
        """,
        (day, day + dt.timedelta(days=1), f"%{HOST}/{bucket}/embeddings/%"),
    )
    folders: dict[str, list[str]] = defaultdict(list)
    for (url,) in cur.fetchall():
        key = url.split(f"/{bucket}/", 1)[1]
        folders[key.rsplit("/", 1)[0]].append(key)
    return folders


_local = threading.local()


def s3_client(env: dict[str, str]):
    """One boto3 client per thread — they are not thread-safe."""
    if getattr(_local, "s3", None) is None:
        _local.s3 = boto3.client(
            "s3",
            endpoint_url=ENDPOINT,
            aws_access_key_id=env["MINIO_ACCESS_KEY"],
            aws_secret_access_key=env["MINIO_SECRET_KEY"],
            region_name="us-east-1",
            config=botocore.config.Config(
                signature_version="s3v4",
                connect_timeout=20,
                read_timeout=120,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _local.s3


def list_keys(s3, bucket: str, prefix: str) -> set[str]:
    """Every key under ``prefix`` — list_objects_v2, the only check that matches reality."""
    keys: set[str] = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.update(c["Key"] for c in page.get("Contents", []))
    return keys


def migrate_folder(
    env, bucket: str, folder: str, keys: list[str], apply: bool
) -> tuple[str, str, int, str]:
    """Returns (folder, outcome, frames_copied, detail).

    Outcome: done | skipped | would-copy | missing-source | failed.
    """
    s3 = s3_client(env)
    dest_prefix = f"{PUBLIC_PREFIX}{folder}/"
    try:
        present = list_keys(s3, bucket, dest_prefix)
        todo = [k for k in keys if PUBLIC_PREFIX + k not in present]
        if not todo:
            return folder, "skipped", 0, "already public"
        if not apply:
            return folder, "would-copy", len(todo), ""
        missing = 0
        for key in todo:
            try:
                s3.copy_object(
                    Bucket=bucket,
                    Key=PUBLIC_PREFIX + key,
                    CopySource={"Bucket": bucket, "Key": key},
                )
            except botocore.exceptions.ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    missing += 1
                    continue
                raise
        present = list_keys(s3, bucket, dest_prefix)
        absent = [k for k in keys if PUBLIC_PREFIX + k not in present]
        if missing and len(absent) == missing:
            return (
                folder,
                "missing-source",
                len(todo) - missing,
                f"{missing} source frame(s) do not exist",
            )
        if absent:
            return (
                folder,
                "failed",
                len(todo) - len(absent),
                f"{len(absent)} frame(s) do not list after copy",
            )
        return folder, "done", len(todo), ""
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never swallowed
        return folder, "failed", 0, f"{type(exc).__name__}: {exc}"[:200]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true", help="actually copy (default is a dry run)")
    ap.add_argument(
        "--since", default=DEFAULT_SINCE, help=f"first day, inclusive (default {DEFAULT_SINCE})"
    )
    ap.add_argument("--until", help="last day, inclusive (default today)")
    ap.add_argument("--limit", type=int, help="stop after N folders — use for a rehearsal")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--bucket", default="lucam-assets")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--progress", default="scripts/.backfill_public_done.txt")
    args = ap.parse_args()

    env = load_env(args.env_file)
    for required in ("DATABASE_URL", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
        if not env.get(required):
            sys.exit(
                f"{required} missing — export it, or check {args.env_file} (is the line commented out?)"
            )

    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until) if args.until else dt.date.today()
    done: set[str] = set()
    if os.path.exists(args.progress):
        done = {line.strip() for line in open(args.progress) if line.strip()}
    print(
        f"{'APPLY' if args.apply else 'DRY RUN'}  bucket={args.bucket}  {since} → {until}  "
        f"workers={args.workers}  already done: {len(done):,} folders"
    )

    tally: dict[str, int] = defaultdict(int)
    frames = 0
    failures: list[tuple[str, str]] = []
    finished: list[tuple[str, list[str]]] = []
    started = time.time()
    budget = args.limit
    conn = psycopg2.connect(pg_dsn(env["DATABASE_URL"]))
    progress_fh = open(args.progress, "a") if args.apply else None
    try:
        day = since
        while day <= until and (budget is None or budget > 0):
            with conn.cursor() as cur:
                folders = fetch_day(cur, args.bucket, day)
            work = [(f, k) for f, k in folders.items() if f not in done]
            if budget is not None:
                work = work[:budget]
                budget -= len(work)
            if work:
                day_started = time.time()
                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    futures = {
                        pool.submit(migrate_folder, env, args.bucket, f, k, args.apply): (f, k)
                        for f, k in work
                    }
                    for fut in as_completed(futures):
                        folder, outcome, n, detail = fut.result()
                        tally[outcome] += 1
                        frames += n
                        if outcome == "failed":
                            failures.append((folder, detail))
                            continue
                        if outcome in ("done", "skipped", "missing-source"):
                            if (
                                outcome != "missing-source"
                            ):  # a gone frame would fail the spot check
                                finished.append(futures[fut])
                            if progress_fh:
                                progress_fh.write(folder + "\n")
                                progress_fh.flush()
                rate = len(work) / max(time.time() - day_started, 0.001)
                print(
                    f"  {day}  {len(work):>6,} folders  {rate:5.1f}/s  "
                    f"total {dict(tally)}  frames {frames:,}  "
                    f"elapsed {(time.time() - started) / 60:.0f}m",
                    flush=True,
                )
            day += dt.timedelta(days=1)
    finally:
        conn.close()
        if progress_fh:
            progress_fh.close()

    print(
        f"\n{'=' * 60}\n{dict(tally)}  frames={frames:,}  in {(time.time() - started) / 60:.1f} min"
    )
    if tally["missing-source"]:
        print(
            f"  {tally['missing-source']:,} folder(s) had frames that no longer exist in the bucket — unrecoverable."
        )
    if failures:
        print(f"\n{len(failures)} FAILED FOLDERS (first 15):")
        for folder, detail in failures[:15]:
            print(f"  {folder}\n    {detail}")
        print("\nRe-run the same command: finished folders are skipped, only these are retried.")
        return 1
    if not args.apply:
        print("\nDry run only. Re-run with --apply to copy.")
        return 0

    # End-of-run SPOT CHECK — anonymous GET, the thing a browser actually does.
    # Done once on a sample, never in the hot loop: the public gateway lags a copy.
    sample = [k for _, keys in random.sample(finished, min(40, len(finished))) for k in keys[:1]]
    if sample:
        with httpx.Client(timeout=30, follow_redirects=True) as http:
            ok = sum(
                http.get(f"{ENDPOINT}/{args.bucket}/{PUBLIC_PREFIX}{k}").status_code == 200
                for k in sample
            )
        print(f"\nSPOT CHECK — {ok}/{len(sample)} sampled frames publicly readable")
        if ok < len(sample):
            print(
                "  The gateway lags a copy by seconds to minutes. Re-run (it skips everything) to re-check."
            )
            return 1
    print("\nCopies verified present and a sample is publicly readable.")
    print("NEXT: scripts/rewrite_public_urls.py (step 3) — not before this is clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
