#!/usr/bin/env python3
"""
Plaud Recording Transcriber
-----------------------------
Checks /recordings for audio files that don't yet have a matching transcript
in /transcripts, then submits each one to a local Whisper-compatible API
(e.g. whisper.cpp server, faster-whisper-server, LocalAI) and saves the
verbose JSON response.

Equivalent curl command:
    curl -X POST http://192.168.68.85:8000/v1/audio/transcriptions \\
         -F file=@recording.m4a \\
         -F response_format=verbose_json \\
         | python3 -m json.tool

SETUP:
    pip install requests

USAGE:
    python3 transcribe_recordings.py
    python3 transcribe_recordings.py --recordings /plaud/recordings --transcripts /plaud/transcripts
    python3 transcribe_recordings.py --api-url http://192.168.68.85:8080 --dry-run
"""

import os
import sys
import json
import time
import argparse
import requests
from pathlib import Path

# ──────────────────────────────────────────────
# CONFIGURATION — edit these or pass via CLI
# ──────────────────────────────────────────────
API_URL         = "http://192.XXX.XXX.XXX:8080"
RECORDINGS_DIR  = "recordings"
TRANSCRIPTS_DIR = "transcripts"

# Audio extensions to consider
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".opus", ".ogg", ".wav", ".flac", ".aac", ".webm"}

# Transcription API settings
RESPONSE_FORMAT  = "verbose_json"   # verbose_json includes timestamps, language, segments
REQUEST_TIMEOUT  = 18000            # seconds — long audio files take time


# ──────────────────────────────────────────────
# TRANSCRIPTION
# ──────────────────────────────────────────────
def transcript_path(audio_file: Path, transcripts_dir: Path) -> Path:
    """Return the expected .json path in transcripts_dir for a given audio file."""
    return transcripts_dir / (audio_file.stem + ".json")


def already_transcribed(audio_file: Path, transcripts_dir: Path) -> bool:
    """Return True if a non-empty transcript JSON already exists for this file."""
    dest = transcript_path(audio_file, transcripts_dir)
    return dest.exists() and dest.stat().st_size > 0


def transcribe(audio_file: Path, api_url: str) -> dict:
    """
    POST the audio file to the transcription API and return the parsed JSON response.

    Equivalent to:
        curl -X POST {api_url}/v1/audio/transcriptions \\
             -F file=@{audio_file} \\
             -F response_format=verbose_json
    """
    endpoint = api_url.rstrip("/") + "/v1/audio/transcriptions"

    with open(audio_file, "rb") as f:
        files   = {"file": (audio_file.name, f, _mime_type(audio_file))}
        data    = {"response_format": RESPONSE_FORMAT}
        resp    = requests.post(endpoint, files=files, data=data, timeout=REQUEST_TIMEOUT)

    if resp.status_code == 200:
        return resp.json()

    # Surface the error clearly
    raise RuntimeError(
        f"API returned HTTP {resp.status_code}: {resp.text[:500]}"
    )


