# Plaud Pipeline — User & Maintenance Guide

This document describes a fully automated pipeline that runs on a Debian Linux server (`debianVM` at `192.168.68.79`). It downloads audio recordings from a Plaud device, transcribes them, summarises them into meeting minutes, converts them to Word documents, and uploads them to Google Drive — all without any manual intervention.

---

## Table of Contents

1. [What the pipeline does](#1-what-the-pipeline-does)
1. [File layout on the server](#2-file-layout-on-the-server)
1. [Prerequisites](#3-prerequisites)
1. [One-time setup](#4-one-time-setup)
1. [Configuration reference](#5-configuration-reference)
1. [Running the pipeline manually](#6-running-the-pipeline-manually)
1. [The automatic schedule](#7-the-automatic-schedule)
1. [Monitoring and logs](#8-monitoring-and-logs)
1. [Troubleshooting common errors](#9-troubleshooting-common-errors)
1. [Maintenance tasks](#10-maintenance-tasks)

---

## 1. What the pipeline does

The pipeline runs 6 Python scripts in sequence. Each script only processes files that have not been processed yet, so running it repeatedly is safe — it will never duplicate work.

| Step | Script | What it does |
| :--- | :--- | :--- |
| 1 | `copy_plaud_recording.py` | Downloads new audio recordings from your Plaud account via the Plaud API |
| 2 | `transcribe_plaud_recording.py` | Sends each new audio file to a local Whisper server and saves the transcript as JSON |
| 3 | `summarize_plaud_transcription.py` | Sends each new transcript to a local Ollama LLM and produces meeting minutes in Markdown |
| 4 | `convert_to_word.py` | Converts each new Markdown summary to a Word `.docx` file using Pandoc |
| 5 | `upload_to_gdrive.py` | Uploads new `.docx` files to a Google Drive folder |
| 6 | `delete_processed_recordings.py` | Deletes recordings that have been fully processed (transcript + summary + docx all exist) from Plaud cloud and from `/recordings` |

---

## 2. File layout on the server

```
/plaud/
│
├── copy_plaud_recording.py             ← Step 1 script
├── transcribe_plaud_recording.py            ← Step 2 script
├── summarize_plaud_transcription.py            ← Step 3 script
├── convert_to_word.py                   ← Step 4 script
├── upload_to_gdrive.py                 ← Step 5 script
├── delete_processed_recordings.py      ← Step 6 script
│
├── run_pipeline.sh             ← Master script that calls all 5 steps in order
├── pipeline.env                ← Environment variables (edit this to change settings)
├── credentials.json            ← Google OAuth2 client file (from Google Cloud Console)
├── token.json                  ← Google OAuth2 token (auto-created, do not edit)
│
├── recordings/                 ← Audio files downloaded from Plaud (.m4a / .opus)
├── transcripts/                ← JSON transcripts produced by Whisper
├── summaries/                  ← Markdown meeting minutes produced by the LLM
├── transfer_out/               ← Word .docx files ready for Google Drive
└── logs/                       ← One log file per pipeline run (last 30 kept)
```

The two systemd unit files live outside `/plaud`:

```
/etc/systemd/system/plaud-pipeline.service
/etc/systemd/system/plaud-pipeline.timer
```

---

## 3. Prerequisites

The following must be installed and running on the server. These were set up during the initial build and should not require reinstallation unless the server is rebuilt from scratch.

| Requirement | Purpose | Check it is running |
| :--- | :--- | :--- |
| Python 3.11+ | Runs all scripts | `python3 --version` |
| `requests` library | HTTP calls in all scripts | `pip show requests` |
| `google-api-python-client` | Google Drive upload | `pip show google-api-python-client` |
| `google-auth-oauthlib` | Google OAuth2 flow | `pip show google-auth-oauthlib` |
| Pandoc | Markdown → Word conversion | `pandoc --version` |
| Whisper server | Audio transcription (Step 2) | Running on `http://192.168.68.85:8000` |
| Ollama + qwen3:9b | LLM summarisation (Step 3) | Running on `http://192.168.68.85:11434` |

To install Python libraries if they are missing:

```bash
pip install requests google-api-python-client google-auth google-auth-oauthlib --break-system-packages
```

---

## 4. One-time setup

These steps only need to be done once (or repeated if the server is rebuilt).

### 4.1 — Get your Plaud bearer token

1. Open a browser and go to `app.plaud.ai`
1. Open DevTools (F12) → Network tab
1. Log in and find any request that goes to `api.plaud.ai`
1. In the request headers, find `Authorization: Bearer XXXXX`
1. Copy the long token string after `Bearer `
1. Open `/plaud/pipeline.env` and paste it next to `PLAUD_TOKEN=`

The token is valid for approximately 300 days. When it expires, repeat these steps.

### 4.2 — Set up Google Drive access

You need to do this if `credentials.json` or `token.json` are missing.

**Get `credentials.json` (from Google Cloud Console):**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in with your **company Google account**
1. Create a project (or select an existing one)
1. Go to **APIs & Services → Library** → search **Google Drive API** → **Enable**
1. Go to **APIs & Services → OAuth consent screen**
   - User Type: **Internal** → fill in App name and your company email → Save
1. Go to **APIs & Services → Credentials → + Create Credentials → OAuth client ID**
   - Application type: **Desktop app** → Create
   - Download the JSON file → rename it `credentials.json`
1. Copy it to the server:

```bash
   scp credentials.json vtauzia@192.168.68.79:/plaud/credentials.json
```

**Get your Drive folder ID:**

Open the destination folder in Google Drive. The URL looks like:
`https://drive.google.com/drive/folders/`**`1A2B3C4D5E6F7G8H9I0J`**

Copy the bold part. Open `/plaud/pipeline.env` and paste it next to `GDRIVE_FOLDER_ID=`.

**Generate `token.json` (one-time browser step):**

This step requires a temporary SSH tunnel. Open **two terminals**:

*Terminal 2 — on your local machine:*

```bash
ssh -L 8080:localhost:8080 vtauzia@192.168.68.79
```

*Terminal 1 — on the server:*

```bash
cd /plaud
python3 upload_to_gdrive.py
```

The script will print a Google URL. Open it in your browser, log in with your company account, click Allow. `token.json` is saved automatically. **You will never need to repeat this step** unless you delete `token.json` or revoke access in Google.

### 4.3 — Install the systemd timer

```bash
sudo cp /plaud/plaud-pipeline.service /etc/systemd/system/
sudo cp /plaud/plaud-pipeline.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable plaud-pipeline.timer
sudo systemctl start  plaud-pipeline.timer
```

Verify it is active:

```bash
systemctl status plaud-pipeline.timer
```

---

## 5. Configuration reference

All user-facing settings are in **`/plaud/pipeline.env`**. Edit this file to change any setting — no Python knowledge needed.

```bash
nano /plaud/pipeline.env
```

| Variable | What it controls | Example |
| :--- | :--- | :--- |
| `GDRIVE_FOLDER_ID` | Destination Google Drive folder | `1A2B3C4D5E6F7G8H9I0J` |
| `GDRIVE_CREDS_FILE` | Path to `credentials.json` | `/plaud/credentials.json` |
| `GDRIVE_TOKEN_FILE` | Path to `token.json` | `/plaud/token.json` |
| `PLAUD_TOKEN` | Plaud API bearer token | `eyJhbGci...` |

### Changing the pipeline frequency

The schedule lives in `/etc/systemd/system/plaud-pipeline.timer`. To change it:

```bash
sudo nano /etc/systemd/system/plaud-pipeline.timer
```

Find the line `OnUnitActiveSec=6h` and change the value:

| Value | Meaning |
| :--- | :--- |
| `30min` | Every 30 minutes |
| `1h` | Every hour |
| `6h` | Every 6 hours (default) |
| `24h` | Once a day |

After saving, apply the change:

```bash
sudo systemctl daemon-reload
sudo systemctl restart plaud-pipeline.timer
```

### Changing the LLM chunk size

Long meetings are split into chunks before being sent to the LLM. The default is 12 minutes. To change it, open `run_pipeline.sh`:

```bash
nano /plaud/run_pipeline.sh
```

Find the line for Step 3 and add `--chunk-duration`:

```bash
run_step 3 "Summarize transcripts with LLM" \
    python3 summarize_plaud_transcription.py --chunk-duration 10
```

---

## 6. Running the pipeline manually

To run the full pipeline immediately without waiting for the timer:

```bash
sudo systemctl start plaud-pipeline.service
```

To run a single step directly (useful for testing or re-running a failed step):

```bash
cd /plaud

python3 copy_plaud_recording.py      # Step 1 — download from Plaud
python3 transcribe_plaud_recording.py     # Step 2 — transcribe audio
python3 summarize_plaud_transcription.py     # Step 3 — summarise with LLM
python3 convert_to_word.py            # Step 4 — convert to Word
python3 upload_to_gdrive.py          # Step 5 — upload to Google Drive
```

To preview what a script would do without actually doing anything, add `--dry-run`:

```bash
python3 upload_to_gdrive.py --dry-run
```

---

## 7. The automatic schedule

The pipeline is controlled by two systemd unit files:

- **`plaud-pipeline.timer`** — triggers the pipeline on a schedule
- **`plaud-pipeline.service`** — defines how to run it (as which user, from which directory, etc.)

Key behaviour:

- Runs **2 minutes after every boot** (gives the network time to come up)
- Then repeats every **6 hours** (configurable — see Section 5)
- `Persistent=true` means if the server was off when a run was due, it will run immediately on the next boot — no missed runs

To check the timer status and see the next scheduled run:

```bash
systemctl list-timers plaud-pipeline.timer
```

---

## 8. Monitoring and logs

### Quick status check

```bash
# Is the timer active?
systemctl status plaud-pipeline.timer

# Did the last run succeed?
systemctl status plaud-pipeline.service
```

### Reading logs

Each pipeline run writes a timestamped log file to `/plaud/logs/`. The last 30 runs are kept automatically.

```bash
# List recent log files (newest first)
ls -lt /plaud/logs/

# Read the most recent log
cat $(ls -1t /plaud/logs/pipeline_*.log | head -1)

# Watch a run in real time (while it is happening)
journalctl -u plaud-pipeline -f

# Read the last run from the systemd journal
journalctl -u plaud-pipeline -n 100
```

### What a successful run looks like in the log

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[09:00:02] Pipeline started  —  Monday 07 April 2026 09:00:02

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[09:00:02] STEP 1 — Download recordings from Plaud
...
[09:00:08] STEP 1 complete (6s)

...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[09:04:31] Pipeline finished successfully  —  09:04:31
```

---

## 9. Troubleshooting common errors

### "Permission denied" when writing a file

The `/plaud/` directory is owned by root. Fix it once with:

```bash
sudo chown -R vtauzia:vtauzia /plaud
```

### "No bearer token provided" (Step 1)

Your Plaud token is missing or expired. Get a new one from DevTools (see Section 4.1) and update `/plaud/pipeline.env`.

### "Cannot reach Ollama" (Step 3)

The LLM server at `192.168.68.85:11434` is not responding. Check that the Ollama service is running on that machine. The pipeline will continue and Steps 4 and 5 will still run for any summaries that already exist.

### "Cannot reach [transcription API]" (Step 2)

The Whisper server at `192.168.68.85:8000` is not responding. Same situation — Steps 3–5 will still run for any transcripts that already exist.

### Google Drive upload fails with "invalid_client"

The `credentials.json` file is from the wrong Google Cloud project. Delete it, download a fresh one from the correct project in the **company** Google Cloud Console, and copy it to `/plaud/credentials.json`. Also delete `token.json` and redo the one-time browser authentication (Section 4.2).

### Google Drive upload fails with "Token refresh failed"

The `token.json` has expired or been revoked. Delete it and redo the one-time browser authentication (Section 4.2). You will need the SSH tunnel again.

### LLM output is garbled or contains `<|im_start|>` tokens (Step 3)

The model generated its own chat template tokens. This is handled automatically by the script, but if it persists, try reducing the chunk size:

```bash
python3 summarize_plaud_transcription.py --chunk-duration 10
```

### "pandoc not found" (Step 4)

Pandoc is not installed or not on the PATH. Install it:

```bash
sudo apt install pandoc
```

### Timer is not running after a reboot

The timer was not enabled. Run:

```bash
sudo systemctl enable plaud-pipeline.timer
sudo systemctl start  plaud-pipeline.timer
```

---

## 10. Maintenance tasks

### Renewing the Plaud bearer token (~every 10 months)

The token lasts approximately 300 days. When Step 1 starts failing with a 401 error, get a new token from DevTools (Section 4.1) and update `/plaud/pipeline.env`.

### Renewing the Google Drive token

`token.json` is refreshed automatically and should not expire as long as the pipeline runs at least once every few months. If it does expire, delete it and redo Section 4.2.

### Freeing up disk space

Audio files in `/plaud/recordings/` are the largest files. Once you have confirmed that all recordings have been transcribed and uploaded, you can safely delete them:

```bash
# Check what is in recordings/
ls -lh /plaud/recordings/

# Remove audio files older than 30 days
find /plaud/recordings/ -name "*.m4a" -mtime +30 -delete
find /plaud/recordings/ -name "*.opus" -mtime +30 -delete
```

Log files are automatically trimmed to the last 30 runs. All other output folders (`transcripts/`, `summaries/`, `transfer_out/`) can be archived or cleared manually at any time — the pipeline will not re-process files that have already been uploaded.

### Updating a Python script

Copy the new version of the script to `/plaud/` (overwriting the old one). No restart of the timer is needed — it simply uses whatever version of the script is on disk at the time of the next run.

### Stopping the pipeline temporarily

```bash
# Stop and disable the timer (survives reboot — will NOT restart automatically)
sudo systemctl stop    plaud-pipeline.timer
sudo systemctl disable plaud-pipeline.timer

# Re-enable it later
sudo systemctl enable plaud-pipeline.timer
sudo systemctl start   plaud-pipeline.timer
```

---

*Last updated: April 2026*



