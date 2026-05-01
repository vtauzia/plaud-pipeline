#!/usr/bin/env python3
"""
Plaud Recording Cleanup
------------------------
Deletes audio recordings that have been fully processed through the pipeline —
meaning a transcript, a summary, AND a .docx export all exist for the file.

Deletion happens in two places:
  1. Plaud cloud storage  (via the Plaud API — DELETE /file/{fileId})
  2. Local /recordings folder

The Plaud file ID needed for the cloud delete is looked up from the
_recordings_metadata.json file written by copy_plaud_recording.py.

SAFETY GUARDS (all active by default):
  - Only deletes files that have transcript + summary + docx (fully processed)
  - Only deletes files older than --min-age days (default: 7)
  - --dry-run mode shows what would be deleted without touching anything
  - --skip-cloud  skips the Plaud API delete (local delete only)
  - --skip-local  skips the local file delete (cloud delete only)

SETUP:
    pip install requests

USAGE:
    python3 delete_processed_recordings.py --dry-run   ← always start here
    python3 delete_processed_recordings.py
    python3 delete_processed_recordings.py --min-age 14 --skip-cloud
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
BEARER_TOKEN    = os.environ.get("PLAUD_TOKEN", "YOUR_PLAUD_TOKEN_HERE")
BASE_URL        = "https://api.plaud.ai"
RECORDINGS_DIR  = "recordings"
TRANSCRIPTS_DIR = "transcripts"
SUMMARIES_DIR   = "summaries"
TRANSFER_OUT_DIR = "transfer_out"
METADATA_FILE   = "_recordings_metadata.json"   # written by copy_plaud_recording.py

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".opus", ".ogg", ".wav", ".flac", ".aac"}
DEFAULT_MIN_AGE_DAYS = 3    # never delete files newer than this


# ──────────────────────────────────────────────
# PIPELINE COMPLETION CHECK
# ──────────────────────────────────────────────
def is_fully_processed(audio_file: Path,
                        transcripts_dir: Path,
                        summaries_dir: Path,
                        transfer_out_dir: Path) -> tuple[bool, list[str]]:
    """
    Return (True, []) if all three downstream artefacts exist for this audio file,
    or (False, [list of what is missing]) otherwise.

    Matching is by stem:
      recordings/2026-04-23_Meeting_a7644555.m4a
        → transcripts/2026-04-23_Meeting_a7644555.json   (must exist, non-empty)
        → summaries/2026-04-23_Meeting_a7644555.md       (must exist, non-empty)
        → transfer_out/2026-04-23_Meeting_a7644555.docx  (must exist, non-empty)
    """
    stem    = audio_file.stem
    missing = []

    for path, label in [
        (transcripts_dir  / f"{stem}.json", "transcript"),
        (summaries_dir    / f"{stem}.md",   "summary"),
        (transfer_out_dir / f"{stem}.docx", "docx"),
    ]:
        if not path.exists() or path.stat().st_size == 0:
            missing.append(label)

    return (len(missing) == 0, missing)


def age_days(path: Path) -> float:
    """Return the age of a file in days (based on modification time)."""
    mtime = path.stat().st_mtime
    return (datetime.now(tz=timezone.utc).timestamp() - mtime) / 86400


# ──────────────────────────────────────────────
# PLAUD ID LOOKUP
# ──────────────────────────────────────────────
def load_id_map(recordings_dir: Path) -> dict[str, str]:
    """
    Load _recordings_metadata.json and return a dict of
    {short_id (8 chars): full_plaud_file_id}.

    The local filename encodes the first 8 characters of the Plaud file ID
    as its last underscore-separated segment (e.g. 'a7644555' in
    '2026-04-23_Meeting_a7644555.m4a'). This is how we match local files
    to their cloud counterpart without a separate database.
    """
    metadata_path = recordings_dir / METADATA_FILE
    if not metadata_path.exists():
        print(f"[WARN] {METADATA_FILE} not found in {recordings_dir}")
        print(f"       Cloud deletion will be skipped for files without a known ID.")
        return {}

    try:
        with open(metadata_path, encoding="utf-8") as f:
            records = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Could not read {METADATA_FILE}: {e}")
        return {}

    id_map: dict[str, str] = {}
    for rec in records:
        full_id = rec.get("id", "")
        if full_id:
            id_map[full_id[:8]] = full_id

    return id_map


def extract_short_id(audio_file: Path) -> str | None:
    """
    Extract the 8-character Plaud ID suffix from a filename.
    e.g. '2026-04-23_Meeting_a7644555.m4a' → 'a7644555'
    Returns None if the stem doesn't end with an 8-character segment.
    """
    parts = audio_file.stem.split("_")
    if parts:
        candidate = parts[-1]
        if len(candidate) == 8:
            return candidate
    return None


# ──────────────────────────────────────────────
# PLAUD API — DELETE
# ──────────────────────────────────────────────
def make_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "User-Agent":    "plaud-cleanup/1.0",
    })
    return session


def delete_from_plaud(session: requests.Session, base_url: str,
                      file_id: str) -> tuple[bool, str]:
    """
    Delete a recording from Plaud cloud storage.

    Plaud API endpoint:
      DELETE /file/{fileId}

    Response on success:
      {"status": 0, "msg": "..."}

    Returns (success: bool, message: str).
    """
    url = f"{base_url.rstrip('/')}/file/{file_id}"

    try:
        resp = session.delete(url, timeout=30)
    except requests.RequestException as e:
        return False, f"Network error: {e}"

    if resp.status_code == 401:
        return False, "401 Unauthorized — bearer token is invalid or expired"

    if resp.status_code == 404:
        return False, "404 Not found — already deleted from cloud?"

    # Plaud wraps errors in JSON even on non-2xx responses
    try:
        body = resp.json()
        api_status = body.get("status", -1)
        msg        = body.get("msg", "")

        if api_status == 0:
            return True, msg or "deleted"
        else:
            return False, f"API status {api_status}: {msg}"

    except ValueError:
        # Response was not JSON
        if resp.ok:
            return True, f"HTTP {resp.status_code}"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Delete fully-processed Plaud recordings from cloud and local storage."
    )
    parser.add_argument(
        "--recordings",
        default=RECORDINGS_DIR,
        help=f"Local recordings directory (default: {RECORDINGS_DIR})"
    )
    parser.add_argument(
        "--transcripts",
        default=TRANSCRIPTS_DIR,
        help=f"Transcripts directory (default: {TRANSCRIPTS_DIR})"
    )
    parser.add_argument(
        "--summaries",
        default=SUMMARIES_DIR,
        help=f"Summaries directory (default: {SUMMARIES_DIR})"
    )
    parser.add_argument(
        "--transfer-out",
        default=TRANSFER_OUT_DIR,
        help=f"Transfer-out directory (default: {TRANSFER_OUT_DIR})"
    )
    parser.add_argument(
        "--token", "-t",
        default=BEARER_TOKEN,
        help="Plaud bearer token (overrides PLAUD_TOKEN env var)"
    )
    parser.add_argument(
        "--base-url",
        default=BASE_URL,
        help=f"Plaud API base URL (default: {BASE_URL})"
    )
    parser.add_argument(
        "--min-age",
        type=int,
        default=DEFAULT_MIN_AGE_DAYS,
        metavar="DAYS",
        help=f"Only delete files older than this many days (default: {DEFAULT_MIN_AGE_DAYS})"
    )
    parser.add_argument(
        "--skip-cloud",
        action="store_true",
        help="Skip the Plaud cloud delete — only remove local files"
    )
    parser.add_argument(
        "--skip-local",
        action="store_true",
        help="Skip the local file delete — only remove from Plaud cloud"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting anything"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of files to delete in this run (default: all eligible)"
    )
    args = parser.parse_args()

    recordings_dir   = Path(args.recordings)
    transcripts_dir  = Path(args.transcripts)
    summaries_dir    = Path(args.summaries)
    transfer_out_dir = Path(args.transfer_out)
    token            = args.token

    # ── Validate ──────────────────────────────────────────────────────────
    if not recordings_dir.exists():
        print(f"[ERROR] Recordings directory not found: {recordings_dir}")
        sys.exit(1)

    if not args.skip_cloud:
        if not token or token == "YOUR_BEARER_TOKEN_HERE":
            print("[ERROR] No Plaud bearer token provided.")
            print("        Set PLAUD_TOKEN env var or use --token")
            sys.exit(1)

    # ── Collect all audio files ───────────────────────────────────────────
    all_audio = sorted(
        f for f in recordings_dir.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    )

    if not all_audio:
        print(f"No audio files found in {recordings_dir}")
        sys.exit(0)

    # ── Load Plaud ID map from metadata ───────────────────────────────────
    id_map = {} if args.skip_cloud else load_id_map(recordings_dir)

    # ── Classify each file ────────────────────────────────────────────────
    eligible    = []   # ready to delete
    not_ready   = []   # pipeline not yet complete
    too_new     = []   # younger than --min-age

    for audio_file in all_audio:
        file_age = age_days(audio_file)
        if file_age < args.min_age:
            too_new.append((audio_file, file_age))
            continue

        complete, missing = is_fully_processed(
            audio_file, transcripts_dir, summaries_dir, transfer_out_dir
        )
        if complete:
            eligible.append(audio_file)
        else:
            not_ready.append((audio_file, missing))

    print(f"\n Plaud Recording Cleanup")
    print(f"{'─'*44}")
    print(f"  Recordings dir  : {recordings_dir.resolve()}")
    print(f"  Min age         : {args.min_age} days")
    print(f"  Delete from cloud: {'no (--skip-cloud)' if args.skip_cloud else 'yes'}")
    print(f"  Delete locally  : {'no (--skip-local)' if args.skip_local else 'yes'}")
    print(f"  Dry run         : {args.dry_run}")
    print(f"")
    print(f"  Total audio files : {len(all_audio)}")
    print(f"  Too new (<{args.min_age}d)   : {len(too_new)}")
    print(f"  Pipeline incomplete: {len(not_ready)}")
    print(f"  Ready to delete : {len(eligible)}")

    if not_ready:
        print(f"\n  Files held back (pipeline incomplete):")
        for f, missing in not_ready:
            print(f"    {f.name}  — missing: {', '.join(missing)}")

    if not eligible:
        print(f"\nNothing eligible for deletion.")
        sys.exit(0)

    if args.limit:
        eligible = eligible[: args.limit]
        print(f"\n  (processing first {args.limit} eligible files)")

    print()

    # ── Set up Plaud session ──────────────────────────────────────────────
    session = None if args.skip_cloud else make_session(token)

    # ── Delete ────────────────────────────────────────────────────────────
    cloud_ok = cloud_skip = cloud_fail = 0
    local_ok = local_skip = local_fail = 0

    for i, audio_file in enumerate(eligible, start=1):
        file_age    = age_days(audio_file)
        short_id    = extract_short_id(audio_file)
        full_id     = id_map.get(short_id) if short_id else None

        print(f"[{i:>3}/{len(eligible)}] {audio_file.name}  (age: {file_age:.0f}d)")

        # ── Cloud delete ───────────────────────────────────────────────
        if args.skip_cloud:
            print(f"         cloud  : skipped (--skip-cloud)")
            cloud_skip += 1
        elif not full_id:
            print(f"         cloud  : skipped — Plaud file ID not found in metadata")
            print(f"                  (run copy_plaud_recording.py first to refresh metadata)")
            cloud_skip += 1
        elif args.dry_run:
            print(f"         cloud  : would delete Plaud ID {full_id}  [dry run]")
            cloud_skip += 1
        else:
            ok, msg = delete_from_plaud(session, args.base_url, full_id)
            if ok:
                print(f"         cloud  : deleted (ID: {full_id})")
                cloud_ok += 1
            else:
                print(f"         cloud  : FAILED — {msg}")
                cloud_fail += 1

        # ── Local delete ───────────────────────────────────────────────
        if args.skip_local:
            print(f"         local  : skipped (--skip-local)")
            local_skip += 1
        elif args.dry_run:
            print(f"         local  : would delete {audio_file}  [dry run]")
            local_skip += 1
        else:
            try:
                audio_file.unlink()
                print(f"         local  : deleted")
                local_ok += 1
            except OSError as e:
                print(f"         local  : FAILED — {e}")
                local_fail += 1

        time.sleep(0.1)   # brief pause between API calls

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'─'*44}")
    if args.dry_run:
        print(f"  DRY RUN — nothing was actually deleted")
        print(f"  Files that would be deleted: {len(eligible)}")
    else:
        print(f"  Cloud deleted : {cloud_ok}  |  skipped: {cloud_skip}  |  failed: {cloud_fail}")
        print(f"  Local deleted : {local_ok}  |  skipped: {local_skip}  |  failed: {local_fail}")
    print()


if __name__ == "__main__":
    main()