# Container Lambda scaffold — Verified Learning service.
#
# Build target: AWS Lambda container image (python3.12 base).
# The NLI cross-encoder model (~400 MB) is bundled at build time so cold-start
# does not require a network fetch.  The model artifact is pinned by revision
# (see PINNED_NLI_MODEL_REVISION) so a HuggingFace namespace-hijack / weight
# substitution that preserves label order cannot silently flip verdicts — the
# label-map assertion in nli.py guards order, pinned revision guards weights.
#
# Explicitly NOT installed: sqlite-vec, agent_families.store, agent_families.vecindex.
# The live loop writes DynamoDB + S3 Vectors (boto3); SQLite is the offline sandbox only.
#
# Usage (local smoke):
#   docker build -t learning-service .
#   docker run --rm -e LS_MODE=shadow learning-service learning-ingest --help
#
# Multi-stage build notes:
#   Stage 1 (builder): install deps + pre-fetch the NLI model into HF_HOME.
#   Stage 2 (runtime): copy venv + model cache; run as non-root (uid 1000).

FROM public.ecr.aws/lambda/python:3.12 AS builder

WORKDIR /build

# ---- system dependencies ----
RUN dnf install -y git && dnf clean all

# ---- Python dependencies (no sqlite-vec) ----
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "sentence-transformers[onnx]>=5.0"

# ---- install agent-families (store-free wheel, no sqlite-vec) ----
# Assumes agent-families is co-located in the repo; in CI this path is adjusted.
# Only nli.py / judge.py are used from this package; the install carries no
# sqlite-vec because we do NOT install the [sqlite] extra.
COPY ../../agent-families /build/agent-families
RUN pip install --no-cache-dir /build/agent-families

# ---- install learning-service itself ----
COPY . /build/learning-service
RUN pip install --no-cache-dir /build/learning-service

# ---- pre-fetch + pin the NLI model ----
# PINNED_NLI_MODEL_REVISION is set at build time via --build-arg.
# Override with: docker build --build-arg PINNED_NLI_MODEL_REVISION=<sha> .
ARG PINNED_NLI_MODEL_REVISION=main
ENV HF_HOME=/opt/hf_cache \
    HF_HUB_OFFLINE=0 \
    TRANSFORMERS_OFFLINE=0
RUN python - <<'EOF'
from sentence_transformers import CrossEncoder
import os

model_id = "cross-encoder/nli-deberta-v3-base"
revision = os.environ.get("PINNED_NLI_MODEL_REVISION", "main")
print(f"pre-fetching {model_id} @ {revision} into {os.environ['HF_HOME']}")
# This triggers the HuggingFace download + caches the model weights.
CrossEncoder(model_id, device="cpu", revision=revision)
print("model pre-fetch ok")
EOF

# ---- runtime stage ----
FROM public.ecr.aws/lambda/python:3.12 AS runtime

# Copy the installed packages and the pre-fetched model.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /opt/hf_cache /opt/hf_cache

# Runtime environment.
ENV HF_HOME=/opt/hf_cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    LS_MODE=shadow \
    LS_NLI_MODE=replay \
    LS_JUDGE_MODE=replay \
    PYTHONUNBUFFERED=1

# Lambda handler for the deferred webhook receiver.
CMD ["learning_service.entrypoints.webhook.handler"]
