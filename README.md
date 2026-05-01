<div align="center">

# 🎙️ PlaudPipeline

**Self-hosted transcription & automation pipeline for Plaud Note devices**

*Replace Plaud's $20/month AI subscription with your own infrastructure*

</div>

---

## What is this?

PlaudPipeline is a self-hosted alternative to Plaud's cloud AI features. 

**Automation pipeline** — a set of Python scripts + systemd timer that runs every few hours on a Linux server, automatically downloading recordings from Plaud, transcribing them, generating meeting minutes with a local LLM, exporting to Word, and uploading to Google Drive

---

## Automation Pipeline

Six Python scripts that run in sequence, managed by a systemd timer.

| Script | What it does |
|--------|-------------|
| `copy_plaud_recording.py` | Downloads new audio from the Plaud API |
| `transcribe_recordings.py` | Sends audio to a local Whisper server |
| `summarize_transcripts.py` | Generates meeting minutes via a local Ollama LLM |
| `export_to_docx.py` | Converts Markdown summaries to Word files via Pandoc |
| `upload_to_gdrive.py` | Uploads `.docx` files to a Google Drive folder |
| `delete_processed_recordings.py` | Cleans up fully-processed recordings (dry-run by default) |

### Setup

1. Copy all files to `/plaud/` on your Linux server
2. Edit `pipeline.env` with your credentials:
   ```
   GDRIVE_FOLDER_ID=your_drive_folder_id
   PLAUD_TOKEN=your_bearer_token
   ```
3. Install the systemd units:
   ```bash
   sudo cp plaud-pipeline.service plaud-pipeline.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now plaud-pipeline.timer
   ```

The pipeline runs every 6 hours and logs to `/plaud/logs/`.

### First-time Google Drive auth

The first run of `upload_to_gdrive.py` requires an interactive OAuth flow. With an SSH tunnel:

```bash
ssh -L 8080:localhost:8080 user@your-server
python3 /plaud/upload_to_gdrive.py
```

Follow the URL printed in the terminal. After this, `token.json` is saved and renewals are automatic.

Full setup and maintenance instructions: [PLAUD_PIPELINE_GUIDE.md](PLAUD_PIPELINE_GUIDE.md)

---

## Requirements


**Pipeline**: Python 3.11+, Pandoc, a Whisper-compatible HTTP server, Ollama with a chat model, Google Cloud OAuth2 credentials