def _mime_type(path: Path) -> str:
    mime_map = {
        ".m4a":  "audio/mp4",
        ".mp3":  "audio/mpeg",
        ".opus": "audio/opus",
        ".ogg":  "audio/ogg",
        ".wav":  "audio/wav",
        ".flac": "audio/flac",
        ".aac":  "audio/aac",
        ".webm": "audio/webm",
    }
    return mime_map.get(path.suffix.lower(), "application/octet-stream")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Transcribe Plaud audio recordings that don't yet have a transcript."
    )
    parser.add_argument(
        "--recordings", "-r",
        default=RECORDINGS_DIR,
        help=f"Directory containing audio files (default: {RECORDINGS_DIR})"
    )
    parser.add_argument(
        "--transcripts", "-t",
        default=TRANSCRIPTS_DIR,
        help=f"Directory for output JSON transcripts (default: {TRANSCRIPTS_DIR})"
    )
    parser.add_argument(
        "--api-url",
        default=API_URL,
        help=f"Base URL of the transcription API (default: {API_URL})"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be transcribed without actually sending them"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of files to transcribe in this run (default: all pending)"
    )
    args = parser.parse_args()

    recordings_dir  = Path(args.recordings)
    transcripts_dir = Path(args.transcripts)
    api_url         = args.api_url.rstrip("/")

    # Validate directories
    if not recordings_dir.exists():
        print(f"[ERROR] Recordings directory not found: {recordings_dir}")
        sys.exit(1)

    transcripts_dir.mkdir(parents=True, exist_ok=True)

    if not os.access(transcripts_dir, os.W_OK):
        print(f"[ERROR] Cannot write to transcripts directory: {transcripts_dir}")
        print(f"        Fix with:  sudo chown $USER {transcripts_dir}")
        sys.exit(1)

    # Collect audio files
    audio_files = sorted(
        f for f in recordings_dir.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    )

    if not audio_files:
        print(f"No audio files found in {recordings_dir}")
        sys.exit(0)

    # Split into pending vs already done
    pending = [f for f in audio_files if not already_transcribed(f, transcripts_dir)]
    done    = len(audio_files) - len(pending)

    print(f"\n Plaud Transcriber")
    print(f"{'─'*40}")
    print(f"  Recordings dir  : {recordings_dir.resolve()}")
    print(f"  Transcripts dir : {transcripts_dir.resolve()}")
    print(f"  API endpoint    : {api_url}/v1/audio/transcriptions")
    print(f"  Audio files     : {len(audio_files)} total, {done} already transcribed, {len(pending)} pending")
    print(f"  Dry run         : {args.dry_run}\n")

    if not pending:
        print("All recordings already have transcripts. Nothing to do.")
        sys.exit(0)

    if args.limit:
        pending = pending[: args.limit]
        print(f"  (processing first {args.limit} pending files)\n")

    # ── Process each pending file ────────────────────────────────────────────
    success, skipped, failed = 0, 0, 0

    for i, audio_file in enumerate(pending, start=1):
        dest = transcript_path(audio_file, transcripts_dir)
        size_mb = audio_file.stat().st_size / (1024 * 1024)

        print(f"[{i:>3}/{len(pending)}] {audio_file.name}  ({size_mb:.1f} MB)")

        if args.dry_run:
            print(f"         → {dest.name}  [dry run, skipped]")
            skipped += 1
            continue

        try:
            start = time.monotonic()
            result = transcribe(audio_file, api_url)
            elapsed = time.monotonic() - start

            # Pretty-print JSON (matching what `| python3 -m json.tool` does)
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

            # Show a brief summary from the response
            language = result.get("language", "?")
            duration = result.get("duration", 0)
            n_segs   = len(result.get("segments", []))
            text_preview = (result.get("text") or "")[:80].replace("\n", " ")

            print(f"         → {dest.name}")
            print(f"            language={language}  duration={duration:.1f}s"
                  f"  segments={n_segs}  took={elapsed:.1f}s")
            if text_preview:
                print(f"            \"{text_preview}{'...' if len(result.get('text','')) > 80 else ''}\"")

            success += 1

        except FileNotFoundError:
            print(f"         [ERROR] Audio file disappeared: {audio_file}")
            failed += 1
        except RuntimeError as e:
            print(f"         [ERROR] {e}")
            failed += 1
        except requests.ConnectionError:
            print(f"         [ERROR] Could not connect to {api_url}")
            print(f"                 Is the transcription server running?")
            failed += 1
        except requests.Timeout:
            print(f"         [ERROR] Request timed out after {REQUEST_TIMEOUT}s")
            print(f"                 Try a shorter file or increase REQUEST_TIMEOUT in the script")
            failed += 1
        except Exception as e:
            print(f"         [ERROR] Unexpected error: {e}")
            failed += 1

        print()

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"{'─'*40}")
    print(f"  Transcribed : {success}")
    print(f"  Skipped     : {skipped}")
    print(f"  Failed      : {failed}")
    print(f"  Output dir  : {transcripts_dir.resolve()}\n")


if __name__ == "__main__":
    main()