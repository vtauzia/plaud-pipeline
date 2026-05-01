#!/usr/bin/env python3
"""
Google Drive Uploader
----------------------
Uploads .docx files from /transfer_out to a Google Drive folder using
OAuth2 authentication with your personal Google account.

Only files that do not already exist in the Drive folder are uploaded,
checked by filename.

SETUP:
    pip install google-api-python-client google-auth google-auth-oauthlib

    1. Google Cloud Console → APIs & Services → OAuth consent screen
       External → fill App name + your email → Save
       (Skip scopes, add your Gmail as a test user)

    2. APIs & Services → Credentials → + Create Credentials → OAuth client ID
       Application type: Desktop app → Create → Download JSON
       Save as credentials.json next to this script (or set GDRIVE_CREDS_FILE)

    3. Set your Drive folder ID (last part of the folder's URL):
       https://drive.google.com/drive/folders/THIS_PART_HERE

    4. Run the script once — it prints a URL, you open it in any browser,
       approve, paste the code back. A token.json is saved for future runs.

USAGE:
    python3 upload_to_gdrive.py
    python3 upload_to_gdrive.py --input /plaud/transfer_out --folder-id XXXX
    python3 upload_to_gdrive.py --dry-run
"""

import os
import sys
import argparse
from pathlib import Path

# ──────────────────────────────────────────────
# CONFIGURATION — edit these or pass via CLI / env vars
# ──────────────────────────────────────────────
TRANSFER_OUT_DIR = "transfer_out"
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "YOUR_FOLDER_ID_HERE")
CREDENTIALS_FILE = os.environ.get("GDRIVE_CREDS_FILE", "credentials.json")
TOKEN_FILE       = os.environ.get("GDRIVE_TOKEN_FILE", "token.json")

# Scopes: drive.file = only files created by this app; drive = full access.
# Use drive.file if you only ever upload from this script.
# Use drive if you also need to read/list files created outside the script.
SCOPES = ["https://www.googleapis.com/auth/drive"]

UPLOAD_EXTENSIONS = {".docx", ".doc", ".pdf", ".md"}
MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".pdf":  "application/pdf",
    ".md":   "text/markdown",
}
DEFAULT_MIME = "application/octet-stream"


