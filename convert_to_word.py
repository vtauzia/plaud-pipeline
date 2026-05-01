#!/usr/bin/env python3
"""
Markdown to Word Exporter
--------------------------
Converts meeting-minutes Markdown files from /summaries into .docx files
in /transfer_out using Pandoc. Only files without a matching .docx are
processed.

REQUIREMENTS:
    pandoc must be installed and on PATH
    (check with: pandoc --version)

USAGE:
    python3 export_to_docx.py
    python3 export_to_docx.py --summaries /plaud/summaries --output /plaud/transfer_out
    python3 export_to_docx.py --dry-run
    python3 export_to_docx.py --reference-doc my_template.docx  # apply a Word style template
"""

import os
import sys
import shutil
import subprocess
import argparse
from pathlib import Path

# ──────────────────────────────────────────────
# CONFIGURATION — edit these or pass via CLI
# ──────────────────────────────────────────────
SUMMARIES_DIR   = "summaries"
TRANSFER_OUT_DIR = "transfer_out"


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def check_pandoc() -> str:
    """Verify pandoc is available and return its version string."""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        print("[ERROR] pandoc not found on PATH.")
        print("        Install it from https://pandoc.org/installing.html")
        print("        or via:  sudo apt install pandoc  /  brew install pandoc")
        sys.exit(1)

    result = subprocess.run(
        ["pandoc", "--version"],
        capture_output=True, text=True
    )
    version_line = result.stdout.splitlines()[0] if result.stdout else "unknown version"
    return version_line


def docx_path(md_file: Path, output_dir: Path) -> Path:
    """Return the expected .docx path in output_dir for a given .md file."""
    return output_dir / (md_file.stem + ".docx")


def already_exported(md_file: Path, output_dir: Path) -> bool:
    """Return True if a non-empty .docx already exists for this markdown file."""
    dest = docx_path(md_file, output_dir)
    return dest.exists() and dest.stat().st_size > 0


def convert_to_docx(
    md_file: Path,
    dest: Path,
    reference_doc: Path | None = None,
) -> tuple[bool, str]:
    """
    Run pandoc to convert a Markdown file to .docx.

    pandoc command:
        pandoc input.md -o output.docx [--reference-doc=template.docx]

    Returns (success: bool, message: str).
    """
    cmd = [
        "pandoc",
        str(md_file),
        "--output", str(dest),
        "--from",   "markdown",
        "--to",     "docx",
    ]

    if reference_doc:
        cmd += [f"--reference-doc={reference_doc}"]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "no error output").strip()
        return False, error

    # Verify the output file was actually created and is non-empty
    if not dest.exists() or dest.stat().st_size == 0:
        return False, "pandoc exited 0 but output file is missing or empty"

    return True, ""


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Convert summary Markdown files to Word .docx using Pandoc."
    )
    parser.add_argument(
        "--summaries", "-s",
        default=SUMMARIES_DIR,
        help=f"Directory containing Markdown summary files (default: {SUMMARIES_DIR})"
    )
    parser.add_argument(
        "--output", "-o",
        default=TRANSFER_OUT_DIR,
        help=f"Output directory for .docx files (default: {TRANSFER_OUT_DIR})"
    )
    parser.add_argument(
        "--reference-doc",
        default=None,
        metavar="TEMPLATE.docx",
        help="Optional Word template (.docx) for styles, fonts, and margins"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be converted without running pandoc"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of files to convert in this run (default: all pending)"
    )
    args = parser.parse_args()

    summaries_dir   = Path(args.summaries)
    output_dir      = Path(args.output)
    reference_doc   = Path(args.reference_doc) if args.reference_doc else None

    # ── Pre-flight checks ─────────────────────────────────────────────────
    if not args.dry_run:
        pandoc_version = check_pandoc()
    else:
        pandoc_version = "skipped (dry run)"

    if not summaries_dir.exists():
        print(f"[ERROR] Summaries directory not found: {summaries_dir}")
        sys.exit(1)

    if reference_doc and not reference_doc.exists():
        print(f"[ERROR] Reference doc not found: {reference_doc}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(output_dir, os.W_OK):
        print(f"[ERROR] Cannot write to output directory: {output_dir}")
        print(f"        Fix with:  sudo chown $USER {output_dir}")
        sys.exit(1)

    # ── Collect files ─────────────────────────────────────────────────────
    all_md = sorted(
        f for f in summaries_dir.iterdir()
        if f.is_file() and f.suffix.lower() == ".md"
    )

    if not all_md:
        print(f"No Markdown files found in {summaries_dir}")
        sys.exit(0)

    pending = [f for f in all_md if not already_exported(f, output_dir)]
    done    = len(all_md) - len(pending)

    print(f"\n Markdown → Word Exporter")
    print(f"{'─'*40}")
    print(f"  Summaries dir : {summaries_dir.resolve()}")
    print(f"  Output dir    : {output_dir.resolve()}")
    print(f"  Pandoc        : {pandoc_version}")
    if reference_doc:
        print(f"  Template      : {reference_doc}")
    print(f"  Files         : {len(all_md)} total, {done} already exported, {len(pending)} pending")
    print(f"  Dry run       : {args.dry_run}\n")

    if not pending:
        print("All summaries already have a matching .docx. Nothing to do.")
        sys.exit(0)

    if args.limit:
        pending = pending[: args.limit]
        print(f"  (processing first {args.limit} pending files)\n")

    # ── Convert ───────────────────────────────────────────────────────────
    success, skipped, failed = 0, 0, 0

    for i, md_file in enumerate(pending, start=1):
        dest     = docx_path(md_file, output_dir)
        size_kb  = md_file.stat().st_size // 1024

        print(f"[{i:>3}/{len(pending)}] {md_file.name}  ({size_kb} KB)")

        if args.dry_run:
            print(f"         → {dest.name}  [dry run, skipped]")
            skipped += 1
            continue

        ok, err = convert_to_docx(md_file, dest, reference_doc)

        if ok:
            out_kb = dest.stat().st_size // 1024
            print(f"         → {dest.name}  ({out_kb} KB)")
            success += 1
        else:
            print(f"         [ERROR] pandoc failed: {err}")
            # Remove any partial output file
            if dest.exists():
                dest.unlink()
            failed += 1

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'─'*40}")
    print(f"  Converted : {success}")
    print(f"  Skipped   : {skipped}")
    print(f"  Failed    : {failed}")
    print(f"  Output    : {output_dir.resolve()}\n")


if __name__ == "__main__":
    main()