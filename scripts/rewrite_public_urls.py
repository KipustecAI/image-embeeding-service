#!/usr/bin/env python
"""Step 3 — rewrite embedding frame URLs to ``public/`` in Postgres AND Qdrant.

RUN ONLY AFTER scripts/backfill_public_assets.py HAS FINISHED CLEANLY.
Rewriting first turns the UI's 403 into a 404, which is strictly worse: a 403
at least means the bytes are still there.

    https://storage.lookia.mx/<bucket>/embeddings/...
 →  https://storage.lookia.mx/<bucket>/public/embeddings/...

WHY QDRANT TOO
    Search results are built from the Qdrant payload (qdrant_repository.py,
    ``payload["image_url"]``), not from Postgres. Rewriting only the DB would
    leave every search result pointing at a private URL. etl-service did not
    have this problem; this service does.

WHAT IT REWRITES  (day by day, via embedding_requests.created_at — indexed)
    * Qdrant  — payload ``image_url`` of every point of the day
                (point ids from evidence_embeddings.qdrant_point_id)
    * evidence_embeddings.image_url   (text)
    * evidence_embeddings.json_data   (jsonb — embeds the same image_url)
    * embedding_requests.image_urls   (jsonb array)
    Qdrant goes first, then the DB in one transaction per day. Both are literal
    string replaces, so a crash mid-day is healed by re-running the same day.

CATALOG-DRIVEN SAFETY NET
    A hand-written column list is how you miss one (etl missed a cached search
    payload that way). So before touching anything the script scans EVERY
    text / varchar / json / jsonb / text[] column in the schema for the private
    prefix. Any column holding it that is NOT in the list above is printed as
    NOT HANDLED, and --apply refuses until you either add it here or pass
    --allow-unhandled after deciding it is not this service's to fix.

WHAT IT DELIBERATELY LEAVES ALONE
    * minio.lookia.mx — a decommissioned host; those objects are gone.
    * rows before --since (2026-09-01): their frames no longer exist in the
      bucket, so nothing was copied for them.
    * URLs this service did not mint (search_requests.image_url,
      image_index_results.source_url, blacklist_image_references.image_url) —
      caller-supplied; fix them at their producer.

SAFETY
    Dry run unless --apply. Each day: anonymous GET on a sample of the day's
    target URLs FIRST (Qdrant is not transactional, so nothing is written unless
    the bytes are already public) → Qdrant update → DB UPDATE → verify zero
    private rows remain → Qdrant read-back → COMMIT. Any failure stops the run.

ROLLBACK
    The inverse replace ('/public/embeddings/' → '/embeddings/') on the same
    columns and payload key. The original objects were never deleted.

CREDENTIALS
    Environment first, then --env-file (this repo's .env has a STALE Neon
    password — runbook §3): DATABASE_URL, QDRANT_HOST, QDRANT_PORT,
    QDRANT_API_KEY, QDRANT_COLLECTION_NAME.

USAGE
    python scripts/rewrite_public_urls.py                                   # dry run
    python scripts/rewrite_public_urls.py --apply --since 2026-09-01 --until 2026-09-01   # one-day rehearsal
    python scripts/rewrite_public_urls.py --apply                           # the real run
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import sys
import time

try:
    import httpx
    import psycopg2
    from qdrant_client import QdrantClient, models
except ImportError as exc:  # pragma: no cover - operator-facing
    sys.exit(f"missing dependency: {exc}. pip install httpx psycopg2-binary qdrant-client")

HOST = "storage.lookia.mx"
DEFAULT_SINCE = "2026-09-01"
QDRANT_BATCH = 256
HANDLED = {
    ("evidence_embeddings", "image_url"),
    ("evidence_embeddings", "json_data"),
    ("embedding_requests", "image_urls"),
}
ENV_KEYS = (
    "DATABASE_URL",
    "QDRANT_HOST",
    "QDRANT_PORT",
    "QDRANT_API_KEY",
    "QDRANT_COLLECTION_NAME",
)

DISCOVER = """
SELECT c.table_name, c.column_name, c.udt_name
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'
WHERE c.table_schema = 'public'
  AND c.table_name NOT LIKE '%%alembic%%'
  AND (c.data_type IN ('text','character varying','jsonb','json') OR c.udt_name IN ('_text','_varchar'))