# ──────────────────────────────────────────────
# AUTHENTICATION
# ──────────────────────────────────────────────
def get_credentials(credentials_file: str, token_file: str):
    """
    Load cached OAuth2 credentials from token.json, refreshing automatically
    if they have expired. On first run (or if token.json is missing/invalid),
    starts a console-based auth flow:
      1. Prints a URL — open it in any browser on any machine
      2. Log in with your Google account and approve
      3. Copy the authorisation code and paste it back here
      4. token.json is saved — subsequent runs are fully automatic
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("[ERROR] Required libraries not installed.")
        print("        Run: pip install google-api-python-client google-auth google-auth-oauthlib")
        sys.exit(1)

    creds = None

    # Try loading a saved token
    if Path(token_file).exists():
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except Exception:
            creds = None   # corrupt token — will re-authenticate below

    # Refresh expired token automatically
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, token_file)
            return creds
        except Exception as e:
            print(f"[WARN] Token refresh failed ({e}). Re-authenticating...")
            creds = None

    # Valid token already loaded
    if creds and creds.valid:
        return creds

    # First run (or token unusable) — start the OAuth flow
    if not Path(credentials_file).exists():
        print(f"[ERROR] OAuth credentials file not found: {credentials_file}")
        print("        Download it from Google Cloud Console:")
        print("        APIs & Services → Credentials → your OAuth client → Download JSON")
        sys.exit(1)

    print("\n── Google OAuth2 Authentication ────────────────────────────────")
    print("  This is a one-time step. The script will start a local server on")
    print("  port 8080 to receive the OAuth callback from Google.")
    print()
    print("  Since you are on a remote machine, open a SECOND terminal on your")
    print("  local computer and run this SSH tunnel command BEFORE continuing:")
    print()
    print("      ssh -L 8080:localhost:8080 vtauzia@debianVM")
    print()
    print("  Then press Enter here to continue — a URL will be printed.")
    print("  Open it in your local browser, log in with your company account,")
    print("  click Allow, and the script will finish automatically.\n")
    input("  Press Enter when the SSH tunnel is open...")

    flow  = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
    creds = flow.run_local_server(
        port=8080,
        open_browser=False,
        success_message="Authentication successful — you can close this tab and return to the terminal.",
    )

    _save_token(creds, token_file)
    print(f"\n  Token saved to {token_file} — future runs will not require this step.\n")
    return creds


def _save_token(creds, token_file: str) -> None:
    with open(token_file, "w") as f:
        f.write(creds.to_json())


# ──────────────────────────────────────────────
# GOOGLE DRIVE OPERATIONS
# ──────────────────────────────────────────────
def build_drive_service(credentials_file: str, token_file: str):
    """Authenticate and return a Drive API v3 service object."""
    try:
        from googleapiclient.discovery import build
    except ImportError:
        print("[ERROR] google-api-python-client not installed.")
        print("        Run: pip install google-api-python-client")
        sys.exit(1)

    creds = get_credentials(credentials_file, token_file)
    return build("drive", "v3", credentials=creds)


def list_remote_files(service, folder_id: str) -> dict[str, str]:
    """
    Return {filename: file_id} for all non-trashed files in the Drive folder.
    Handles pagination (Drive returns max 1000 results per page).
    """
    remote: dict[str, str] = {}
    page_token = None

    while True:
        kwargs = {
            "q":         f"'{folder_id}' in parents and trashed = false",
            "fields":    "nextPageToken, files(id, name)",
            "pageSize":  1000,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        resp       = service.files().list(**kwargs).execute()
        page_token = resp.get("nextPageToken")

        for f in resp.get("files", []):
            remote[f["name"]] = f["id"]

        if not page_token:
            break

    return remote


def upload_file(service, local_path: Path, folder_id: str, mime_type: str) -> str:
    """
    Upload a single file to the Drive folder and return the new Drive file ID.
    Uses resumable upload for reliability on large files / flaky networks.
    """
    from googleapiclient.http import MediaFileUpload

    metadata = {"name": local_path.name, "parents": [folder_id]}
    media    = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

    result = (
        service.files()
        .create(body=metadata, media_body=media, fields="id")
        .execute()
    )
    return result.get("id", "")


def _api_error_message(e: Exception) -> str:
    """Extract a readable message from a Google API HttpError."""
    try:
        import json as _json
        detail = _json.loads(e.content.decode())          # type: ignore[attr-defined]
        return detail.get("error", {}).get("message", str(e))
    except Exception:
        return str(e)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Upload .docx files from transfer_out to Google Drive."
    )
    parser.add_argument(
        "--input", "-i",
        default=TRANSFER_OUT_DIR,
        help=f"Local folder to upload from (default: {TRANSFER_OUT_DIR})"
    )
    parser.add_argument(
        "--folder-id",
        default=GDRIVE_FOLDER_ID,
        help="Google Drive destination folder ID (last part of the folder URL)"
    )
    parser.add_argument(
        "--credentials",
        default=CREDENTIALS_FILE,
        metavar="credentials.json",
        help=f"Path to OAuth2 client credentials JSON (default: {CREDENTIALS_FILE})"
    )
    parser.add_argument(
        "--token",
        default=TOKEN_FILE,
        metavar="token.json",
        help=f"Path to saved OAuth2 token (default: {TOKEN_FILE}, created on first run)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be uploaded without actually uploading"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of files to upload in this run (default: all pending)"
    )
    args = parser.parse_args()

    input_dir   = Path(args.input)
    folder_id   = args.folder_id

    # ── Validate ──────────────────────────────────────────────────────────
    if folder_id == "YOUR_FOLDER_ID_HERE" or not folder_id:
        print("[ERROR] No Google Drive folder ID provided.")
        print("        Set GDRIVE_FOLDER_ID env var or use --folder-id XXXX")
        print("        (The ID is the last part of the folder's Drive URL)")
        sys.exit(1)

    if not input_dir.exists():
        print(f"[ERROR] Input directory not found: {input_dir}")
        sys.exit(1)

    # ── Collect local files ───────────────────────────────────────────────
    local_files = sorted(
        f for f in input_dir.iterdir()
        if f.is_file() and f.suffix.lower() in UPLOAD_EXTENSIONS
    )

    if not local_files:
        print(f"No uploadable files found in {input_dir}")
        sys.exit(0)

    # ── Connect and check remote state ────────────────────────────────────
    if not args.dry_run:
        print("Authenticating with Google Drive...", end=" ", flush=True)
        service = build_drive_service(args.credentials, args.token)
        print("OK")

        print("Fetching remote file list...", end=" ", flush=True)
        remote_files = list_remote_files(service, folder_id)
        print(f"{len(remote_files)} file(s) already in Drive folder")
    else:
        service      = None
        remote_files = {}

    # ── Determine what needs uploading ────────────────────────────────────
    pending = [f for f in local_files if f.name not in remote_files]
    done    = len(local_files) - len(pending)

    print(f"\n Google Drive Uploader")
    print(f"{'─'*42}")
    print(f"  Source dir    : {input_dir.resolve()}")
    print(f"  Drive folder  : https://drive.google.com/drive/folders/{folder_id}")
    print(f"  Files         : {len(local_files)} local, {done} already in Drive, {len(pending)} pending")
    print(f"  Dry run       : {args.dry_run}\n")

    if not pending:
        print("All files already exist in the Drive folder. Nothing to upload.")
        sys.exit(0)

    if args.limit:
        pending = pending[: args.limit]
        print(f"  (uploading first {args.limit} pending files)\n")

    # ── Upload ────────────────────────────────────────────────────────────
    success, skipped, failed = 0, 0, 0

    for i, local_file in enumerate(pending, start=1):
        size_kb   = local_file.stat().st_size // 1024
        mime_type = MIME_TYPES.get(local_file.suffix.lower(), DEFAULT_MIME)

        print(f"[{i:>3}/{len(pending)}] {local_file.name}  ({size_kb} KB)")

        if args.dry_run:
            print(f"         → Drive folder  [dry run, skipped]")
            skipped += 1
            continue

        try:
            file_id = upload_file(service, local_file, folder_id, mime_type)
            print(f"         → uploaded  (id: {file_id})")
            success += 1
        except Exception as e:
            print(f"         [ERROR] {_api_error_message(e)}")
            failed += 1

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'─'*42}")
    print(f"  Uploaded  : {success}")
    print(f"  Skipped   : {skipped}")
    print(f"  Failed    : {failed}")
    print(f"  Drive URL : https://drive.google.com/drive/folders/{folder_id}\n")


if __name__ == "__main__":
    main()