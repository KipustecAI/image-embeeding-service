#!/usr/bin/env python3
"""Live e2e for the on-demand image-index feature — exercises BOTH docs/apis/ contracts.

  SUBMIT  (docs/apis/IMAGE_INDEX_SUBMIT.md)  — XADD image:index:submit on the shared
          Redis (DB 3), exactly as a no-HTTP coordinator (dw-offline) would.
  READ    (docs/apis/IMAGE_INDEX_API.md)     — poll GET /api/v1/embedding/image-index/
          results/by-external-id/{external_id} through the API gateway with an API key.

The deployed ms-embedding-api's submit + results consumers do the middle (mint → dispatch
to image:index → image-embedding-compute embeds → land in Qdrant+Postgres → lifecycle).

Secrets: Redis creds are read from .env; the gateway API key is passed at call time
(--api-key or IMAGE_INDEX_API_KEY) and is NEVER printed or persisted.

Modes:
  --groups                 Preconditions only: are the compute + our consumer groups live?
  (default)                Full submit → poll → verify against the two contracts.

Usage:
  IMAGE_INDEX_API_KEY=<key> python3 scripts/test_e2e_image_index.py \
      --gateway https://api.lookia.mx --user-id <tenant-uuid>
  python3 scripts/test_e2e_image_index.py --groups
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV = REPO / ".env"
URLS_FILE = REPO / "data" / "image_urls.text"

# Frozen contract stream/group names (docs/requirements/IMAGE_INDEX_COMPUTE.md).
SUBMIT_STREAM = "image:index:submit"
DISPATCH_STREAM = "image:index"
RESULTS_STREAM = "image:index:results"
LIFECYCLE_STREAM = "image_batch:raw"
PROGRESS_STREAM = "image:index:progress"  # v1.2 advisory (compute-emitted, >=20 items)
COMPUTE_GROUP = "image-index-compute-workers"  # compute's group on image:index


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def redis_client(env: dict):
    try:
        import redis  # noqa: PLC0415
    except ImportError:
        sys.exit("redis-py not installed in this env — `pip install redis`")
    return redis.Redis(
        host=env.get("REDIS_HOST", "localhost"),
        port=int(env.get("REDIS_PORT", 6379)),
        password=env.get("REDIS_PASSWORD") or None,
        db=int(env.get("REDIS_STREAMS_DB", 3)),
        socket_connect_timeout=10,
        decode_responses=True,
    )


def read_urls(args_urls: list[str]) -> list[str]:
    if args_urls:
        return args_urls
    if not URLS_FILE.exists():
        sys.exit(f"No URLs given and {URLS_FILE} not found")
    return re.findall(r"https?://\S+", URLS_FILE.read_text())


def group_exists(r, stream: str, group: str) -> bool:
    try:
        return any(g["name"] == group for g in r.xinfo_groups(stream))
    except Exception:
        return False  # stream doesn't exist yet


def check_groups(r) -> bool:
    print("── Preconditions: consumer groups ──")
    ok = True
    checks = [
        (DISPATCH_STREAM, COMPUTE_GROUP, "compute embeds our dispatch"),
        (SUBMIT_STREAM, None, "our submit-intake stream exists"),
        (RESULTS_STREAM, None, "our results stream exists"),
    ]
    for stream, group, why in checks:
        try:
            xlen = r.xlen(stream)
        except Exception:
            xlen = None
        if group:
            present = group_exists(r, stream, group)
            ok = ok and present
            mark = "✓" if present else "✗ MISSING (pre-group XADDs are DROPPED — do not submit)"
            print(f"  {mark}  group {group!r} on {stream}  (xlen={xlen}) — {why}")
        else:
            print(f"  •  {stream} xlen={xlen} — {why}")
    return ok


def watch_lifecycle(r, external_id: str, attempts: int = 60, interval: float = 2.0):
    """READ leg over Redis instead of the gateway: tail image_batch:raw for OUR
    external_id until a terminal image_batch.completed/failed. Proves the full
    submit→compute→land pipeline (IMAGE_INDEX_SUBMIT.md) with no API key needed."""
    print("── WATCH LIFECYCLE (image_batch:raw) ──")
    last_id = "0-0"  # from the start of the (short, MAXLEN-trimmed) stream
    terminal_types = {"image_batch.completed", "image_batch.failed"}
    seen = set()
    for _ in range(attempts):
        entries = r.xread({LIFECYCLE_STREAM: last_id}, count=200, block=int(interval * 1000))
        if entries:
            for _stream, msgs in entries:
                for msg_id, fields in msgs:
                    last_id = msg_id
                    try:
                        payload = json.loads(fields.get("payload", "{}"))
                    except Exception:
                        continue
                    if payload.get("external_id") != external_id:
                        continue
                    et = fields.get("event_type")
                    if et not in seen:
                        seen.add(et)
                        print(f"  {et}  batch_id={payload.get('batch_id')} "
                              f"status={payload.get('status')} counts={payload.get('counts')}")
                    if et in terminal_types:
                        return payload
    return None


def http_get(url: str, api_key: str, timeout: int = 30):
    # A real User-Agent: the gateway sits behind Cloudflare, which 403s (code
    # 1010) the default "Python-urllib" signature. curl's UA passes; mirror it.
    req = urllib.request.Request(
        url,
        headers={
            "X-API-Key": api_key,
            "User-Agent": "curl/8.4.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body
    except urllib.error.URLError as e:
        return None, str(e)


def build_items(urls: list[str], count: int, alias: bool) -> list[dict]:
    """N items, URLs replicated round-robin. `alias` → images/image_id (v1.1)."""
    n = count or len(urls)
    id_key = "image_id" if alias else "item_id"
    return [{"image_url": urls[i % len(urls)], id_key: f"crop-{i}"} for i in range(n)]


def http_post(url: str, api_key: str, body: dict, timeout: int = 60):
    """POST JSON through the gateway (real UA — Cloudflare 403s Python-urllib)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"X-API-Key": api_key, "User-Agent": "curl/8.4.0",
                 "Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        try:
            body_txt = json.loads(body_txt)
        except Exception:
            pass
        return e.code, body_txt
    except urllib.error.URLError as e:
        return None, str(e)


