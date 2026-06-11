---
name: check-es-logs
description: Query the tiktok-agent logs shipped to Elasticsearch Serverless. Use to check recent logs, investigate failures/errors/warnings, inspect MQTT payloads, or get log stats for the posting/commenting/reading agents. Reads ES_LOG_URL / ES_API_KEY from the project .env.
---

# Check ES logs

Every tiktok-agent CLI ships its logs to Elasticsearch Serverless (see CLAUDE.md
"Logging"). This skill queries that data stream. Auth + endpoint come from the
project `.env` (`ES_LOG_URL`, `ES_API_KEY`, `ES_LOG_INDEX`); the helper loads them
itself, so run it from the repo root.

## Usage

Run the bundled helper (stdlib only, no deps):

```bash
python3 .claude/skills/check-es-logs/es_logs.py [options]
```

Common investigations:

```bash
# Recent activity (default: last 1h, newest at bottom)
python3 .claude/skills/check-es-logs/es_logs.py --since 1h

# Why did it fail? — warnings + errors in the last few hours
python3 .claude/skills/check-es-logs/es_logs.py --since 3h --level warn

# Only records carrying an exception, with full stack traces + MQTT payload
python3 .claude/skills/check-es-logs/es_logs.py --errors --since 24h --mqtt

# Find a specific outcome / message
python3 .claude/skills/check-es-logs/es_logs.py --grep needs_manual --since 24h
python3 .claude/skills/check-es-logs/es_logs.py --grep wrong_account --since 24h

# Scope to one CLI or module
python3 .claude/skills/check-es-logs/es_logs.py --service tiktok-commenter --since 6h
python3 .claude/skills/check-es-logs/es_logs.py --logger tiktok_poster --since 6h

# Aggregate counts (by level / service / logger / error.type) instead of a list
python3 .claude/skills/check-es-logs/es_logs.py --stats --since 6h
```

## Options

- `--since 30m|2h|1d` — lookback window (default `1h`; bare number = hours).
- `--level debug|info|warn|error` — minimum level (inclusive; `warn` includes `error`).
- `--service NAME` — exact `service.name` (`tiktok-agent`, `tiktok-watch-all`, `tiktok-commenter`, `tiktok-comment-reader`).
- `--logger NAME` — exact `log.logger` (the Python module, e.g. `tiktok_poster`, `comment_agent`).
- `--grep TEXT` — full-text match on `message`.
- `--errors` — only records with an `error.type`.
- `--mqtt` — show full MQTT payloads (`labels.mqtt.payload`) and stack traces.
- `--stats` — aggregate counts instead of listing rows.
- `-n/--size N` — max rows (default 50). `--oldest-first`, `--json` also available.

## Notes

- Default index is `logs-tiktok_agent-default` (override via `ES_LOG_INDEX`).
- Document fields: `@timestamp`, `log.level`, `log.logger`, `message`,
  `service.name`, `host.name`, `process.pid`, `error.{type,message,stack_trace}`,
  and `labels.{...}` (per-flow context: post `id`, `PostURL`, device serial, and
  for MQTT records `mqtt.payload`/`mqtt.topic`/`mqtt.direction`).
- Read-only: this only queries ES, never writes or deletes.
