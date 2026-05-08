#!/usr/bin/env python3
"""
Plaud.ai Audio Recording Downloader (v2)
========================================
Downloads all audio recordings from your Plaud account using a bearer token.

SETUP:
    pip install requests
    # Optional, only needed for --format mp3 / wav:
    # Install ffmpeg  (apt install ffmpeg | brew install ffmpeg | choco install ffmpeg)

HOW TO GET YOUR BEARER TOKEN:
    1. Open your browser's DevTools (F12) and go to the Network tab
    2. Log in to app.plaud.ai (or web.plaud.ai)
    3. Find a request to api*.plaud.ai with an "Authorization" header
    4. Copy the value after "Bearer " — a long alphanumeric JWT
    5. Set it via --token, or export it as PLAUD_TOKEN

REGIONAL ACCOUNTS — handled automatically:
    The script now tries multiple PLAUD API hosts and follows the server's
    `status: -302` regional redirect. You usually don't need --base-url anymore.

USAGE:
    python copy_plaud_recording.py --token YOUR_TOKEN
    python copy_plaud_recording.py                       # uses PLAUD_TOKEN env var
    python copy_plaud_recording.py --format mp3          # transcode to MP3
                                                          # (fixes malformed M4As)
    python copy_plaud_recording.py --dry-run             # list, don't download

WHAT'S NEW vs v1:
    * Auto-discovers regional API host via PLAUD's `status: -302` redirect
    * Tries multiple PLAUD API origins (api.plaud.ai, api-euc1, api-apse1)
    * Defensive field-name lookup for IDs, titles, and temp URLs
    * Sends `edit-from: web` header (matches the official web client)
    * Pre-flights the token before doing any real work
    * --format mp3/wav: locally re-encode via ffmpeg to fix PLAUD's quirky MP4
    * Default source is now the original M4A — the old default (OPUS) saved
      OPUS bytes with a misleading .m4a extension, which broke many players.
"""

import os
import sys
import json
import time
import shutil
import argparse
import subprocess
import tempfile
import requests

from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Iterable
from urllib.parse import quote, urlparse


# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
BEARER_TOKEN = os.environ.get("PLAUD_TOKEN", "")
OUTPUT_DIR = "recordings"
DEFAULT_BASE_URL = "https://api.plaud.ai"

# Tried in order. The first one that returns a usable response wins; PLAUD's
# own -302 redirect can also inject the correct regional host at runtime.
CANDIDATE_API_ORIGINS = [
    "https://api.plaud.ai",
    "https://api-euc1.plaud.ai",
    "https://api-apse1.plaud.ai",
]

PAGE_SIZE = 50

# What --format can be set to:
#   original  - whatever PLAUD has (M4A from device)
#   opus      - server-side OPUS transcode (smaller, .opus)
#   mp3       - download original, locally re-encode via ffmpeg
#   wav       - download original, locally re-encode via ffmpeg
SUPPORTED_FORMATS = ("original", "opus", "mp3", "wav")
FORMATS_NEEDING_FFMPEG = ("mp3", "wav")


# ──────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────
def pick_first_nonempty(*values: object) -> str:
    """Return the first arg that's a non-empty string (trimmed)."""
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def normalize_origin(value: object) -> str:
    """'api.plaud.ai/foo/' or 'https://x/' -> 'https://x' (no trailing slash)."""
    if not value:
        return ""
    raw = str(value).strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def dedupe_origins(values: Iterable[object]) -> list[str]:
    """Normalize and dedupe (case-insensitive on host)."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        norm = normalize_origin(v)
        if not norm:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


# ──────────────────────────────────────────────
# CUSTOM ERRORS
# ──────────────────────────────────────────────
class PlaudAuthError(Exception):
    """401/403 — token is invalid or revoked. Don't bother trying other origins."""


class PlaudRoutingError(Exception):
    """All candidate API origins failed; message tells the user what to try next."""