ORDER BY c.table_name, c.ordinal_position
"""


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k in ENV_KEYS})
    return env


def as_text(column: str, udt: str) -> str:
    ident = f'"{column}"'
    return f"array_to_string({ident}, ',')" if udt in ("_text", "_varchar") else f"{ident}::text"


def discover(cur, old_like: str) -> list[tuple[str, str, int]]:
    """Every column, anywhere in the schema, that holds the private prefix."""
    cur.execute(DISCOVER)
    found = []
    for table, column, udt in cur.fetchall():
        cur.execute(
            f'SELECT count(*) FROM "{table}" WHERE {as_text(column, udt)} LIKE %s',  # noqa: S608 - catalog identifiers
            (old_like,),
        )
        n = cur.fetchone()[0]
        if n:
            found.append((table, column, n))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--since", default=DEFAULT_SINCE, help="first day, inclusive")
    ap.add_argument("--until", help="last day, inclusive (default today)")
    ap.add_argument(
        "--before",
        help="only rows created before this UTC timestamp, e.g. the backfill start. Use it while the "
        "consumer fix is NOT deployed yet: rows written after the backfill have no public copy.",
    )
    ap.add_argument("--bucket", default="lucam-assets")
    ap.add_argument("--sample", type=int, default=5, help="anonymous GETs per day before COMMIT")
    ap.add_argument("--allow-unhandled", action="store_true")
    ap.add_argument(
        "--skip-discovery", action="store_true", help="skip the full-schema scan (slow on 4 GB)"
    )
    ap.add_argument("--env-file", default=".env")
    args = ap.parse_args()

    env = load_env(args.env_file)
    for required in ("DATABASE_URL", "QDRANT_HOST", "QDRANT_COLLECTION_NAME"):
        if not env.get(required):
            sys.exit(f"{required} missing — export it, or check {args.env_file}")

    old = f"/{args.bucket}/embeddings/"
    new = f"/{args.bucket}/public/embeddings/"
    old_like = f"%{HOST}{old}%"
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until) if args.until else dt.date.today()

    qhost = env["QDRANT_HOST"]
    qdrant = QdrantClient(
        host=qhost,
        port=int(env.get("QDRANT_PORT") or 6333),
        api_key=env.get("QDRANT_API_KEY") or None,
        https=False,
        timeout=120,
        check_compatibility=False,
    )
    collection = env["QDRANT_COLLECTION_NAME"]

    dsn = (
        env["DATABASE_URL"]
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("?ssl=require", "?sslmode=require")
    )
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    print(f"{'APPLY' if args.apply else 'DRY RUN'}  {since} → {until}  collection={collection}")

    try:
        with conn.cursor() as cur:
            if not args.skip_discovery:
                print("\n  scanning every text/json column for the private prefix ...")
                found = discover(cur, old_like)
                unhandled = [f for f in found if (f[0], f[1]) not in HANDLED]
                for table, column, n in found:
                    tag = "handled" if (table, column) in HANDLED else "NOT HANDLED"
                    print(f"    {table}.{column:<20} {n:>10,} rows (all dates)  {tag}")
                if unhandled and args.apply and not args.allow_unhandled:
                    sys.exit(
                        "\n  REFUSING: columns above hold the private prefix and are not handled. "
                        "Add them to HANDLED, or pass --allow-unhandled if they are not ours to fix."
                    )
                conn.rollback()

        totals = {"points": 0, "emb_rows": 0, "req_rows": 0}
        started = time.time()
        day = since
        with httpx.Client(timeout=30, follow_redirects=True) as http:
            while day <= until:
                nxt = day + dt.timedelta(days=1)
                with conn.cursor() as cur:
                    nxt_ts = dt.datetime.combine(nxt, dt.time())
                    upper = min(nxt_ts, dt.datetime.fromisoformat(args.before)) if args.before else nxt_ts
                    cur.execute(
                        "SELECT id FROM embedding_requests WHERE created_at >= %s AND created_at < %s",
                        (day, upper),
                    )
                    req_ids = [r[0] for r in cur.fetchall()]
                    if not req_ids:
                        day = nxt
                        continue
                    cur.execute(
                        "SELECT qdrant_point_id, image_url FROM evidence_embeddings "
                        "WHERE request_id = ANY(%s::uuid[]) AND image_url LIKE %s",
                        (req_ids, old_like),
                    )
                    points = cur.fetchall()
                    cur.execute(
                        "SELECT count(*) FROM embedding_requests WHERE id = ANY(%s::uuid[]) AND image_urls::text LIKE %s",
                        (req_ids, old_like),
                    )
                    n_req = cur.fetchone()[0]

                    if not args.apply:
                        print(
                            f"  {day}  would rewrite {len(points):>8,} points/embedding rows  {n_req:>6,} requests"
                        )
                        totals["points"] += len(points)
                        totals["req_rows"] += n_req
                        conn.rollback()
                        day = nxt
                        continue

                    # 0. BEFORE ANY WRITE: a browser with no credentials can load what we are
                    #    about to point at. Qdrant is not transactional, so this gate must
                    #    come first — a failure here leaves nothing changed.
                    sample = [
                        u.replace(old, new)
                        for _, u in random.sample(points, min(args.sample, len(points)))
                    ]
                    bad = [u for u in sample if http.get(u).status_code != 200]
                    if bad:
                        conn.rollback()
                        print(
                            f"\n  STOPPED at {day} (nothing written): {len(bad)}/{len(sample)} rewritten URLs not publicly readable, e.g."
                        )
                        print(f"    {bad[0]}")
                        print(
                            "  Has the backfill finished for this day? Run it, then re-run from this day."
                        )
                        return 1

                    # 1. Qdrant — the URL search results actually serve.
                    for i in range(0, len(points), QDRANT_BATCH):
                        ops = [
                            models.SetPayloadOperation(
                                set_payload=models.SetPayload(
                                    payload={"image_url": url.replace(old, new)}, points=[pid]
                                )
                            )
                            for pid, url in points[i : i + QDRANT_BATCH]
                            if pid
                        ]
                        if ops:
                            qdrant.batch_update_points(collection, update_operations=ops, wait=True)

                    # 2. Postgres, one transaction for the day.
                    cur.execute(
                        "UPDATE evidence_embeddings SET "
                        "  image_url = replace(image_url, %s, %s), "
                        "  json_data = replace(json_data::text, %s, %s)::jsonb "
                        "WHERE request_id = ANY(%s::uuid[]) "
                        "  AND (image_url LIKE %s OR json_data::text LIKE %s)",
                        (old, new, old, new, req_ids, old_like, old_like),
                    )
                    emb_rows = cur.rowcount
                    cur.execute(
                        "UPDATE embedding_requests SET image_urls = replace(image_urls::text, %s, %s)::jsonb "
                        "WHERE id = ANY(%s::uuid[]) AND image_urls::text LIKE %s",
                        (old, new, req_ids, old_like),
                    )
                    req_rows = cur.rowcount

                    # 3. Verify nothing private is left for the day.
                    cur.execute(
                        "SELECT count(*) FROM evidence_embeddings WHERE request_id = ANY(%s::uuid[]) "
                        "AND (image_url LIKE %s OR json_data::text LIKE %s)",
                        (req_ids, old_like, old_like),
                    )
                    left = cur.fetchone()[0]
                    if left:
                        conn.rollback()
                        print(f"\n  ROLLBACK {day}: {left:,} rows still private")
                        return 1

                    # 5. Qdrant read-back on one point.
                    pid = next((p for p, _ in points if p), None)
                    if pid:
                        got = qdrant.retrieve(collection, ids=[pid], with_payload=["image_url"])
                        if not got or new not in (got[0].payload or {}).get("image_url", ""):
                            conn.rollback()
                            print(
                                f"\n  ROLLBACK {day}: Qdrant point {pid} did not take the new URL"
                            )
                            return 1

                    conn.commit()
                    totals["points"] += len(points)
                    totals["emb_rows"] += emb_rows
                    totals["req_rows"] += req_rows
                    print(
                        f"  {day}  qdrant {len(points):>8,}  evidence_embeddings {emb_rows:>8,}  "
                        f"embedding_requests {req_rows:>6,}  sample {len(sample)}/{len(sample)} ok  "
                        f"elapsed {(time.time() - started) / 60:.0f}m",
                        flush=True,
                    )
                day = nxt

        print(f"\n{'=' * 60}\n{totals}  in {(time.time() - started) / 60:.1f} min")
        if not args.apply:
            print("Dry run. Re-run with --apply (after the backfill is clean).")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
