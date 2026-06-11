#!/usr/bin/env python3
"""Query the tiktok-agent logs shipped to Elasticsearch Serverless.

Auth + endpoint come from the project `.env` (ES_LOG_URL, ES_API_KEY,
ES_LOG_INDEX) — real environment variables win over `.env`, matching the app.
Stdlib only; no dependencies.

Document shape (ECS-ish, dotted fields stored as nested objects):
  @timestamp, log.level, log.logger, message, service.name, host.name,
  process.pid, process.thread.name, error.type/message/stack_trace,
  labels.{...}  (per-flow context: post id, PostURL, mqtt.payload, ...)

Examples:
  es_logs.py --since 1h                      # last hour, newest first
  es_logs.py --since 2h --level warn         # warnings + errors (>= warn)
  es_logs.py --errors --since 24h            # only records carrying an error.type
  es_logs.py --grep "needs_manual"           # full-text match on message
  es_logs.py --service tiktok-commenter      # one CLI's records
  es_logs.py --stats --since 6h              # counts by level / service / logger
  es_logs.py --mqtt --since 1h               # show full MQTT payloads (labels.mqtt.payload)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Canonical severity order. The app renames WARNING -> WARN (logging_setup), so
# records are stored as "warn"; we accept "warning" as a synonym on input.
LEVEL_ORDER = ["debug", "info", "warn", "error", "critical"]


def load_env() -> None:
    """Load .env from the project root (cwd or this file's repo) without clobbering real env."""
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"]
    for p in candidates:
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
        break


def parse_since(s: str) -> str:
    """'90m'/'2h'/'1d'/'45s' -> ES date-math 'now-90m'. Bare number -> hours."""
    s = s.strip().lower()
    if re.fullmatch(r"\d+", s):
        s += "h"
    if not re.fullmatch(r"\d+[smhdw]", s):
        raise SystemExit(f"bad --since '{s}' (use e.g. 30m, 2h, 1d)")
    return f"now-{s}"


def build_query(args) -> dict:
    filters = [{"range": {"@timestamp": {"gte": parse_since(args.since)}}}]

    if args.level:
        lvl = args.level.lower()
        lvl = "warn" if lvl == "warning" else lvl
        try:
            idx = LEVEL_ORDER.index(lvl)
        except ValueError:
            raise SystemExit(f"bad --level '{args.level}'")
        allowed = LEVEL_ORDER[idx:] + (["warning"] if idx <= LEVEL_ORDER.index("warn") else [])
        filters.append({"terms": {"log.level": allowed}})

    if args.service:
        filters.append({"term": {"service.name": args.service}})
    if args.logger:
        filters.append({"term": {"log.logger": args.logger}})
    if args.errors:
        filters.append({"exists": {"field": "error.type"}})
    if args.has_mqtt:
        filters.append({"exists": {"field": "labels.mqtt.payload"}})

    must = []
    if args.grep:
        must.append({"match": {"message": args.grep}})

    return {"bool": {"filter": filters, "must": must}}


def search(url: str, key: str, index: str, body: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/{index}/_search",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"ApiKey {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"ES HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except Exception as e:
        sys.exit(f"ES request failed: {type(e).__name__}: {e}")


def dig(src: dict, dotted: str):
    """Read a value that may be stored flat ('a.b') or nested ({'a':{'b':..}})."""
    if dotted in src:
        return src[dotted]
    cur = src
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def run_stats(url, key, index, query):
    body = {
        "size": 0,
        "query": query,
        "aggs": {
            "by_level": {"terms": {"field": "log.level", "size": 20}},
            "by_service": {"terms": {"field": "service.name", "size": 20}},
            "by_logger": {"terms": {"field": "log.logger", "size": 30}},
            "by_error": {"terms": {"field": "error.type", "size": 20}},
        },
    }
    res = search(url, key, index, body)
    total = res.get("hits", {}).get("total", {}).get("value", 0)
    aggs = res.get("aggs", res.get("aggregations", {}))
    print(f"total matching: {total}\n")
    for title, agg in (
        ("by level", "by_level"),
        ("by service", "by_service"),
        ("by logger", "by_logger"),
        ("by error.type", "by_error"),
    ):
        buckets = aggs.get(agg, {}).get("buckets", [])
        if not buckets:
            continue
        print(f"# {title}")
        for b in buckets:
            print(f"  {b['doc_count']:>6}  {b['key']}")
        print()


def run_list(url, key, index, query, size, oldest_first, show_mqtt, json_out):
    body = {
        "size": size,
        "sort": [{"@timestamp": "asc" if oldest_first else "desc"}],
        "query": query,
    }
    res = search(url, key, index, body)
    hits = res.get("hits", {}).get("hits", [])
    total = res.get("hits", {}).get("total", {}).get("value", 0)

    if json_out:
        print(json.dumps([h["_source"] for h in hits], indent=2, default=str))
        return

    if not hits:
        print("(no matching log records)")
        return

    rows = hits if oldest_first else list(reversed(hits))  # print oldest->newest
    for h in rows:
        s = h["_source"]
        ts = (dig(s, "@timestamp") or "")[:23]
        lvl = (dig(s, "log.level") or "?").upper()
        logger = dig(s, "log.logger") or "?"
        svc = dig(s, "service.name") or "?"
        msg = dig(s, "message") or ""
        print(f"{ts}  {lvl:<5} [{svc}/{logger}] {msg}")
        etype = dig(s, "error.type")
        if etype:
            print(f"             ↳ {etype}: {dig(s, 'error.message')}")
            if show_mqtt:
                trace = dig(s, "error.stack_trace")
                if trace:
                    for ln in str(trace).splitlines():
                        print(f"               {ln}")
        if show_mqtt:
            payload = dig(s, "labels.mqtt.payload")
            if payload is not None:
                topic = dig(s, "labels.mqtt.topic")
                direction = dig(s, "labels.mqtt.direction")
                print(f"             ↳ mqtt[{direction} {topic}]: {json.dumps(payload, default=str)}")
    print(f"\n— showing {len(hits)} of {total} matching (newest at bottom) —")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="1h", help="lookback window: 30m, 2h, 1d (default 1h)")
    ap.add_argument("--level", help="minimum level: debug|info|warn|error")
    ap.add_argument("--service", help="exact service.name (e.g. tiktok-agent, tiktok-watch-all)")
    ap.add_argument("--logger", help="exact log.logger (e.g. tiktok_poster, comment_agent)")
    ap.add_argument("--grep", help="full-text match on the message field")
    ap.add_argument("--errors", action="store_true", help="only records carrying an error.type")
    ap.add_argument("--mqtt", action="store_true", help="show full MQTT payloads + stack traces in output")
    ap.add_argument("--has-mqtt", action="store_true", help="filter to only records carrying an MQTT payload")
    ap.add_argument("--stats", action="store_true", help="aggregate counts instead of listing")
    ap.add_argument("-n", "--size", type=int, default=50, help="max records to list (default 50)")
    ap.add_argument("--oldest-first", action="store_true", help="sort ascending")
    ap.add_argument("--json", action="store_true", help="raw JSON sources")
    args = ap.parse_args()

    load_env()
    url = os.environ.get("ES_LOG_URL")
    key = os.environ.get("ES_API_KEY")
    index = os.environ.get("ES_LOG_INDEX", "logs-tiktok_agent-default")
    if not url or not key:
        sys.exit("ES_LOG_URL / ES_API_KEY not set (.env or env).")

    query = build_query(args)
    if args.stats:
        run_stats(url, key, index, query)
    else:
        run_list(url, key, index, query, args.size, args.oldest_first, args.mqtt, args.json)


if __name__ == "__main__":
    main()
