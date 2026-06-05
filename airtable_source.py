#!/usr/bin/env python3
"""
Airtable as the work queue — the source of truth.

The tiktok-pipeline writes one record per generated post to an Airtable table
(default name "Posts") with Status = "pending", carrying the caption, description,
and the public ImageKit image URL. This module reads those pending records (the
queue) and flips their Status to "posted"/"failed" after the agent acts on them.

See the schema reference: tiktok-pipeline/docs/airtable.md.

Auth is the standard Airtable REST API: a Bearer personal access token (PAT).

Credentials (read from the environment / .env):
  - AIRTABLE_API_KEY      Personal access token (needs data.records:read+write) — required
  - AIRTABLE_BASE_ID      Base id, starts with "app..." (default DEFAULT_BASE_ID)
  - AIRTABLE_TABLE_NAME   Table name or id (default "Posts")

Usage (CLI):
    python airtable_source.py
    python airtable_source.py --json

Usage (as a module):
    from airtable_source import list_pending, update_status
    for rec in list_pending():
        ...
        update_status(rec["id"], "posted")
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


AIRTABLE_API_URL = "https://api.airtable.com/v0"
DEFAULT_BASE_ID = "appYyBCWQlkLLMAX4"
DEFAULT_TABLE = "Posts"
PENDING = "pending"


class AirtableSourceError(Exception):
    """Raised when reading from or writing to Airtable fails."""


def _get_config(table: Optional[str] = None) -> tuple[str, str, str]:
    """Return (api_key, base_id, table). Raises if required vars are missing."""
    api_key = os.getenv("AIRTABLE_API_KEY")
    if not api_key:
        raise AirtableSourceError(
            "AIRTABLE_API_KEY env var is not set. "
            "Export it (or put it in .env) before running the agent."
        )
    base_id = os.getenv("AIRTABLE_BASE_ID") or DEFAULT_BASE_ID
    table = table or os.getenv("AIRTABLE_TABLE_NAME") or DEFAULT_TABLE
    return api_key, base_id, table


def _auth_header(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _table_url(base_id: str, table: str) -> str:
    # Path segments may contain spaces (e.g. a table named "My Posts").
    return f"{AIRTABLE_API_URL}/{base_id}/{urllib.parse.quote(table)}"


def list_pending(
    *,
    table: Optional[str] = None,
    page_size: int = 100,
    timeout: int = 60,
) -> list[dict]:
    """
    List records with Status == "pending", oldest first (by CreatedAt).

    Handles pagination transparently (follows the API's `offset` cursor).

    Returns:
        A list of record dicts, each {id, createdTime, fields: {...}}.

    Raises:
        AirtableSourceError: On any failure.
    """
    api_key, base_id, table = _get_config(table)
    base_url = _table_url(base_id, table)
    headers = _auth_header(api_key)

    records: list[dict] = []
    offset: Optional[str] = None
    while True:
        params = [
            ("filterByFormula", f'{{Status}}="{PENDING}"'),
            ("sort[0][field]", "CreatedAt"),
            ("sort[0][direction]", "asc"),
            ("pageSize", str(page_size)),
        ]
        if offset:
            params.append(("offset", offset))
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise AirtableSourceError(f"HTTP {e.code} listing {table}: {err_body}") from e
        except urllib.error.URLError as e:
            raise AirtableSourceError(f"Network error listing {table}: {e}") from e
        except json.JSONDecodeError as e:
            raise AirtableSourceError(f"Invalid JSON from Airtable: {e}") from e

        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise AirtableSourceError(f"Unexpected list response shape: {str(data)[:200]}")

        records.extend(r for r in data["records"] if isinstance(r, dict))
        offset = data.get("offset")
        if not offset:
            break

    return records


def update_status(
    record_id: str,
    status: str,
    *,
    table: Optional[str] = None,
    timeout: int = 60,
) -> None:
    """
    Set a record's Status field (e.g. "posted" or "failed").

    Raises:
        AirtableSourceError: On any failure.
    """
    api_key, base_id, table = _get_config(table)
    url = f"{_table_url(base_id, table)}/{urllib.parse.quote(record_id)}"
    body = json.dumps({"fields": {"Status": status}, "typecast": True}).encode()
    headers = {**_auth_header(api_key), "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method="PATCH")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        raise AirtableSourceError(
            f"HTTP {e.code} updating {record_id} -> {status}: {err_body}"
        ) from e
    except urllib.error.URLError as e:
        raise AirtableSourceError(f"Network error updating {record_id}: {e}") from e


def _cli() -> int:
    from env_loader import load_env
    load_env()

    parser = argparse.ArgumentParser(description="Inspect the Airtable 'Posts' queue (pending rows).")
    parser.add_argument("--table", default=None, help="Table name or id (default: env AIRTABLE_TABLE_NAME or 'Posts')")
    parser.add_argument("--json", action="store_true", help="Print full JSON instead of a summary")
    args = parser.parse_args()

    try:
        records = list_pending(table=args.table)
    except AirtableSourceError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(records, indent=2))
    else:
        print(f"{len(records)} pending record(s):")
        for rec in records:
            f = rec.get("fields", {})
            idea = f.get("Idea") or f.get("Caption") or ""
            print(f"  {rec.get('id')}  {f.get('CreatedAt', '')}  {idea}  -> {f.get('ImageURL', '')}")

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
