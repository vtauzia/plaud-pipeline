#!/usr/bin/env python3
"""
Plaud.ai Audio Recording Downloader
-------------------------------------
Downloads all audio recordings from your Plaud account using a bearer token.

SETUP:
    pip install requests tqdm

HOW TO GET YOUR BEARER TOKEN:
    1. Open your browser's DevTools (F12) and go to the Network tab
    2. Log in to app.plaud.ai (or the Plaud web app)
    3. Filter requests and find one going to api.plaud.ai with an "Authorization" header
    4. Copy the token after "Bearer " — it's a long alphanumeric string
    5. Paste it into BEARER_TOKEN below (or set the PLAUD_TOKEN env variable)

REGIONAL ACCOUNTS:
    EU users may need:  --base-url https://api-euc1.plaud.ai
    APAC users may need: --base-url https://api-apse1.plaud.ai

USAGE:
    python copy_plaud_recording.py --token YOUR_TOKEN
    python copy_plaud_recording.py  # reads PLAUD_TOKEN env variable
"""

import os
import sys
import json
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone

# ──────────────────────────────────────────────
# CONFIGURATION — edit these or pass via CLI
# ──────────────────────────────────────────────
BEARER_TOKEN = os.environ.get("PLAUD_TOKEN", "YOUR_BEARER_TOKEN_HERE")
OUTPUT_DIR   = "recordings"
BASE_URL     = "https://api.plaud.ai"

PAGE_SIZE    = 50    # Max recordings per list request
PREFER_OPUS  = True  # Prefer smaller OPUS format over original M4A when available


# ──────────────────────────────────────────────
# HTTP SESSION
# ──────────────────────────────────────────────
def make_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "User-Agent":    "plaud-downloader/1.0",
    })
    return session


def api_get(session: requests.Session, url: str, params: dict = None,
            retries: int = 3, stream: bool = False) -> requests.Response:
    """GET with exponential backoff on 429 / 5xx / network errors."""
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, timeout=60, stream=stream)
            if resp.status_code == 401:
                print("\n[ERROR] 401 Unauthorized — bearer token is invalid or expired.")
                print("        Refresh it from your browser DevTools and try again.\n")
                sys.exit(1)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", delay))
                print(f"  [RATE LIMIT] Waiting {wait:.0f}s before retry...")
                time.sleep(wait)
                delay *= 2
                continue
            if resp.status_code >= 500 and attempt < retries:
                print(f"  [SERVER ERROR {resp.status_code}] Retrying in {delay:.0f}s...")
                time.sleep(delay)
                delay *= 2
                continue
            return resp
        except requests.ConnectionError as e:
            if attempt < retries:
                print(f"  [NETWORK ERROR] {e}. Retrying in {delay:.0f}s...")
                time.sleep(delay)
                delay *= 2
            else:
                raise
    return resp  # last attempt


# ──────────────────────────────────────────────
# API HELPERS
# ──────────────────────────────────────────────
def fetch_recordings_page(session: requests.Session, base_url: str,
                           skip: int = 0, limit: int = PAGE_SIZE) -> dict:
    """
    Fetch one page of recording metadata.

    Actual Plaud API endpoint:
      GET /file/simple/web
      Query params: skip, limit, is_trash, sort_by, is_desc
    """
    url = base_url + "/file/simple/web"
    params = {
        "skip":     skip,
        "limit":    limit,
        "is_trash": 0,
        "sort_by":  "edit_time",
        "is_desc":  "true",
    }
    resp = api_get(session, url, params=params)
    resp.raise_for_status()
    return resp.json()


def collect_all_recordings(session: requests.Session, base_url: str) -> list[dict]:
    """
    Paginate through all recordings and return the full list.

    Response shape:
      {
        "status": 0,
        "data_file_total": 150,
        "data_file_list": [ { ...recording... }, ... ]
      }
    """
    all_recordings: list[dict] = []
    skip = 0
    total = None

    while True:
        print(f"  Fetching recordings {skip + 1}–{skip + PAGE_SIZE}...", end=" ", flush=True)
        try:
            data = fetch_recordings_page(session, base_url, skip=skip, limit=PAGE_SIZE)
        except requests.HTTPError as e:
            print(f"\n[ERROR] {e}")
            break

        if data.get("status", 0) != 0:
            print(f"\n[API ERROR] status={data.get('status')} msg={data.get('msg')}")
            break

        items: list[dict] = data.get("data_file_list") or []
        if total is None:
            total = data.get("data_file_total", "?")

        if not items:
            print("done.")
            break

        all_recordings.extend(items)
        print(f"got {len(items)} (total so far: {len(all_recordings)} / {total})")

        # Exit when we've received fewer than a full page — no more pages remain
        if len(items) < PAGE_SIZE:
            break

        skip += len(items)
        time.sleep(0.3)  # be polite

    return all_recordings


