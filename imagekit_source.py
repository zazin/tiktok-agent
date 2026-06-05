#!/usr/bin/env python3
"""
ImageKit access — image download (both sources) + the legacy folder queue.

`download()` is used by both sources to fetch an image from its public ImageKit
CDN URL (no auth). `list_images()` powers the legacy `--source imagekit` queue:
it lists an ImageKit folder via the Media Management API so the tiktok-agent can
discover images the tiktok-pipeline uploaded. The default source of truth is now
Airtable (see airtable_source.py); this folder listing is the legacy fallback.

Auth uses the SAME scheme as the pipeline's uploader: HTTP Basic with the
private key as username and an empty password.

Credentials (read from the environment / .env):
  - IMAGEKIT_PRIVATE_KEY

Usage (CLI):
    python imagekit_source.py --folder /tiktok
    python imagekit_source.py --folder /tiktok --download ./downloads

Usage (as a module):
    from imagekit_source import list_images, download
    files = list_images(folder="/tiktok")
    download(files[0]["url"], "out.jpg")
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


IMAGEKIT_LIST_URL = "https://api.imagekit.io/v1/files"


class ImageKitSourceError(Exception):
    """Raised when listing or downloading from ImageKit fails."""


def _get_private_key() -> str:
    key = os.getenv("IMAGEKIT_PRIVATE_KEY")
    if not key:
        raise ImageKitSourceError(
            "IMAGEKIT_PRIVATE_KEY env var is not set. "
            "Export it (or put it in .env) before running the agent."
        )
    return key


def _auth_header(private_key: str) -> dict:
    # ImageKit Basic auth: private key as username, empty password.
    token = base64.b64encode(f"{private_key}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def list_images(
    *,
    folder: str = "/tiktok",
    limit: int = 100,
    sort: str = "DESC_CREATED",
    timeout: int = 60,
) -> list[dict]:
    """
    List image files in an ImageKit folder, newest first by default.

    Args:
        folder: ImageKit folder path (e.g. "/tiktok").
        limit: Max files to return (ImageKit caps a single page at 1000).
        sort: ImageKit sort key (e.g. DESC_CREATED, ASC_CREATED).
        timeout: HTTP timeout in seconds.

    Returns:
        A list of file dicts (fileId, name, url, createdAt, tags,
        customMetadata, ...). Only items with fileType == "image" are returned.

    Raises:
        ImageKitSourceError: On any failure.
    """
    params = {
        "path": folder,
        "limit": str(limit),
        "sort": sort,
        "fileType": "image",
    }
    url = f"{IMAGEKIT_LIST_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=_auth_header(_get_private_key()), method="GET")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        raise ImageKitSourceError(f"HTTP {e.code} listing {folder}: {err_body}") from e
    except urllib.error.URLError as e:
        raise ImageKitSourceError(f"Network error listing {folder}: {e}") from e
    except json.JSONDecodeError as e:
        raise ImageKitSourceError(f"Invalid JSON from ImageKit: {e}") from e

    if not isinstance(data, list):
        raise ImageKitSourceError(f"Unexpected list response shape: {str(data)[:200]}")
    # The folder listing already filters by fileType, but be defensive.
    return [f for f in data if isinstance(f, dict) and f.get("type") != "folder"]


def download(url: str, dest: str | os.PathLike, *, timeout: int = 120) -> Path:
    """
    Download a file from a URL to a local path. Returns the local Path.

    Raises:
        ImageKitSourceError: On any failure.
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            blob = r.read()
    except urllib.error.URLError as e:
        raise ImageKitSourceError(f"Failed to download {url}: {e}") from e
    dest_path.write_bytes(blob)
    return dest_path


def _cli() -> int:
    from env_loader import load_env
    load_env()

    parser = argparse.ArgumentParser(description="List/download images from an ImageKit folder.")
    parser.add_argument("--folder", default="/tiktok", help="ImageKit folder (default: /tiktok)")
    parser.add_argument("--limit", type=int, default=100, help="Max files to list (default: 100)")
    parser.add_argument("--download", metavar="DIR", default=None, help="Download all listed files into DIR")
    parser.add_argument("--json", action="store_true", help="Print full JSON instead of a summary")
    args = parser.parse_args()

    try:
        files = list_images(folder=args.folder, limit=args.limit)
    except ImageKitSourceError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(files, indent=2))
    else:
        print(f"{len(files)} image(s) in {args.folder}:")
        for f in files:
            print(f"  {f.get('fileId')}  {f.get('name')}  {f.get('url')}")

    if args.download:
        for f in files:
            try:
                out = download(f["url"], Path(args.download) / f["name"])
                print(f"Downloaded: {out}")
            except (ImageKitSourceError, KeyError) as e:
                print(f"Failed to download {f.get('name')}: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
