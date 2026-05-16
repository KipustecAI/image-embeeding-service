"""Measure input→output latency between two Redis Streams retroactively.

Each Redis Stream entry id is ``<unix_ms>-<seq>`` (see Redis docs on
``XADD``). That gives a free, per-message timestamp without touching
the consumer. This script reads recent entries of both streams,
matches by a shared payload key (defaults to ``evidence_id``), and
prints per-event deltas + percentile stats.

Designed for "is our consumer slow?" debugging on production data
without restarting anything. See docs/weapons/RUNTIME.md for the
weapons flow we're typically measuring.

Examples
--------
Weapons flow (default):
    python scripts/measure_stream_latency.py

Blacklist inline-match latency:
    python scripts/measure_stream_latency.py \\
        --input-stream embeddings:results \\
        --output-stream image:blacklist_match

User-facing search latency (request → returned vector):
    python scripts/measure_stream_latency.py \\
        --input-stream evidence:search \\
        --output-stream search:results \\
        --match-key search_id

Connection: reads REDIS_HOST / REDIS_PORT / REDIS_PASSWORD /
REDIS_STREAMS_DB from env. Defaults to localhost:6379 db=3 — same
config the backend uses. Point it at prod by exporting those vars.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from statistics import median

import redis


def parse_id_ms(stream_id: str) -> int:
    """Extract the unix-ms portion of a Redis stream entry id."""
    return int(stream_id.split("-", 1)[0])


def decode_envelope(entry: tuple) -> tuple[str, str, dict]:
    """Return (stream_id, event_type, payload_dict) for one XRANGE entry.

    Payload is JSON-encoded inside the envelope by our StreamProducer
    (see src/streams/producer.py). Malformed JSON yields an empty
    dict — we'd rather skip a corrupt row than crash the whole scan.
    """
    stream_id, fields = entry
    raw_payload = fields.get("payload", "{}")
    try:
        payload = json.loads(raw_payload)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return stream_id, fields.get("event_type", ""), payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure input→output stream latency from Redis IDs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input-stream",
        default="embeddings:results",
        help="Stream the consumer reads from (default: embeddings:results)",
    )
    parser.add_argument(
        "--output-stream",
        default="weapons:detected",
        help="Stream the consumer publishes to (default: weapons:detected)",
    )
    parser.add_argument(
        "--match-key",
        default="evidence_id",
        help="Payload field used to pair input ↔ output entries (default: evidence_id)",
    )
    parser.add_argument(
        "--output-limit",
        type=int,
        default=200,
        help="Number of recent output entries to sample (default: 200)",
    )
    parser.add_argument(
        "--input-window",
        type=int,
        default=2000,
        help="How far back to scan the input stream looking for matches "
        "(default: 2000). Larger = more matches found at the cost of a "
        "bigger XREVRANGE.",
    )
    parser.add_argument(
        "--show-rows",
        type=int,
        default=20,
        help="How many per-event rows to print (default: 20, sorted oldest first). "
        "Set 0 to skip rows and only print stats.",
    )
    args = parser.parse_args()

    host = os.environ.get("REDIS_HOST", "localhost")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD") or None
    db = int(os.environ.get("REDIS_STREAMS_DB", "3"))

    print(
        f"# Connecting to redis://{host}:{port}/{db} "
        f"(auth={'yes' if password else 'no'})",
        file=sys.stderr,
    )

    try:
        r = redis.Redis(
            host=host, port=port, password=password, db=db, decode_responses=True
        )
        r.ping()
    except redis.exceptions.RedisError as e:
        print(f"# Redis connection failed: {e}", file=sys.stderr)
        return 2

    # ── Surface consumer-group lag as a separate diagnostic ──
    # Even before computing per-event latency, the consumer-group lag
    # tells you whether the consumer is keeping up at all. A big lag here
    # is the actual problem; per-event latency is meaningless if the
    # consumer is hours behind on intake.
    try:
        groups = r.xinfo_groups(args.input_stream)
        print(f"\n# Consumer groups on {args.input_stream}:", file=sys.stderr)
        for g in groups:
            print(
                f"#   name={g.get('name')!s:30s} "
                f"consumers={g.get('consumers'):>3} "
                f"pending={g.get('pending'):>4} "
                f"lag={g.get('lag', 'n/a'):>6} "
                f"last-delivered-id={g.get('last-delivered-id')}",
                file=sys.stderr,
            )
    except redis.exceptions.RedisError as e:
        print(f"# (XINFO GROUPS unavailable: {e})", file=sys.stderr)

    # ── Sample the output stream ──
    # XREVRANGE returns newest first. We index by match-key so multiple
    # outputs sharing the same key (e.g. reverse-search backfills that
    # republish for an evidence_id) collapse to the most recent.
    out_entries = r.xrevrange(args.output_stream, count=args.output_limit)
    if not out_entries:
        print(f"# {args.output_stream} is empty.", file=sys.stderr)
        return 1

    output_by_key: dict[str, str] = {}
    for entry in out_entries:
        sid, _evt, payload = decode_envelope(entry)
        key = payload.get(args.match_key)
        if key and key not in output_by_key:
            output_by_key[key] = sid

    if not output_by_key:
        print(
            f"# No output entries with `{args.match_key}` in last "
            f"{args.output_limit} of {args.output_stream}.",
            file=sys.stderr,
        )
        return 1

    # ── Per-output, scan inputs backward FROM the output's time ──
    # This is the load-bearing fix: a naive "match any input with the
    # same key" matches on the wrong end of the stream when the same
    # evidence_id legitimately appears multiple times (re-ingestion,
    # backfills). We constrain the input search to entries with id ≤
    # output_id, then take the most recent match — that's the input
    # the consumer actually processed to produce this output.
    matches: list[tuple[str, int, int]] = []  # (key, in_ms, out_ms)
    no_match: list[tuple[str, int]] = []  # outputs we couldn't pair

    for key, out_sid in output_by_key.items():
        out_ms = parse_id_ms(out_sid)
        # Scan up to args.input_window inputs at or before the output time.
        in_entries = r.xrevrange(
            args.input_stream, max=out_sid, min="-", count=args.input_window
        )
        for entry in in_entries:
            in_sid, _evt, payload = decode_envelope(entry)
            if payload.get(args.match_key) == key:
                in_ms = parse_id_ms(in_sid)
                matches.append((key, in_ms, out_ms))
                break
        else:
            no_match.append((key, out_ms))

    if not matches:
        print(
            f"# Sampled {len(output_by_key)} unique `{args.match_key}` values "
            f"from {args.output_stream} but found no matching input within "
            f"{args.input_window} entries before each output. The matching "
            f"inputs are probably evicted from the stream (trimmed by MAXLEN). "
            f"Try --input-window larger, or accept that older outputs aren't "
            f"measurable retroactively.",
            file=sys.stderr,
        )
        if no_match:
            sample = ", ".join(k for k, _ in no_match[:3])
            print(f"# Unmatched samples: {sample}{'...' if len(no_match) > 3 else ''}",
                  file=sys.stderr)
        return 1

    # ── Print per-event rows ──
    deltas = sorted(out_ms - in_ms for _, in_ms, out_ms in matches)
    matches_chrono = sorted(matches, key=lambda m: m[1])

    if args.show_rows > 0:
        rows = matches_chrono[-args.show_rows:]
        key_col_w = max(len(args.match_key), max(len(k) for k, _, _ in rows))
        print(
            f"\n{args.match_key:<{key_col_w}}  "
            f"{'in_id_ms':>16}  {'out_id_ms':>16}  {'delta_ms':>10}"
        )
        print("-" * (key_col_w + 50))
        for key, in_ms, out_ms in rows:
            print(
                f"{key:<{key_col_w}}  {in_ms:>16}  {out_ms:>16}  "
                f"{out_ms - in_ms:>10}"
            )

    # ── Percentile stats ──
    def pct(p: float) -> int:
        idx = min(int(len(deltas) * p), len(deltas) - 1)
        return deltas[idx]

    print(f"\n# {args.input_stream} → {args.output_stream}  ({args.match_key})")
    print(
        f"# {len(matches)} matches "
        f"(output sample: {args.output_limit}, input window: {args.input_window})"
    )
    print(f"#   min  : {deltas[0]:>10} ms")
    print(f"#   p50  : {int(median(deltas)):>10} ms")
    print(f"#   p90  : {pct(0.90):>10} ms")
    print(f"#   p99  : {pct(0.99):>10} ms")
    print(f"#   max  : {deltas[-1]:>10} ms")
    print(f"#   avg  : {int(sum(deltas) / len(deltas)):>10} ms")

    # If percentiles look ugly, surface the worst offenders so the user
    # can grep their logs for those evidence_ids.
    if pct(0.90) > 5000:  # heuristic — adjust if needed
        slowest = sorted(matches, key=lambda m: (m[2] - m[1]))[-5:]
        print("\n# slowest 5 (look these up in logs):")
        for key, in_ms, out_ms in slowest:
            print(f"#   {key}  delta={out_ms - in_ms} ms")

    return 0


if __name__ == "__main__":
    sys.exit(main())