def qdrant_count_external_id(env: dict, external_id: str) -> dict:
    """Live-Qdrant verification (read-only): count image_index_embeddings points
    for an external_id + sample the model_version stamp. Uses .env QDRANT_* creds."""
    try:
        from qdrant_client import QdrantClient  # noqa: PLC0415
        from qdrant_client.http import models as qm  # noqa: PLC0415
    except ImportError:
        return {"error": "qdrant-client not installed"}
    host = env.get("QDRANT_HOST", "localhost")
    client = QdrantClient(host=host, port=int(env.get("QDRANT_PORT", 6333)),
                          api_key=env.get("QDRANT_API_KEY") or None, https=False, timeout=15)
    flt = qm.Filter(must=[qm.FieldCondition(key="external_id", match=qm.MatchValue(value=external_id))])
    pts, _ = client.scroll(collection_name="image_index_embeddings", scroll_filter=flt,
                           limit=100, with_payload=True, with_vectors=False)
    mv = {p.payload.get("model_version") for p in pts}
    return {"points": len(pts), "model_versions": sorted(v for v in mv if v)}


def submit(r, user_id, external_id, client_ref, items, *, alias: bool = False) -> None:
    list_key = "images" if alias else "items"  # v1.1 vocabulary alias
    payload = {
        "user_id": user_id,
        "client_batch_ref": client_ref,
        "external_id": external_id,
        "source_ref": "image-index-e2e",
        list_key: items,
    }
    msg_id = r.xadd(SUBMIT_STREAM, {"event_type": "image.index.submit", "payload": json.dumps(payload)})
    print("── SUBMIT (IMAGE_INDEX_SUBMIT.md) ──")
    print(f"  XADD {SUBMIT_STREAM} msg={msg_id}  ({list_key}/{'image_id' if alias else 'item_id'} vocab)")
    print(f"  external_id={external_id}  client_batch_ref={client_ref}  items={len(items)}")