# ──────────────────────────────────────────────
# HTTP SESSION
# ──────────────────────────────────────────────
def make_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        # PLAUD's web client sends this; some endpoints check for it.
        "edit-from":     "web",
        "User-Agent":    "plaud-downloader/2.0",
    })
    return session


def api_get(session: requests.Session, url: str, params: Optional[dict] = None,
            retries: int = 3, stream: bool = False) -> requests.Response:
    """GET with exponential backoff on 429 / 5xx / network errors.

    Does NOT exit on 401 — the caller decides whether that's terminal.
    """
    delay = 1.0
    last_resp: Optional[requests.Response] = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, timeout=60, stream=stream)
            last_resp = resp
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
    assert last_resp is not None  # at least one attempt was made
    return last_resp


# ──────────────────────────────────────────────
# REGIONAL API ROUTING
# ──────────────────────────────────────────────
def extract_redirect_origin(payload: dict) -> str:
    """Pull the regional API host out of a PLAUD `status: -302` response.

    Mirrors the JS `extractRedirectApiOrigin` field-priority order.
    """
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    domains = data.get("domains") if isinstance(data.get("domains"), dict) else {}
    domain = data.get("domain") if isinstance(data.get("domain"), dict) else {}
    payload_domains = (
        payload.get("domains") if isinstance(payload.get("domains"), dict) else {}
    )

    candidates = [
        domains.get("api"),
        payload_domains.get("api"),
        domain.get("api"),
        data.get("api_origin"),
        data.get("apiOrigin"),
        payload.get("api_origin"),
        payload.get("apiOrigin"),
    ]
    for c in candidates:
        n = normalize_origin(c)
        if n:
            return n
    return ""


def call_plaud_api(session: requests.Session, origins: list[str], path: str,
                   params: Optional[dict] = None, retries: int = 3
                   ) -> tuple[dict, str]:
    """Try each origin in turn. Follows PLAUD's `status: -302` redirect once.

    Returns (parsed_body, working_origin) on success.
    Raises PlaudAuthError on 401/403 (no point trying other origins).
    Raises PlaudRoutingError if all origins exhausted with no success.
    """
    errors: list[tuple[str, str]] = []
    queue = list(dedupe_origins(origins))
    tried: set[str] = set()
    redirect_followed = False
    last_minus_302_payload: Optional[dict] = None

    while queue:
        origin = queue.pop(0)
        if origin in tried:
            continue
        tried.add(origin)

        url = origin + path
        try:
            resp = api_get(session, url, params=params, retries=retries)

            if resp.status_code in (401, 403):
                # Auth failure — token bad for ALL regions.
                raise PlaudAuthError(
                    f"HTTP {resp.status_code} from {origin}: token rejected. "
                    "Refresh it from your browser DevTools and try again."
                )

            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError as e:
                errors.append((origin, f"non-JSON response: {e}"))
                continue

            # PLAUD's regional redirect mechanism.
            if isinstance(body, dict) and body.get("status") == -302:
                last_minus_302_payload = body
                if not redirect_followed:
                    redirect_origin = extract_redirect_origin(body)
                    if redirect_origin and redirect_origin not in tried:
                        print(f"  [REGION] PLAUD redirected to {redirect_origin}")
                        queue.insert(0, redirect_origin)
                        redirect_followed = True
                        continue
                msg = body.get("msg") or "regional redirect"
                errors.append((origin, f"status=-302 ({msg})"))
                continue

            return body, origin

        except PlaudAuthError:
            raise  # propagate unchanged
        except requests.HTTPError as e:
            errors.append((origin, f"HTTP error: {e}"))
            continue
        except requests.RequestException as e:
            errors.append((origin, f"request error: {e}"))
            continue

    # Everything failed — assemble a useful diagnostic.
    detail = "\n".join(f"  {o}: {e}" for o, e in errors) or "  (no detail)"
    hint = ""
    if last_minus_302_payload:
        target = extract_redirect_origin(last_minus_302_payload)
        if target:
            hint = (
                f"\n  PLAUD's regional redirect pointed at {target}, "
                f"but that host was unreachable.\n"
                f"  Try passing it explicitly:  --base-url {target}"
            )
        else:
            hint = (
                f"\n  PLAUD returned a regional redirect (status -302) "
                f"but no usable API host was found in the payload.\n"
                f"  Raw response: "
                f"{json.dumps(last_minus_302_payload, ensure_ascii=False)[:300]}"
            )
    raise PlaudRoutingError(
        f"Failed to reach PLAUD across {len(tried)} origin(s):\n{detail}{hint}"
    )


