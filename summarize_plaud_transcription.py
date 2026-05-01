#!/usr/bin/env python3
"""
Plaud Transcript Summarizer
-----------------------------
Reads JSON transcript files from /transcripts, sends each one to a local
Ollama instance, and writes Markdown meeting-minutes to /summaries.

Only transcripts that do not yet have a matching summary are processed.

Model  : qwen (via Ollama)
API    : http://192.168.68.85:11434/api/chat

Chunking strategy (time-based):
  Short meetings (≤ chunk duration) are sent in a single call.
  Longer meetings are split into overlapping time windows (default 12 min,
  configurable via --chunk-duration). Each window is summarised into dense
  notes (map phase), then all notes are synthesised into final minutes
  (reduce phase).

SETUP:
    pip install requests

USAGE:
    python3 summarize_transcripts.py
    python3 summarize_transcripts.py --transcripts /plaud/transcripts --summaries /plaud/summaries
    python3 summarize_transcripts.py --chunk-duration 10   # 10-minute chunks
    python3 summarize_transcripts.py --dry-run
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
OLLAMA_BASE_URL  = "http://192.XXX.XXX.XXX:11434"
MODEL            = "batiai/qwen3.6-27b:iq3"
TRANSCRIPTS_DIR  = "transcripts"
SUMMARIES_DIR    = "summaries"

REQUEST_TIMEOUT  = 60000    # seconds per LLM call

# Time-based chunking parameters.
# Each chunk covers CHUNK_DURATION_MINUTES of audio. Consecutive chunks
# overlap by CHUNK_OVERLAP_MINUTES so conversation context is not severed
# at boundaries. 10–15 minutes per chunk works well for 7–9B models.
DEFAULT_CHUNK_DURATION_MINUTES = 12
DEFAULT_CHUNK_OVERLAP_MINUTES  = 2

# Whisper verbose_json fields that carry no meaning for the LLM.
_WHISPER_NOISE_FIELDS = {
    "tokens", "avg_logprob", "compression_ratio", "no_speech_prob",
    "temperature", "seek", "id",
}

# Qwen3 (and other chat-formatted models) use these as structural delimiters.
# If they appear in generated output the model is hallucinating the next turn.
_CHAT_STOP_TOKENS = [
    "<|im_end|>", "<|im_start|>", "<|endoftext|>", "<|end_of_text|>",
]


# ──────────────────────────────────────────────
# PROMPTS
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """\
**System Persona:**
You are an expert AI meeting analyst, business strategist, and technical project manager. Your task is to carefully analyze a meeting transcript provided in a JSON format (containing keys such as `speaker`, `text`, and timestamps) and extract a highly granular, actionable, and structured summary.

**Instructions:**
1. **Analyze the JSON:** Read through the provided JSON transcript. Pay close attention to the flow of conversation between different speakers, the technical or strategic context (e.g., AI implementations, product roadmaps), and the subtle nuances or sentiments behind their words.
2. **Extract Nuance:** Do not just summarize the surface-level dialogue. Capture the underlying rationales, concerns, business priorities, and differing perspectives among the participants.
3. **Format strictly in Markdown:** Do not include raw JSON or timestamps in your final response. Use clear Markdown headers, bullet points, and bold text for readability.

**Required Output Structure:**

# [Insert a Descriptive and Professional Title]

## Executive Summary
Provide a concise but comprehensive high-level overview of the meeting. Summarize the primary purpose of the discussion, the core participants involved, the overall tone, and the ultimate outcomes or consensus reached.