def watch_progress_and_lifecycle(r, external_id, attempts=90, interval=2.0):
    """v1.2: tail image:index:progress + image_batch:raw for OUR external_id.

    Reports every progress frame (stage/processed/total) and the terminal
    lifecycle. progress is keyed on batch_id (learned from image_batch.created).
    """
    print("── WATCH PROGRESS (image:index:progress) + LIFECYCLE (image_batch:raw) ──")
    last = {LIFECYCLE_STREAM: "0-0", PROGRESS_STREAM: "$"}  # progress: only new frames
    batch_id = None
    frames = 0
    terminal = {"image_batch.completed", "image_batch.failed"}
    for _ in range(attempts):
        entries = r.xread(last, count=500, block=int(interval * 1000))
        for stream, msgs in entries or []:
            for msg_id, fields in msgs:
                last[stream] = msg_id
                try:
                    p = json.loads(fields.get("payload", "{}"))
                except Exception:
                    continue
                et = fields.get("event_type")
                if stream == LIFECYCLE_STREAM:
                    if p.get("external_id") != external_id:
                        continue
                    if et == "image_batch.created":
                        batch_id = p.get("batch_id")
                        print(f"  lifecycle {et}  batch_id={batch_id} status={p.get('status')}")
                    elif et in terminal:
                        print(f"  lifecycle {et}  status={p.get('status')} counts={p.get('counts')}")
                        print(f"  → progress frames seen: {frames}")
                        return p, frames
                elif stream == PROGRESS_STREAM and batch_id and p.get("batch_id") == batch_id:
                    frames += 1
                    print(f"  progress  stage={p.get('stage'):<11} "
                          f"processed={p.get('processed')}/{p.get('total')} "
                          f"embedded={p.get('embedded_so_far')} failed={p.get('failed_so_far')}")
    return None, frames


def poll(gateway: str, api_key: str, external_id: str, attempts: int = 40, interval: float = 2.0):
    url = f"{gateway}/api/v1/embedding/image-index/results/by-external-id/{external_id}?include_items=true"
    print(f"── READ (IMAGE_INDEX_API.md) ──\n  GET {url}")
    terminal = {"completed", "completed_with_errors", "error"}
    last = None
    for n in range(1, attempts + 1):
        code, body = http_get(url, api_key)
        if code == 404:
            last = (code, body)
            print(f"  [{n}] 404 — not landed yet (or gateway route/tenant miss); retrying…")
            time.sleep(interval)
            continue
        if code != 200:
            return code, body
        status = body.get("status")
        counts = body.get("counts", {})
        print(f"  [{n}] status={status} counts={counts}")
        if status in terminal:
            return code, body
        last = (code, body)
        time.sleep(interval)
    return last if last else (None, "no response")


