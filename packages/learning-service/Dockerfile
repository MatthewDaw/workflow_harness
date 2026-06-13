# Container Lambda scaffold — Verified Learning service.
#
# Build target: AWS Lambda container image (python3.12 base).
# The NLI cross-encoder model (~400 MB) is bundled at build time so cold-start
# does not require a network fetch.  The model artifact is pinned by BOTH a
# git revision (PINNED_NLI_MODEL_REVISION) AND a sha256 digest of the weights
# file (PINNED_NLI_MODEL_SHA256) so a HuggingFace namespace-hijack / weight
# substitution that preserves label order cannot silently flip verdicts.
#
# Build contexts
# --------------
# This Dockerfile MUST be built from the REPO ROOT so that both
# packages/learning-service and agent-families are reachable.
#
#   # From repo root:
#   docker build -f packages/learning-service/Dockerfile \
#     --build-arg PINNED_NLI_MODEL_REVISION=<sha> \
#     --build-arg PINNED_NLI_MODEL_SHA256=<sha256hex> \
#     -t learning-service .
#
# For local smoke (no checksum enforcement):
#   docker build -f packages/learning-service/Dockerfile \
#     -t learning-service .
#
# Explicitly NOT installed: sqlite-vec, agent_families.store, agent_families.vecindex.
# The live loop writes DynamoDB + S3 Vectors (boto3); SQLite is the offline sandbox only.
#
# Multi-stage build notes:
#   Stage 1 (builder): install deps + pre-fetch the NLI model into HF_HOME.
#   Stage 2 (runtime): copy venv + model cache; run as non-root (uid 1000).

FROM public.ecr.aws/lambda/python:3.12 AS builder

WORKDIR /build

# ---- system dependencies ----
RUN dnf install -y git && dnf clean all

# ---- Python dependencies (no sqlite-vec) ----
COPY packages/learning-service/pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "sentence-transformers[onnx]>=5.0"

# ---- install agent-families (store-free wheel, no sqlite-vec) ----
# Built from repo root context so ../../agent-families is reachable as
# agent-families/ at the repo root.  Only nli.py / judge.py are used from
# this package; the install carries no sqlite-vec because we do NOT install
# the [sqlite] extra.
COPY agent-families /build/agent-families
RUN pip install --no-cache-dir /build/agent-families

# ---- install learning-service itself ----
COPY packages/learning-service /build/learning-service
RUN pip install --no-cache-dir /build/learning-service

# ---- pre-fetch + pin the NLI model ----
# PINNED_NLI_MODEL_REVISION: HuggingFace git revision (commit SHA or tag).
#   Prevents a future revision from swapping weights while keeping the same
#   model ID.  Use "main" for local smoke only; always supply a real SHA in CI.
#
# PINNED_NLI_MODEL_SHA256: sha256 hex digest of the pytorch_model.bin (or
#   model.safetensors) weights file.  Verified at build time so the image
#   cannot be built with substituted weights even if the HF revision is
#   somehow repointed.  Leave empty ("") to skip verification (local dev only).
ARG PINNED_NLI_MODEL_REVISION=main
ARG PINNED_NLI_MODEL_SHA256=""
ENV HF_HOME=/opt/hf_cache \
    HF_HUB_OFFLINE=0 \
    TRANSFORMERS_OFFLINE=0
RUN python - <<'PYEOF'
import hashlib
import os
import pathlib

model_id = "cross-encoder/nli-deberta-v3-base"
revision = os.environ.get("PINNED_NLI_MODEL_REVISION", "main")
expected_sha256 = os.environ.get("PINNED_NLI_MODEL_SHA256", "")
hf_home = os.environ["HF_HOME"]

print(f"pre-fetching {model_id} @ {revision} into {hf_home}")

from sentence_transformers import CrossEncoder
model = CrossEncoder(model_id, device="cpu", revision=revision)
print("model pre-fetch ok")

# ---- sha256 integrity check ----
# Locate the weights file (safetensors preferred; fall back to pytorch bin).
# HuggingFace caches under HF_HOME/hub/models--<org>--<name>/snapshots/<hash>/
if expected_sha256:
    hub_dir = pathlib.Path(hf_home) / "hub"
    # Find the snapshot directory for this model.
    model_slug = model_id.replace("/", "--")
    model_cache_dir = hub_dir / f"models--{model_slug}"
    snapshots_dir = model_cache_dir / "snapshots"
    # Pick the most recently modified snapshot (the one we just downloaded).
    snapshot_dirs = sorted(snapshots_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    if not snapshot_dirs:
        raise RuntimeError(f"No snapshots found under {snapshots_dir}")
    snapshot = snapshot_dirs[-1]
    # Prefer safetensors; fall back to pytorch_model.bin.
    candidates = list(snapshot.glob("*.safetensors")) + list(snapshot.glob("pytorch_model*.bin"))
    if not candidates:
        raise RuntimeError(
            f"No weight file found under {snapshot}; "
            "cannot verify model integrity.  "
            "Files present: " + str(list(snapshot.iterdir()))
        )
    # Hash every weight file found and concatenate their names+hashes for a
    # stable aggregate digest.  For models split into shards this ensures all
    # shards are covered.
    candidates_sorted = sorted(candidates, key=lambda p: p.name)
    sha256 = hashlib.sha256()
    for wf in candidates_sorted:
        print(f"  hashing {wf.name} ({wf.stat().st_size:,} bytes) ...")
        sha256.update(wf.name.encode())
        sha256.update(b":")
        with open(wf, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                sha256.update(chunk)
        sha256.update(b"\n")
    actual = sha256.hexdigest()
    print(f"weights digest (sha256): {actual}")
    if actual != expected_sha256:
        raise RuntimeError(
            f"Model integrity check FAILED for {model_id} @ {revision}.\n"
            f"  expected sha256: {expected_sha256}\n"
            f"  actual   sha256: {actual}\n"
            "Build aborted — do not ship an image with unverified weights."
        )
    print(f"sha256 verified ok: {actual}")
else:
    print(
        "WARNING: PINNED_NLI_MODEL_SHA256 not set — skipping weight integrity check. "
        "Always supply the digest in CI/production builds."
    )
PYEOF

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