## Granular Discussion Topics
Break down the transcript into clearly delineated, logical sections based on the main themes discussed (do not just summarize chronologically). For each theme, use a subheader (### [Topic Name]) and include:
- **Key Insights:** The core arguments, technical details, and business context discussed.
- **Nuances & Challenges:** Any specific blockers, differing opinions, risks, or strategic concerns raised by the speakers.
- **Decisions:** Any consensus or strategic directions agreed upon regarding this topic.

## Action Items
Provide a clearly articulated checklist of next steps and commitments derived from the conversation. For each item, explicitly state:
- **Task:** What needs to be done.
- **Owner:** Who is responsible (e.g., specific speakers or teams mentioned).
- **Timeline/Deadline:** When it needs to be completed (if discussed).\
"""

_CHUNK_SYSTEM_PROMPT = """\
You are a precise meeting note-taker. You will receive a time segment of a longer meeting transcript in JSON format.
Extract dense, lossless notes covering:
- Every decision made or position stated
- Every action item or commitment (with owner and deadline if mentioned)
- Key facts, numbers, names, and technical details
- Any disagreements, open questions, or risks raised
- The speakers involved and their roles/stances

Output plain bullet points only — no headers, no prose paragraphs, no JSON.
Be exhaustive: it is better to include too much than to lose information.
These notes will later be synthesised into final meeting minutes.\
"""

_REDUCE_SYSTEM_PROMPT = SYSTEM_PROMPT

_REDUCE_USER_PREFIX = """\
Below are detailed notes extracted from sequential time segments of a single meeting.
They cover the full meeting from start to finish.
Using these notes, produce the complete, polished meeting minutes as instructed.
Do NOT reference the note segments or their numbering — write as if you read the whole meeting at once.

--- NOTES START ---
"""


# ──────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────
def fmt_time(seconds: float) -> str:
    """Format a timestamp in seconds as MM:SS for readable display."""
    try:
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"
    except (TypeError, ValueError):
        return str(seconds)


def clean_model_output(text: str) -> str:
    """
    Truncate at the first chat-template special token.

    Qwen3 may generate <|im_start|> / <|endoftext|> when hallucinating the
    next conversation turn. Everything from that point on is noise. This is
    applied to every model response — critically including MAP outputs before
    they are concatenated into the REDUCE prompt, which would otherwise cause
    prompt injection and corrupt the final summary.
    """
    for token in _CHAT_STOP_TOKENS:
        idx = text.find(token)
        if idx != -1:
            text = text[:idx]
    return text.strip()


def sanitize_for_prompt(text: str) -> str:
    """
    Remove chat-template tokens from strings that will be embedded in prompts.
    Prevents transcript content that happens to contain these strings from being
    misinterpreted as conversation structure by the model's template parser.
    """
    for token in _CHAT_STOP_TOKENS:
        text = text.replace(token, "")
    return text


# ──────────────────────────────────────────────
# TRANSCRIPT PREPROCESSING
# ──────────────────────────────────────────────
def strip_whisper_noise(transcript_data: dict | list) -> list[dict]:
    """
    Return the segment list with Whisper metadata fields removed.

    Handles two common verbose_json shapes:
      • {"segments": [...], "text": "...", "language": "en", ...}
      • [{"start": ..., "end": ..., "text": ...}, ...]
    """
    if isinstance(transcript_data, list):
        segments = transcript_data
    else:
        segments = transcript_data.get("segments") or []

    return [
        {k: v for k, v in seg.items() if k not in _WHISPER_NOISE_FIELDS}
        for seg in segments
    ]


def get_duration(segments: list[dict]) -> float:
    """Return total audio duration in seconds from the segment timestamps."""
    if not segments:
        return 0.0
    last = segments[-1]
    return float(last.get("end") or last.get("start") or 0)


# ──────────────────────────────────────────────
# TIME-BASED CHUNKING
# ──────────────────────────────────────────────
def chunk_segments_by_time(
    segments: list[dict],
    chunk_duration_s: float,
    overlap_s: float,
) -> list[list[dict]]:
    """
    Split segments into overlapping time windows.

    Each window covers chunk_duration_s seconds of audio. Consecutive windows
    share overlap_s seconds at their boundary so the model retains context
    across cuts.

    Example with chunk=720s (12 min), overlap=60s (1 min):
      Window 1 :   0s –  720s
      Window 2 : 660s – 1380s   ← starts 60s before window 1 ends
      Window 3 : 1320s – 2040s
      ...

    A segment is included in a window if its start timestamp falls within
    [window_start, window_end). This is simple, predictable, and immune to
    segments that span a boundary — they appear in both windows.
    """
    if not segments:
        return []

    total = get_duration(segments)
    if total <= chunk_duration_s:
        return [segments]   # short enough for a single call

    step = chunk_duration_s - overlap_s
    if step <= 0:
        raise ValueError(
            f"overlap ({overlap_s}s) must be less than chunk duration ({chunk_duration_s}s)"
        )

    chunks: list[list[dict]] = []
    window_start = 0.0

    while window_start < total:
        window_end = window_start + chunk_duration_s
        chunk = [
            seg for seg in segments
            if seg.get("start", 0) >= window_start
            and seg.get("start", 0) < window_end
        ]
        if chunk:
            chunks.append(chunk)

        # Stop once we've covered up to (or past) the end
        if window_end >= total:
            break
        window_start += step

    return chunks


# ──────────────────────────────────────────────
# OLLAMA CALLS
# ──────────────────────────────────────────────
def call_ollama(system: str, user: str, ollama_base_url: str, model: str) -> str:
    """POST a chat completion to Ollama and return the cleaned assistant text."""
    url = ollama_base_url.rstrip("/") + "/api/chat"
    payload = {
        "model":  model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        # Belt-and-suspenders stop sequences: set at both top level and inside
        # options because different Ollama versions read one or the other.
        "stop":    _CHAT_STOP_TOKENS,
        "options": {
            "temperature": 0.3,
            "stop":        _CHAT_STOP_TOKENS,
        },
    }

    resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)

    if resp.status_code != 200:
        raise RuntimeError(
            f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    try:
        raw = body["message"]["content"]
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"Unexpected Ollama response shape: {e}\n{body}") from e

    return clean_model_output(raw)


# ──────────────────────────────────────────────
# SUMMARIZATION STRATEGIES
# ──────────────────────────────────────────────
def summarize_direct(segments: list[dict], ollama_url: str, model: str) -> str:
    """Single-pass summarization for short meetings that fit in one call."""
    transcript_json = sanitize_for_prompt(
        json.dumps(segments, indent=2, ensure_ascii=False)
    )
    user_msg = (
        "Here is the meeting transcript in JSON format. "
        "Please produce the full meeting minutes as instructed.\n\n"
        f"```json\n{transcript_json}\n```"
    )
    return call_ollama(SYSTEM_PROMPT, user_msg, ollama_url, model)


def summarize_chunked(
    chunks: list[list[dict]],
    ollama_url: str,
    model: str,
) -> str:
    """
    Map-reduce summarization for meetings split across multiple time windows.

    Map   : each time window → dense bullet-point notes (compact, exhaustive)
    Reduce: all notes combined → final meeting minutes (full SYSTEM_PROMPT)
    """
    n = len(chunks)
    print(f"         [CHUNKED] {n} time windows — running map-reduce...")

    # ── MAP phase ─────────────────────────────────────────────────────────
    all_notes: list[str] = []

    for idx, chunk in enumerate(chunks, start=1):
        first_s = chunk[0].get("start", 0)
        last_s  = chunk[-1].get("end") or chunk[-1].get("start", 0)
        label   = f"{fmt_time(first_s)} – {fmt_time(last_s)}"

        print(f"         [MAP {idx}/{n}] {label} ({len(chunk)} segments) ...",
              end=" ", flush=True)
        t0 = time.monotonic()

        chunk_json = sanitize_for_prompt(
            json.dumps(chunk, indent=2, ensure_ascii=False)
        )
        user_msg = (
            f"This is segment {idx} of {n} of the meeting transcript "
            f"covering {label}.\n\n"
            f"```json\n{chunk_json}\n```"
        )

        notes = call_ollama(_CHUNK_SYSTEM_PROMPT, user_msg, ollama_url, model)

        # Guard: if MAP output is suspiciously short it likely failed silently
        if len(notes.strip()) < 20:
            print(f"[WARN: very short output — {len(notes)} chars]", end=" ")

        all_notes.append(f"### Segment {idx}/{n} ({label})\n{notes}")
        print(f"done ({time.monotonic() - t0:.1f}s)")

    # ── REDUCE phase ──────────────────────────────────────────────────────
    print(f"         [REDUCE] Synthesising {n} note sets into final minutes ...",
          end=" ", flush=True)
    t0 = time.monotonic()

    combined_notes = sanitize_for_prompt("\n\n".join(all_notes))
    reduce_user    = _REDUCE_USER_PREFIX + combined_notes + "\n--- NOTES END ---"
    final          = call_ollama(_REDUCE_SYSTEM_PROMPT, reduce_user, ollama_url, model)

    print(f"done ({time.monotonic() - t0:.1f}s)")
    return final


def summarize_transcript(
    transcript_data: dict | list,
    ollama_url: str,
    model: str,
    chunk_duration_min: int,
    chunk_overlap_min: int,
) -> tuple[str, str]:
    """
    Main entry point. Returns (summary_markdown, strategy_used).

    Strategy:
      1. Strip Whisper noise fields.
      2. Measure total audio duration.
      3a. Duration ≤ chunk_duration → single direct call.
      3b. Duration  > chunk_duration → split into time windows, map-reduce.
    """
    segments = strip_whisper_noise(transcript_data)

    if not segments:
        raise ValueError("Transcript contains no segments after preprocessing.")

    total_s         = get_duration(segments)
    chunk_duration_s = chunk_duration_min * 60
    overlap_s        = chunk_overlap_min  * 60
    original_size    = len(json.dumps(transcript_data, ensure_ascii=False))
    stripped_size    = len(json.dumps(segments, ensure_ascii=False))
    reduction_pct    = 100 * (1 - stripped_size / original_size) if original_size else 0

    print(f"         [PREPROCESS] {len(segments)} segments, "
          f"duration {fmt_time(total_s)}, "
          f"stripped {reduction_pct:.0f}% noise "
          f"({stripped_size // 1024} KB remaining)")

    if total_s <= chunk_duration_s:
        print(f"         [STRATEGY] Direct (duration fits in one {chunk_duration_min}-min chunk)")
        return summarize_direct(segments, ollama_url, model), "direct"

    chunks = chunk_segments_by_time(segments, chunk_duration_s, overlap_s)
    n_chunks = len(chunks)
    print(f"         [STRATEGY] Map-reduce: {n_chunks} × {chunk_duration_min}-min chunks "
          f"(overlap {chunk_overlap_min} min)")
    return summarize_chunked(chunks, ollama_url, model), f"map-reduce ({n_chunks} chunks)"


# ──────────────────────────────────────────────
# CONNECTIVITY CHECK
# ──────────────────────────────────────────────
def check_ollama_reachable(ollama_base_url: str, model: str) -> None:
    try:
        resp = requests.get(ollama_base_url.rstrip("/") + "/api/tags", timeout=10)
        resp.raise_for_status()
        available = [m["name"] for m in resp.json().get("models", [])]
        if not any(m.startswith(model.split(":")[0]) for m in available):
            print(f"[WARN] Model '{model}' not listed on the server.")
            print(f"       Available: {', '.join(available) or '(none)'}")
            print(f"       Continuing — Ollama will pull it on first use if needed.\n")
    except requests.ConnectionError:
        print(f"[ERROR] Cannot reach Ollama at {ollama_base_url}")
        print(f"        Is the server running and accessible on the network?")
        sys.exit(1)
    except requests.Timeout:
        print(f"[ERROR] Connection to {ollama_base_url} timed out.")
        sys.exit(1)


# ──────────────────────────────────────────────
# FILE HELPERS
# ──────────────────────────────────────────────
def summary_path(transcript_file: Path, summaries_dir: Path) -> Path:
    return summaries_dir / (transcript_file.stem + ".md")


def already_summarized(transcript_file: Path, summaries_dir: Path) -> bool:
    dest = summary_path(transcript_file, summaries_dir)
    return dest.exists() and dest.stat().st_size > 0


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Summarize Plaud transcripts into meeting minutes using a local LLM."
    )
    parser.add_argument(
        "--transcripts", "-t",
        default=TRANSCRIPTS_DIR,
        help=f"Directory of JSON transcript files (default: {TRANSCRIPTS_DIR})"
    )
    parser.add_argument(
        "--summaries", "-s",
        default=SUMMARIES_DIR,
        help=f"Directory for output Markdown summaries (default: {SUMMARIES_DIR})"
    )
    parser.add_argument(
        "--ollama-url",
        default=OLLAMA_BASE_URL,
        help=f"Base URL of the Ollama server (default: {OLLAMA_BASE_URL})"
    )
    parser.add_argument(
        "--model",
        default=MODEL,
        help=f"Ollama model to use (default: {MODEL})"
    )
    parser.add_argument(
        "--chunk-duration",
        type=int,
        default=DEFAULT_CHUNK_DURATION_MINUTES,
        metavar="MINUTES",
        help=(
            f"Duration of each time-based chunk in minutes (default: {DEFAULT_CHUNK_DURATION_MINUTES}). "
            "Meetings shorter than this are sent in a single call. "
            "Recommended range: 10–15 for 7–9B models."
        )
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=DEFAULT_CHUNK_OVERLAP_MINUTES,
        metavar="MINUTES",
        help=(
            f"Overlap between consecutive chunks in minutes (default: {DEFAULT_CHUNK_OVERLAP_MINUTES}). "
            "Must be less than --chunk-duration."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List transcripts that would be summarized without calling the LLM"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of transcripts to process in this run (default: all pending)"
    )
    args = parser.parse_args()

    if args.chunk_overlap >= args.chunk_duration:
        print(f"[ERROR] --chunk-overlap ({args.chunk_overlap}) must be less than "
              f"--chunk-duration ({args.chunk_duration})")
        sys.exit(1)

    transcripts_dir = Path(args.transcripts)
    summaries_dir   = Path(args.summaries)
    ollama_url      = args.ollama_url.rstrip("/")
    model           = args.model

    if not transcripts_dir.exists():
        print(f"[ERROR] Transcripts directory not found: {transcripts_dir}")
        sys.exit(1)

    summaries_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(summaries_dir, os.W_OK):
        print(f"[ERROR] Cannot write to summaries directory: {summaries_dir}")
        print(f"        Fix with:  sudo chown $USER {summaries_dir}")
        sys.exit(1)

    all_transcripts = sorted(
        f for f in transcripts_dir.iterdir()
        if f.is_file()
        and f.suffix.lower() == ".json"
        and f.name != "_recordings_metadata.json"
    )

    if not all_transcripts:
        print(f"No JSON transcript files found in {transcripts_dir}")
        sys.exit(0)

    pending = [f for f in all_transcripts if not already_summarized(f, summaries_dir)]
    done    = len(all_transcripts) - len(pending)

    print(f"\n Plaud Transcript Summarizer")
    print(f"{'─'*44}")
    print(f"  Transcripts dir : {transcripts_dir.resolve()}")
    print(f"  Summaries dir   : {summaries_dir.resolve()}")
    print(f"  Ollama server   : {ollama_url}")
    print(f"  Model           : {model}")
    print(f"  Chunk size      : {args.chunk_duration} min  (overlap {args.chunk_overlap} min)")
    print(f"  Transcripts     : {len(all_transcripts)} total, {done} already summarized, {len(pending)} pending")
    print(f"  Dry run         : {args.dry_run}\n")

    if not pending:
        print("All transcripts already have summaries. Nothing to do.")
        sys.exit(0)

    if args.limit:
        pending = pending[: args.limit]
        print(f"  (processing first {args.limit} pending files)\n")

    if not args.dry_run:
        check_ollama_reachable(ollama_url, model)

    success, skipped, failed = 0, 0, 0

    for i, transcript_file in enumerate(pending, start=1):
        dest      = summary_path(transcript_file, summaries_dir)
        file_size = transcript_file.stat().st_size / 1024

        print(f"[{i:>3}/{len(pending)}] {transcript_file.name}  ({file_size:.0f} KB)")

        if args.dry_run:
            print(f"         → {dest.name}  [dry run, skipped]\n")
            skipped += 1
            continue

        try:
            with open(transcript_file, encoding="utf-8") as f:
                transcript_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"         [ERROR] Could not read transcript: {e}\n")
            failed += 1
            continue

        try:
            t0 = time.monotonic()
            summary_text, strategy = summarize_transcript(
                transcript_data,
                ollama_url,
                model,
                args.chunk_duration,
                args.chunk_overlap,
            )
            elapsed = time.monotonic() - t0

            with open(dest, "w", encoding="utf-8") as f:
                f.write(summary_text)

            first_line = next(
                (ln.strip("#").strip() for ln in summary_text.splitlines() if ln.strip()),
                "(no title)"
            )
            word_count = len(summary_text.split())

            print(f"         → {dest.name}  [{strategy}, {word_count} words, {elapsed:.1f}s]")
            print(f"            \"{first_line}\"")
            success += 1

        except ValueError as e:
            print(f"         [ERROR] {e}")
            failed += 1
        except requests.ConnectionError:
            print(f"         [ERROR] Lost connection to Ollama at {ollama_url}")
            failed += 1
        except requests.Timeout:
            print(f"         [ERROR] Request timed out after {REQUEST_TIMEOUT}s")
            print(f"                 Try increasing REQUEST_TIMEOUT in the script")
            failed += 1
        except RuntimeError as e:
            print(f"         [ERROR] {e}")
            failed += 1
        except OSError as e:
            print(f"         [ERROR] Could not write summary file: {e}")
            failed += 1
        except Exception as e:
            print(f"         [ERROR] Unexpected error: {e}")
            failed += 1

        print()

    print(f"{'─'*44}")
    print(f"  Summarized  : {success}")
    print(f"  Skipped     : {skipped}")
    print(f"  Failed      : {failed}")
    print(f"  Output dir  : {summaries_dir.resolve()}\n")


if __name__ == "__main__":
    main()