#!/bin/bash
# Wait for GitHub rate limit to reset, then run ingestion for both repos.
set -e

REPO_ROOT="C:/Users/mattd/Documents/gauntlet/workflow_harness"
PYTHON="$REPO_ROOT/.venv/Scripts/python.exe"
RESET_AT=1781378279

echo "=== Waiting for GitHub rate limit reset at epoch $RESET_AT ==="
while true; do
    NOW=$(date +%s)
    REMAINING=$((RESET_AT - NOW + 10))
    if [ "$REMAINING" -le 0 ]; then
        echo "Rate limit should be reset now."
        break
    fi
    echo "$(date): Sleeping ${REMAINING}s until reset..."
    if [ "$REMAINING" -gt 60 ]; then
        sleep 60
    else
        sleep "$REMAINING"
    fi
done

echo ""
echo "=== Checking rate limit ==="
curl -s https://api.github.com/rate_limit

echo ""
echo "=== Running puzzle/okr ingest ==="
AF_NLI_MODE=passthrough AF_JUDGE_MODE=passthrough \
    "$PYTHON" -m learning_service.entrypoints.run_real_ingest \
    --org puzzle --repo puzzle/okr --max-prs 6 --mode enforce 2>&1
echo "puzzle/okr EXIT: $?"

echo ""
echo "=== Checking rate limit after puzzle/okr ==="
curl -s https://api.github.com/rate_limit

echo ""
echo "=== Running makeplane/plane ingest ==="
AF_NLI_MODE=passthrough AF_JUDGE_MODE=passthrough \
    "$PYTHON" -m learning_service.entrypoints.run_real_ingest \
    --org makeplane --repo makeplane/plane --default-branch preview --max-prs 6 --mode enforce 2>&1
echo "makeplane/plane EXIT: $?"

echo ""
echo "=== Final rate limit ==="
curl -s https://api.github.com/rate_limit