def verify(body: dict) -> bool:
    print("── VERIFY (contract invariants) ──")
    ok = True
    counts = body.get("counts", {})
    submitted = counts.get("submitted", 0)
    embedded = counts.get("embedded", 0)
    filtered = counts.get("filtered", 0)
    failed = counts.get("failed", 0)

    def check(label, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  {'✓' if cond else '✗'}  {label}")

    check(f"status terminal ({body.get('status')})",
          body.get("status") in {"completed", "completed_with_errors", "error"})
    check(f"reconciliation submitted({submitted}) == embedded({embedded}) + failed({failed})",
          submitted == embedded + failed)
    check(f"filtered == 0 (dedup disabled in v1) — got {filtered}", filtered == 0)
    items = body.get("items", [])
    check(f"one item row per submitted ({len(items)} == {submitted})", len(items) == submitted)
    for it in items:
        st = it.get("status")
        if st == "embedded":
            check(f"embedded item {it.get('item_ref')} has qdrant_point_id",
                  bool(it.get("qdrant_point_id")))
        elif st in {"download_failed", "decode_failed", "no_result"}:
            print(f"  •  item {it.get('item_ref')} disposition={st} (no vector — expected)")
        else:
            check(f"item {it.get('item_ref')} has a known disposition (got {st!r})", False)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default=os.environ.get("IMAGE_INDEX_GATEWAY", "https://api.lookia.mx"))
    ap.add_argument("--api-key", default=os.environ.get("IMAGE_INDEX_API_KEY", ""))
    ap.add_argument("--user-id", default=os.environ.get("IMAGE_INDEX_USER_ID", ""))
    ap.add_argument("--external-id", default=f"e2e-{int(time.time())}")
    ap.add_argument("--groups", action="store_true", help="only check consumer-group preconditions")
    ap.add_argument("--redis-only", action="store_true",
                    help="submit + watch image_batch:raw lifecycle (no gateway/API key needed)")
    ap.add_argument("--progress", action="store_true",
                    help="v1.2: submit a batch + tail image:index:progress + lifecycle (no key needed)")
    ap.add_argument("--count", type=int, default=0,
                    help="number of items to submit (URLs replicated round-robin; default = #urls)")
    ap.add_argument("--alias", action="store_true",
                    help="v1.1: submit with the images/image_id vocabulary alias")
    ap.add_argument("--search", action="store_true",
                    help="Cap-A: POST /image-index/search a query image over --external-ids, poll, verify")
    ap.add_argument("--xref", action="store_true",
                    help="Cap-B: POST a blacklist entry cross-reference over --external-ids (needs --entry-id)")
    ap.add_argument("--query", default="", help="Cap-A query image URL (default: first data URL)")
    ap.add_argument("--entry-id", default="", help="Cap-B blacklist entry id")
    ap.add_argument("--external-ids", default="", help="comma-separated external_ids to scope search/xref")
    ap.add_argument("--verify-qdrant", action="store_true",
                    help="after: count image_index_embeddings points for the external_id(s) via .env QDRANT_*")
    ap.add_argument("urls", nargs="*", help="image URLs (default: data/image_urls.text)")
    args = ap.parse_args()

    env = load_env(ENV)
    r = redis_client(env)
    try:
        r.ping()
    except Exception as e:
        return _fail(f"Cannot reach Redis {env.get('REDIS_HOST')}:{env.get('REDIS_PORT')} db{env.get('REDIS_STREAMS_DB')} — {e}")

    groups_ok = check_groups(r)
    if args.groups:
        print("\nRESULT:", "READY to submit" if groups_ok else "NOT ready (compute group missing)")
        return 0 if groups_ok else 2
    if not groups_ok:
        return _fail("compute group not live — submitting now would drop the dispatch. Aborting.")

    # ── Cap-A: async search-by-image over external_ids (gateway POST → poll) ──
    if args.search:
        if not args.api_key or not args.user_id:
            return _fail("--search needs --api-key + --user-id (the key's tenant)")
        ext_ids = [e for e in args.external_ids.split(",") if e] or [args.external_id]
        query = args.query or read_urls(args.urls)[0]
        base = f"{args.gateway}/api/v1/embedding/image-index/search"
        print(f"── Cap-A SEARCH ──\n  query={query}\n  external_ids={ext_ids}")
        code, body = http_post(base, args.api_key,
                               {"image_url": query, "external_ids": ext_ids,
                                "threshold": 0.75, "max_results": 50})
        if code != 202:
            return _fail(f"POST /search → HTTP {code} {body}")
        sid = body.get("search_id")
        print(f"  search_id={sid} (submitted; compute embeds the query → we search)")
        term = {"completed", "completed_with_errors", "error"}
        st = None
        for n in range(1, 41):
            c2, b2 = http_get(f"{base}/{sid}", args.api_key)
            if c2 == 200:
                st = b2.get("status")
                print(f"  [{n}] status={st} total_matches={b2.get('total_matches')}")
                if st in term:
                    break
            time.sleep(2)
        c3, mb = http_get(f"{base}/{sid}/matches?limit=50", args.api_key)
        print(json.dumps(mb, indent=2, default=str))
        matches = mb.get("matches", []) if isinstance(mb, dict) else []
        top = max((m.get("similarity_score", 0) for m in matches), default=0)
        ok = st == "completed" and top >= 0.9 and all(
            m.get("external_id") in ext_ids for m in matches)
        print(f"\n  ✓ terminal completed: {st == 'completed'}")
        print(f"  ✓ self-match present (top score {top:.3f} ≥ 0.9): {top >= 0.9}")
        print(f"  ✓ every match scoped to {ext_ids}: {all(m.get('external_id') in ext_ids for m in matches)}")
        if args.verify_qdrant:
            for e in ext_ids:
                print(f"  qdrant[{e}]:", qdrant_count_external_id(env, e))
        print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
        return 0 if ok else 1

    # ── Cap-B: GPU-free blacklist cross-reference (sync inline) ──
    if args.xref:
        if not args.api_key or not args.entry_id:
            return _fail("--xref needs --api-key + --entry-id (a blacklist entry)")
        ext_ids = [e for e in args.external_ids.split(",") if e] or [args.external_id]
        # GPU-free proof: embed-dispatch streams must NOT grow during the xref.
        before = {s: r.xlen(s) for s in (DISPATCH_STREAM, "evidence:search")}
        url = f"{args.gateway}/api/v1/images/blacklist/{args.entry_id}/cross-reference"
        print(f"── Cap-B CROSS-REFERENCE ──\n  entry={args.entry_id}  external_ids={ext_ids}")
        code, body = http_post(url, args.api_key, {"external_ids": ext_ids, "threshold": 0.85})
        if code != 200:
            return _fail(f"POST cross-reference → HTTP {code} {body}")
        print(json.dumps(body, indent=2, default=str))
        after = {s: r.xlen(s) for s in (DISPATCH_STREAM, "evidence:search")}
        gpu_free = before == after
        matches = body.get("matches", []) if isinstance(body, dict) else []
        top = max((m.get("similarity_score", 0) for m in matches), default=0)
        ok = code == 200 and gpu_free and top >= 0.9
        print(f"\n  ✓ GPU-free (no embed-stream growth): {gpu_free}  ({before} → {after})")
        print(f"  ✓ match found (top score {top:.3f} ≥ 0.9): {top >= 0.9}")
        if args.verify_qdrant:
            for e in ext_ids:
                print(f"  qdrant[{e}]:", qdrant_count_external_id(env, e))
        print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
        return 0 if ok else 1

    # ── v1.2 progress mode: submit >=20 items + tail image:index:progress ──
    if args.progress:
        user_id = args.user_id or str(uuid.uuid4())
        urls = read_urls(args.urls)
        count = args.count or 22  # default >= IMAGE_INDEX_PROGRESS_MIN_BATCH=20
        items = build_items(urls, count, args.alias)
        client_ref = f"{args.external_id}-{uuid.uuid4().hex[:8]}"
        print(f"\ntenant={user_id}  items={len(items)}  (progress mode; need >=20 for frames)\n")
        submit(r, user_id, args.external_id, client_ref, items, alias=args.alias)
        print("  (watching for compute progress frames + terminal…)\n")
        term, frames = watch_progress_and_lifecycle(r, args.external_id)
        if term is None:
            return _fail("no terminal lifecycle seen — batch stuck or trimmed")
        counts = term.get("counts", {})
        recon = counts.get("submitted", 0) == counts.get("embedded", 0) + counts.get("failed", 0)
        prog_ok = frames > 0 if len(items) >= 20 else True
        print(f"\nbatch_id = {term.get('batch_id')}")
        print(f"reconciliation ok: {recon} | progress frames ({len(items)} items): "
              f"{frames} {'(expected >=1)' if len(items) >= 20 else '(none expected <20)'}")
        ok = recon and prog_ok
        print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
        return 0 if ok else 1

    # ── Redis-only mode: submit + watch the lifecycle stream, no gateway needed ──
    if args.redis_only:
        user_id = args.user_id or str(uuid.uuid4())
        urls = read_urls(args.urls)
        items = build_items(urls, args.count, args.alias)
        client_ref = f"{args.external_id}-{uuid.uuid4().hex[:8]}"
        print(f"\ntenant={user_id}  items={len(items)}  (Redis-only — no gateway READ)\n")
        submit(r, user_id, args.external_id, client_ref, items, alias=args.alias)
        print("  (waiting for compute + land…)\n")
        term = watch_lifecycle(r, args.external_id)
        if term is None:
            return _fail("no terminal image_batch.* lifecycle event seen — batch may be stuck "
                         "(check ms-embedding-api logs) or MAXLEN trimmed it")
        counts = term.get("counts", {})
        ok = (counts.get("submitted", 0) == counts.get("embedded", 0) + counts.get("failed", 0)
              and counts.get("filtered", 1) == 0)
        print(f"\nbatch_id = {term.get('batch_id')}  (send to image-embedding-compute to cross-check)")
        print("reconciliation submitted == embedded + failed, filtered == 0:", ok)
        print("\nRESULT:", "PASS ✅ (pipeline live; gateway READ leg still needs an API key)"
              if ok else "FAIL ❌")
        return 0 if ok else 1

    if not args.api_key:
        return _fail("no API key — pass --api-key or IMAGE_INDEX_API_KEY (needed for the gateway READ leg)")
    if not args.user_id:
        return _fail("no --user-id — must equal the tenant your API key resolves to, or the READ 404s")

    urls = read_urls(args.urls)
    items = build_items(urls, args.count, args.alias)
    client_ref = f"{args.external_id}-{uuid.uuid4().hex[:8]}"
    print(f"\nGateway={args.gateway}  tenant={args.user_id}  items={len(items)}\n")
    submit(r, args.user_id, args.external_id, client_ref, items, alias=args.alias)
    print("  (waiting for compute + land…)\n")
    time.sleep(3)
    code, body = poll(args.gateway, args.api_key, args.external_id)
    if code != 200:
        return _fail(f"READ did not reach a terminal 200 — last: HTTP {code} {body}")
    print(json.dumps(body, indent=2, default=str))
    ok = verify(body)
    bid = body.get("batch_id")
    print(f"\nbatch_id = {bid}  (send this to image-embedding-compute to cross-check their log line)")
    print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