# ──────────────────────────────────────────────
# API HELPERS
# ──────────────────────────────────────────────
def probe_token_and_pick_origin(session: requests.Session,
                                origins: list[str]) -> str:
    """Verify the token works AND discover the working API origin.

    Calls /file/simple/web?limit=1 — same trick as the JS server.
    """
    body, working = call_plaud_api(
        session, origins, "/file/simple/web",
        params={"skip": 0, "limit": 1, "is_trash": 0,
                "sort_by": "edit_time", "is_desc": "true"},
    )
    if isinstance(body, dict) and body.get("status", 0) != 0:
        raise PlaudRoutingError(
            f"Token probe at {working} returned status={body.get('status')} "
            f"msg={body.get('msg')}"
        )
    return working


def fetch_recordings_page(session: requests.Session, origin: str,
                          skip: int, limit: int) -> dict:
    body, _ = call_plaud_api(
        session, [origin], "/file/simple/web",
        params={
            "skip":     skip,
            "limit":    limit,
            "is_trash": 0,
            "sort_by":  "edit_time",
            "is_desc":  "true",
        },
    )
    return body


def collect_all_recordings(session: requests.Session, origin: str
                           ) -> list[dict]:
    all_recordings: list[dict] = []
    skip = 0
    total: object = None

    while True:
        print(f"  Fetching recordings {skip + 1}–{skip + PAGE_SIZE}...",
              end=" ", flush=True)
        try:
            data = fetch_recordings_page(session, origin, skip=skip, limit=PAGE_SIZE)
        except (PlaudAuthError, PlaudRoutingError, requests.HTTPError) as e:
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

        if len(items) < PAGE_SIZE:
            break

        skip += len(items)
        time.sleep(0.3)  # be polite

    return all_recordings


def get_temp_url(session: requests.Session, origin: str, file_id: str,
                 prefer_opus: bool = False) -> Optional[str]:
    """Fetch a time-limited pre-signed download URL.

    Handles both flat and nested PLAUD response shapes:
        {"status": 0, "temp_url": "..."}
        {"status": 0, "data": {"temp_url": "..."}}
    """
    encoded_id = quote(str(file_id).strip(), safe="")
    params = {"is_opus": 1 if prefer_opus else 0}

    try:
        body, _ = call_plaud_api(
            session, [origin],
            f"/file/temp-url/{encoded_id}",
            params=params,
        )
    except (PlaudAuthError, PlaudRoutingError, requests.HTTPError):
        return None

    if not isinstance(body, dict) or body.get("status", 0) != 0:
        return None

    inner = body.get("data") if isinstance(body.get("data"), dict) else {}

    if prefer_opus:
        return pick_first_nonempty(
            body.get("temp_url_opus"),  inner.get("temp_url_opus"),
            body.get("temp_url"),       inner.get("temp_url"),
        ) or None
    return pick_first_nonempty(
        body.get("temp_url"),  inner.get("temp_url"),
    ) or None


# ──────────────────────────────────────────────
# FFMPEG INTEGRATION
# ──────────────────────────────────────────────
_FFMPEG_PATH_CACHE: Optional[str] = None
_FFMPEG_PROBED = False