def get_temp_url(session: requests.Session, base_url: str,
                 file_id: str, prefer_opus: bool = True) -> str | None:
    """
    Fetch a time-limited pre-signed download URL for a recording.

    Actual Plaud API endpoint:
      GET /file/temp-url/{fileId}?is_opus=1

    Response shape:
      {
        "status": 0,
        "temp_url": "https://s3.../file.m4a?sig=...",
        "temp_url_opus": "https://s3.../file.opus?sig=..."
      }

    The pre-signed S3 URL does NOT require an Authorization header.
    """
    url = f"{base_url}/file/temp-url/{file_id}"
    params = {"is_opus": 1 if prefer_opus else 0}
    resp = api_get(session, url, params=params)

    if resp.status_code == 404:
        return None

    resp.raise_for_status()
    body = resp.json()

    if body.get("status", 0) != 0:
        return None

    # Prefer OPUS (smaller) when available, fall back to original
    if prefer_opus:
        return body.get("temp_url_opus") or body.get("temp_url")
    return body.get("temp_url")


# ──────────────────────────────────────────────
# FILENAME HELPERS
# ──────────────────────────────────────────────
def sanitize_filename(name: str) -> str:
    """Remove characters that are unsafe for filenames."""
    keepchars = " ._-()[]"
    return "".join(c if (c.isalnum() or c in keepchars) else "_" for c in name).strip()


