#!/bin/bash
# =============================================================================
# Plaud Pipeline — orchestration script
# Runs all 6 steps in sequence. Each step is logged individually.
# A failed step is recorded but does not abort the remaining steps,
# since later steps may still have work to do from previous runs.
#
# DELETION SAFETY SWITCH
# ----------------------
# Step 6 (delete processed recordings) is kept in DRY-RUN mode by default.
# This means it will log what it would delete but never actually delete
# anything until you are confident the full pipeline is working correctly.
#
# Once you have verified several successful end-to-end runs and confirmed
# that files are appearing correctly in Google Drive, change the line below:
#
#   DELETE_DRY_RUN="--dry-run"   ← safe default, nothing is ever deleted
#   DELETE_DRY_RUN=""            ← live mode, deletions actually happen
#
# DELETE_DRY_RUN="--dry-run"
DELETE_DRY_RUN=""

# Minimum age (in days) before a fully-processed recording can be deleted.
# Even in live mode, files newer than this are never touched.
DELETE_MIN_AGE=3
# =============================================================================

PIPELINE_DIR="/plaud"
LOG_DIR="/plaud/logs"
DATE_TAG="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="$LOG_DIR/pipeline_${DATE_TAG}.log"
MAX_LOG_FILES=30        # keep the last 30 runs 

mkdir -p "$LOG_DIR"

# Tee all output to the log file and to the systemd journal simultaneously
exec > >(tee -a "$LOG_FILE") 2>&1

cd "$PIPELINE_DIR"

# ── helpers ──────────────────────────────────────────────────────────────────
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
rule() { echo ""; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }

FAILED_STEPS=()

run_step() {
    local num="$1"
    local label="$2"
    shift 2          # remaining args are the command

    rule
    log "STEP $num — $label"
    echo ""

    local t0=$SECONDS
    "$@"
    local rc=$?
    local elapsed=$(( SECONDS - t0 ))

    echo ""
    if [ $rc -eq 0 ]; then
        log "STEP $num complete (${elapsed}s)"
    else
        log "STEP $num FAILED with exit code $rc (${elapsed}s) — continuing pipeline"
        FAILED_STEPS+=("$num: $label")
    fi
}

# ── pipeline ─────────────────────────────────────────────────────────────────
rule
log "Pipeline started  —  $(date '+%A %d %B %Y %H:%M:%S')"

run_step 1 "Download recordings from Plaud" \
    python3 copy_plaud_recording.py

run_step 2 "Transcribe audio recordings" \
    python3 transcribe_recordings.py

run_step 3 "Summarize transcripts with LLM" \
    python3 summarize_transcripts.py

run_step 4 "Export summaries to Word (.docx)" \
    python3 export_to_docx.py

run_step 5 "Upload .docx files to Google Drive" \
    python3 upload_to_gdrive.py

run_step 6 "Delete fully-processed recordings" \
    python3 delete_processed_recordings.py \
        --min-age "$DELETE_MIN_AGE" \
        $DELETE_DRY_RUN

# ── final report ─────────────────────────────────────────────────────────────
rule
if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
    log "Pipeline finished successfully  —  $(date '+%H:%M:%S')"
else
    log "Pipeline finished WITH ERRORS in the following steps:"
    for s in "${FAILED_STEPS[@]}"; do
        log "  ✗  $s"
    done
    log "Check the log above for details."
fi
echo ""

# ── log rotation — keep only the last MAX_LOG_FILES files ────────────────────
ls -1t "$LOG_DIR"/pipeline_*.log 2>/dev/null | tail -n +$(( MAX_LOG_FILES + 1 )) | xargs -r rm --