def find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg and verify it runs. Cached after first call."""
    global _FFMPEG_PATH_CACHE, _FFMPEG_PROBED
    if _FFMPEG_PROBED:
        return _FFMPEG_PATH_CACHE
    _FFMPEG_PROBED = True

    candidate = (
        os.environ.get("PLAUD_FFMPEG_PATH")
        or shutil.which("ffmpeg")
        or "ffmpeg"
    )
    try:
        result = subprocess.run(
            [candidate, "-version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            _FFMPEG_PATH_CACHE = candidate
            return candidate
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return None


def transcode_audio(src: Path, dst: Path, target_format: str) -> bool:
    """Transcode `src` -> `dst` via ffmpeg.

    target_format: 'mp3' or 'wav'.
    Returns True on success, False on failure (with a printed reason).
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("  [ERROR] ffmpeg not found. Install it or set PLAUD_FFMPEG_PATH.")
        return False

    if target_format == "mp3":
        codec_args = ["-c:a", "libmp3lame", "-q:a", "2"]   # ~190 kbps VBR
    elif target_format == "wav":
        codec_args = ["-c:a", "pcm_s16le"]
    else:
        print(f"  [ERROR] Unsupported transcode target: {target_format}")
        return False

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn",
        *codec_args,
        str(dst),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        print(f"  [ffmpeg] failed to launch: {e}")
        return False

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        print(f"  [ffmpeg] exit {result.returncode}: {stderr[:300]}")
        return False
    if not dst.exists() or dst.stat().st_size == 0:
        print("  [ffmpeg] produced no output file")
        return False
    return True


# ──────────────────────────────────────────────
# DOWNLOAD LOGIC
# ──────────────────────────────────────────────
def download_to_path(url: str, dest: Path) -> tuple[bool, str]:
    """Stream a pre-signed S3 URL to disk.

    Returns (success, content_type). content_type lets the caller pick the
    right extension when metadata can't be trusted.
    """
    try:
        # Plain requests.get — pre-signed S3 URLs don't need an Authorization header.
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            content_type = resp.headers.get("Content-Type", "")
            downloaded = 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
            if total and downloaded < total * 0.99:
                print(f"  [WARN] Incomplete download ({downloaded}/{total} bytes)")
                return False, content_type
            return True, content_type
    except requests.RequestException as e:
        print(f"  [ERROR] Download failed: {e}")
        return False, ""


# ──────────────────────────────────────────────
# FILENAME HELPERS
# ──────────────────────────────────────────────
def sanitize_filename(name: str) -> str:
    keepchars = " ._-()[]"
    return "".join(c if (c.isalnum() or c in keepchars) else "_" for c in name).strip()


def pick_recording_id(rec: dict) -> str:
    """PLAUD returns different ID field names in different contexts."""
    return pick_first_nonempty(
        rec.get("id"), rec.get("file_id"), rec.get("fileId"),
    )


def pick_recording_title(rec: dict, index: int) -> str:
    """Try each known title field; fall back to a generic name."""
    title = pick_first_nonempty(
        rec.get("title"),
        rec.get("file_title"), rec.get("fileTitle"),
        rec.get("name"),
        rec.get("file_name"),  rec.get("fileName"),
        rec.get("filename"),
    )
    return title or f"recording_{index:04d}"


def build_filename_stem(rec: dict, index: int) -> str:
    """Build the filename stem (without extension)."""
    title = pick_recording_title(rec, index)

    date_str = ""
    raw_ts = rec.get("start_time") or rec.get("startTime")
    if raw_ts:
        try:
            ts = int(raw_ts)
            # PLAUD timestamps are in milliseconds; tolerate seconds too.
            if ts > 10**12:
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            else:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            pass

    rec_id = pick_recording_id(rec)[:8]
    parts = [p for p in [date_str, sanitize_filename(title), rec_id] if p]
    return "_".join(parts) or f"recording_{index:04d}"


