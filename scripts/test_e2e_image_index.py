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


def submit(r, user_id: str, external_id: str, client_ref: str, urls: list[str]) -> None:
    items = [{"image_url": u, "item_id": f"crop-{i}"} for i, u in enumerate(urls)]
    payload = {
        "user_id": user_id,
        "client_batch_ref": client_ref,
        "external_id": external_id,
        "source_ref": "image-index-e2e",
        "items": items,
    }
    msg_id = r.xadd(SUBMIT_STREAM, {"event_type": "image.index.submit", "payload": json.dumps(payload)})
    print("── SUBMIT (IMAGE_INDEX_SUBMIT.md) ──")
    print(f"  XADD {SUBMIT_STREAM} msg={msg_id}")
    print(f"  external_id={external_id}  client_batch_ref={client_ref}  items={len(items)}")


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

    # ── Redis-only mode: submit + watch the lifecycle stream, no gateway needed ──
    if args.redis_only:
        user_id = args.user_id or str(uuid.uuid4())
        urls = read_urls(args.urls)
        client_ref = f"{args.external_id}-{uuid.uuid4().hex[:8]}"
        print(f"\ntenant={user_id}  urls={len(urls)}  (Redis-only — no gateway READ)\n")
        submit(r, user_id, args.external_id, client_ref, urls)
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
    client_ref = f"{args.external_id}-{uuid.uuid4().hex[:8]}"
    print(f"\nGateway={args.gateway}  tenant={args.user_id}  urls={len(urls)}\n")
    submit(r, args.user_id, args.external_id, client_ref, urls)
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