def build_filename(recording: dict, index: int) -> tuple[str, str]:
    """
    Return (filename_stem, extension) for a recording.

    Actual Plaud API field names:
      - filename   : display name set by the user
      - start_time : Unix timestamp in MILLISECONDS
      - filetype   : e.g. "m4a"
      - id         : unique recording ID
    """
    title = recording.get("filename") or f"recording_{index:04d}"

    # start_time is in milliseconds
    date_str = ""
    raw_ts = recording.get("start_time")
    if raw_ts:
        try:
            dt = datetime.fromtimestamp(int(raw_ts) / 1000, tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            pass

    rec_id = str(recording.get("id", ""))[:8]
    parts = [p for p in [date_str, sanitize_filename(title), rec_id] if p]
    stem = "_".join(parts)

    # Extension from the filetype field; fall back to m4a (Plaud's native format)
    ext = "." + (recording.get("filetype") or "m4a").lstrip(".")

    return stem, ext


# ──────────────────────────────────────────────
# DOWNLOAD LOGIC
# ──────────────────────────────────────────────
def download_file(url: str, dest_path: Path) -> bool:
    """
    Stream a pre-signed S3 URL to disk.
    No Authorization header needed — the URL is already signed.
    Returns True on success.
    """
    try:
        # Plain requests.get — no session/auth headers needed for S3 pre-signed URLs
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()

            # Honour Content-Type if dest has no extension yet
            if not dest_path.suffix or dest_path.suffix == ".audio":
                ct = resp.headers.get("Content-Type", "")
                ext_map = {
                    "audio/mpeg":   ".mp3",
                    "audio/mp4":    ".m4a",
                    "audio/wav":    ".wav",
                    "audio/ogg":    ".ogg",
                    "audio/opus":   ".opus",
                    "audio/aac":    ".aac",
                    "audio/webm":   ".webm",
                    "audio/flac":   ".flac",
                }
                ext = ext_map.get(ct.split(";")[0].strip(), ".audio")
                dest_path = dest_path.with_suffix(ext)

            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0

            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

            if total and downloaded < total * 0.99:
                print(f"  [WARN] Incomplete download ({downloaded}/{total} bytes)")
                return False

            return True

    except requests.RequestException as e:
        print(f"  [ERROR] Download failed: {e}")
        return False


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Download audio recordings from your Plaud account."
    )
    parser.add_argument(
        "--token", "-t",
        default=BEARER_TOKEN,
        help="Plaud bearer token (overrides PLAUD_TOKEN env var)"
    )
    parser.add_argument(
        "--output", "-o",
        default=OUTPUT_DIR,
        help=f"Output directory (default: {OUTPUT_DIR})"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List recordings without downloading"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of recordings to process (default: all)"
    )
    parser.add_argument(
        "--base-url",
        default=BASE_URL,
        help=(
            f"Plaud API base URL (default: {BASE_URL}). "
            "EU accounts: https://api-euc1.plaud.ai  "
            "APAC accounts: https://api-apse1.plaud.ai"
        )
    )
    parser.add_argument(
        "--no-opus",
        action="store_true",
        help="Download original format instead of OPUS (larger files)"
    )
    args = parser.parse_args()

    token = args.token
    if not token or token == "YOUR_BEARER_TOKEN_HERE":
        print("[ERROR] No bearer token provided.")
        print("        Set PLAUD_TOKEN env variable or use --token <your_token>")
        sys.exit(1)

    base_url    = args.base_url.rstrip("/")
    output_dir  = Path(args.output)
    prefer_opus = not args.no_opus
    output_dir.mkdir(parents=True, exist_ok=True)

    session = make_session(token)

    print(f"\n Plaud Audio Downloader")
    print(f"{'─'*40}")
    print(f"  Output dir : {output_dir.resolve()}")
    print(f"  API base   : {base_url}")
    print(f"  Format     : {'OPUS (smaller)' if prefer_opus else 'Original'}")
    print(f"  Dry run    : {args.dry_run}\n")

    # ── 1. Collect all recording metadata ────────────────────────────────────
    print("Fetching recording list...")
    recordings = collect_all_recordings(session, base_url)

    if not recordings:
        print("\nNo recordings found. Dumping raw API response for diagnosis:")
        try:
            raw = api_get(session, base_url + "/file/simple/web",
                          params={"skip": 0, "limit": 5, "is_trash": 0})
            print(json.dumps(raw.json(), indent=2, ensure_ascii=False)[:3000])
        except Exception as e:
            print(f"  (could not fetch raw response: {e})")
        sys.exit(0)

    if args.limit:
        recordings = recordings[: args.limit]

    print(f"\nFound {len(recordings)} recording(s) to process.\n")

    # ── 2. Download each recording ────────────────────────────────────────────
    success, skipped, failed = 0, 0, 0

    for i, rec in enumerate(recordings, start=1):
        stem, ext = build_filename(rec, i)
        file_id   = rec.get("id", "")
        dest      = output_dir / f"{stem}{ext}"

        print(f"[{i:>3}/{len(recordings)}] {stem}{ext}")

        if args.dry_run:
            print(f"         id   : {file_id}")
            print(f"         dest : {dest}")
            skipped += 1
            continue

        if dest.exists():
            print("         [SKIP] Already downloaded.")
            skipped += 1
            continue

        if not file_id:
            print("         [SKIP] No file ID in metadata — cannot fetch download URL.")
            skipped += 1
            continue

        # Step 1: get a fresh pre-signed S3 URL (these expire quickly)
        audio_url = get_temp_url(session, base_url, file_id, prefer_opus=prefer_opus)
        if not audio_url:
            print("         [SKIP] Could not obtain a download URL.")
            skipped += 1
            continue

        # Step 2: download from S3 (no auth header needed)
        ok = download_file(audio_url, dest)
        if ok:
            size_kb = dest.stat().st_size // 1024
            print(f"         Saved {size_kb:,} KB → {dest.name}")
            success += 1
        else:
            failed += 1

        time.sleep(0.2)  # brief pause between recordings

    # ── 3. Summary ────────────────────────────────────────────────────────────
    print(f"\n{'─'*40}")
    print(f"  Downloaded : {success}")
    print(f"  Skipped    : {skipped}")
    print(f"  Failed     : {failed}")
    print(f"  Output dir : {output_dir.resolve()}\n")

    # ── 4. Save metadata JSON for reference ───────────────────────────────────
    metadata_path = output_dir / "_recordings_metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(recordings, f, indent=2, ensure_ascii=False, default=str)
    print(f"  Metadata saved to {metadata_path}\n")


if __name__ == "__main__":
    main()