# Common audio Content-Type -> extension. Mirrors the JS `inferAudioFileExtension`.
_CONTENT_TYPE_EXT_MAP = {
    "audio/mpeg":   ".mp3",
    "audio/mp3":    ".mp3",
    "audio/mp4":    ".m4a",
    "audio/x-m4a":  ".m4a",
    "audio/aac":    ".aac",
    "audio/wav":    ".wav",
    "audio/x-wav":  ".wav",
    "audio/wave":   ".wav",
    "audio/ogg":    ".ogg",
    "audio/opus":   ".opus",
    "audio/webm":   ".webm",
    "audio/flac":   ".flac",
    "audio/x-flac": ".flac",
}


def extension_for_source_format(rec: dict, source_format: str,
                                content_type: str = "") -> str:
    """Decide the on-disk extension based on what was actually downloaded.

    Tells the truth — won't claim .m4a when the bytes are OPUS.
    Priority: explicit OPUS -> Content-Type -> metadata.filetype -> .m4a.
    """
    if source_format == "opus":
        return ".opus"

    # Prefer the response's Content-Type when we have it — most authoritative.
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct and ct in _CONTENT_TYPE_EXT_MAP:
        return _CONTENT_TYPE_EXT_MAP[ct]

    # Fall back to PLAUD's metadata.
    filetype = pick_first_nonempty(rec.get("filetype"), rec.get("fileType"))
    if filetype:
        return "." + filetype.lstrip(".").lower()

    return ".m4a"


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download audio recordings from your Plaud account.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--token", "-t", default=BEARER_TOKEN,
                        help="Plaud bearer token (overrides PLAUD_TOKEN env var)")
    parser.add_argument("--output", "-o", default=OUTPUT_DIR,
                        help=f"Output directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="List recordings without downloading")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of recordings to process (default: all)")
    parser.add_argument("--base-url", default=None,
                        help=("Override PLAUD API base URL. Usually unnecessary — "
                              "the script auto-discovers your region via PLAUD's "
                              "redirect mechanism."))
    parser.add_argument("--format", default="original", choices=SUPPORTED_FORMATS,
                        help=("What to save. 'original' (M4A from device, default) | "
                              "'opus' (smaller, server-side transcode) | "
                              "'mp3' (download original, locally re-encode via "
                              "ffmpeg — fixes PLAUD's malformed M4As) | "
                              "'wav' (locally re-encode via ffmpeg)."))
    parser.add_argument("--keep-original", action="store_true",
                        help=("When --format mp3/wav, also keep the downloaded "
                              "original file alongside the transcoded version."))
    # Deprecated; --format defaults to 'original' now (which matches --no-opus).
    parser.add_argument("--no-opus", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    token = (args.token or "").strip()
    if not token:
        print("[ERROR] No bearer token provided.")
        print("        Set PLAUD_TOKEN env variable or use --token <your_token>")
        sys.exit(1)

    target_format: str = args.format
    needs_ffmpeg = target_format in FORMATS_NEEDING_FFMPEG
    if needs_ffmpeg and not find_ffmpeg():
        print(f"[ERROR] --format {target_format} requires ffmpeg, but it wasn't found.")
        print("        Install it (apt/brew/choco install ffmpeg) "
              "or set PLAUD_FFMPEG_PATH.")
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build the candidate origin list. If --base-url was given, try it first.
    origin_candidates = dedupe_origins(
        ([args.base_url] + CANDIDATE_API_ORIGINS)
        if args.base_url else CANDIDATE_API_ORIGINS
    )

    session = make_session(token)

    print(f"\n  Plaud Audio Downloader v2")
    print(f"{'─'*40}")
    print(f"  Output dir : {output_dir.resolve()}")
    print(f"  Format     : {target_format}"
          + (" (will re-encode via ffmpeg)" if needs_ffmpeg else ""))
    print(f"  API hosts  : {', '.join(origin_candidates)}")
    print(f"  Dry run    : {args.dry_run}\n")

    # ── Probe token + discover working API host ───────────────────────────────
    print("Verifying token and discovering API host...")
    try:
        working_origin = probe_token_and_pick_origin(session, origin_candidates)
    except PlaudAuthError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
    except PlaudRoutingError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
    print(f"  Using API host: {working_origin}\n")

    # ── 1. Collect all recording metadata ─────────────────────────────────────
    print("Fetching recording list...")
    recordings = collect_all_recordings(session, working_origin)

    if not recordings:
        print("\nNo recordings found. Dumping raw API response for diagnosis:")
        try:
            raw, _ = call_plaud_api(
                session, [working_origin], "/file/simple/web",
                params={"skip": 0, "limit": 5, "is_trash": 0},
            )
            print(json.dumps(raw, indent=2, ensure_ascii=False)[:3000])
        except Exception as e:
            print(f"  (could not fetch raw response: {e})")
        sys.exit(0)

    if args.limit:
        recordings = recordings[: args.limit]

    print(f"\nFound {len(recordings)} recording(s) to process.\n")

    # ── 2. Download (and optionally transcode) each recording ─────────────────
    success, skipped, failed = 0, 0, 0
    # Source format for the temp_url request: opus only when explicitly asked.
    use_opus_source = (target_format == "opus")

    for i, rec in enumerate(recordings, start=1):
        stem = build_filename_stem(rec, i)
        file_id = pick_recording_id(rec)

        # Decide the final on-disk extension based on what we'll actually save.
        if target_format == "mp3":
            final_ext = ".mp3"
        elif target_format == "wav":
            final_ext = ".wav"
        else:
            final_ext = extension_for_source_format(rec, target_format)
        final_dest = output_dir / f"{stem}{final_ext}"

        print(f"[{i:>3}/{len(recordings)}] {final_dest.name}")

        if args.dry_run:
            print(f"         id   : {file_id}")
            print(f"         dest : {final_dest}")
            skipped += 1
            continue

        if final_dest.exists():
            print("         [SKIP] Already exists.")
            skipped += 1
            continue

        if not file_id:
            print("         [SKIP] No file ID in metadata — cannot fetch download URL.")
            skipped += 1
            continue

        # Step 1: get a fresh pre-signed S3 URL (these expire quickly).
        audio_url = get_temp_url(session, working_origin, file_id,
                                 prefer_opus=use_opus_source)
        if not audio_url:
            print("         [SKIP] Could not obtain a download URL.")
            skipped += 1
            continue

        # Step 2: download from S3 (no auth header needed).
        if needs_ffmpeg:
            # Download to a temp file, then transcode into the output dir.
            with tempfile.TemporaryDirectory(prefix="plaud-dl-") as tmp:
                tmp_dest = Path(tmp) / f"{stem}.input"
                ok, content_type = download_to_path(audio_url, tmp_dest)
                if not ok:
                    failed += 1
                    continue
                if not transcode_audio(tmp_dest, final_dest, target_format):
                    failed += 1
                    continue
                if args.keep_original:
                    src_ext = extension_for_source_format(
                        rec, "original", content_type=content_type)
                    keep_path = output_dir / f"{stem}{src_ext}"
                    if not keep_path.exists():
                        try:
                            shutil.copyfile(tmp_dest, keep_path)
                            print(f"         (kept original at {keep_path.name})")
                        except OSError as e:
                            print(f"         [WARN] could not keep original: {e}")
            size_kb = final_dest.stat().st_size // 1024
            print(f"         Saved {size_kb:,} KB → {final_dest.name} (re-encoded)")
            success += 1
        else:
            ok, _ = download_to_path(audio_url, final_dest)
            if ok:
                size_kb = final_dest.stat().st_size // 1024
                print(f"         Saved {size_kb:,} KB → {final_dest.name}")
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
    try:
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(recordings, f, indent=2, ensure_ascii=False, default=str)
        print(f"  Metadata saved to {metadata_path}\n")
    except OSError as e:
        print(f"  [WARN] could not save metadata: {e}")


if __name__ == "__main__":
    main()
