FROM python:3.12-slim

WORKDIR /app

# Model downloads must not phone home: disable Hugging Face telemetry for every
# process in the container (API, worker, seed/eval runs). Data-protection
# hygiene — test data is treated exactly like production data.
ENV HF_HUB_DISABLE_TELEMETRY=1

# CPU-only torch is the largest, slowest-changing dependency — install it in its
# OWN early layer, BEFORE any application code, so ordinary source edits don't
# re-download it on every rebuild (this layer is what made `make refresh` slow).
# The CPU index avoids the ~2 GB of NVIDIA CUDA libraries the default build drags
# in and this GPU-less container never uses; sentence-transformers then reuses
# the already-satisfied torch instead of pulling the CUDA build.
RUN pip install --no-cache-dir torch --extra-index-url https://download.pytorch.org/whl/cpu

# Dependency layer from pyproject ALONE: a stub package satisfies hatchling's
# editable install, so this layer survives every source edit — the editable
# install references /app/src by path, and the real code lands below.
# (PDF parsing via docling is optional; the worker falls back to Markdown.)
COPY pyproject.toml README.md ./
RUN mkdir -p src/rag_assistant && touch src/rag_assistant/__init__.py \
 && pip install --no-cache-dir -e ".[ml]"

# Real sources last: changing code invalidates only these cheap COPY layers.
COPY src ./src
COPY ui ./ui
COPY config ./config
# seed + eval scripts so the corpus can be seeded and evaluated INSIDE the
# container — the only ML-capable environment on hosts without a native torch
# wheel (e.g. Intel macOS). Copied last: they change most, invalidate least.
COPY seed ./seed
COPY eval ./eval

EXPOSE 8000
CMD ["uvicorn", "rag_assistant.api:app", "--host", "0.0.0.0", "--port", "8000"]
