# macOS setup — native dev mode (Apple Silicon)

Zero-to-running on a fresh Mac. Every block is copy-paste; checkpoints tell you what
you should see. Native mode runs the API and models on the host (MPS inference — the
reranker drops from ~28 s/query on container CPU to sub-second) while Postgres and
Redis run in containers.

> **Intel Mac (no Apple Silicon)?** The current PyTorch build ships no macOS x86_64
> wheel, so `uv sync --extra ml` (step 3) fails with a "no wheel for the current
> platform" error. That's expected — use the **full-container path** instead, where
> the models run in a Linux container (CPU), independent of the host CPU. Jump to
> [§A · Full-container mode](#a--full-container-mode-intel-mac--any-host-without-a-torch-wheel)
> and skip steps 3–8.

## 0 · Tools (once)

```bash
brew --version || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install uv
brew install --cask docker
```

Start **Docker Desktop** once from Applications and wait for the whale icon in the
menu bar. (Postgres and Redis need it; nothing else does.)

## 1 · Clone

```bash
cd ~
git clone https://github.com/halil-ucar/enterprise-rag-assistant.git
cd enterprise-rag-assistant
```

## 2 · Secrets (`.env` — git-ignored, never leaves the machine)

```bash
cp .env.example .env
nano .env
```

Fill in at minimum `OPENAI_API_KEY=`. Save: `Ctrl-O`, `Enter`, exit: `Ctrl-X`.

## 3 · Python environment (first run downloads PyTorch — a few minutes)

```bash
uv sync --extra ml
```

## 4 · Infrastructure

```bash
make dev
```

Checkpoint: `docker ps` shows a postgres and a redis container.

## 5 · Corpus (first run downloads BGE-M3, ~2.3 GB)

```bash
make seed
```

Checkpoint: 12 lines showing status `created` (each ends `via md` or `via pdf(docling)`),
then `seed complete`.

## 6 · API

```bash
make dev-api
```

Open <http://localhost:8000> — the UI loads; the live pill turns green. Leave this
terminal running. (The arq worker — `make dev-worker` in another terminal — is only
needed for uploads via `POST /ingest`; the seeded demo works without it.)

## 7 · Verify the real model IDs (placeholders ship in the config)

New terminal tab, back in the project folder:

```bash
uv run --env-file .env python -c \
  "from openai import OpenAI; [print(m.id) for m in OpenAI().models.list().data if 'gpt' in m.id]"
```

Pick the current small/cheap model for `OPENAI_MODEL_MINI` and a stronger one for
`OPENAI_MODEL_STRONG`, write both into `.env`, restart `make dev-api`. First question
in the UI ⇒ the glass-box line shows the real provider/model instead of a fake.

## 8 · Answer-layer eval (real LLM: quality, refusal contract, injection, latency SLOs)

```bash
RUN_MODE=dev uv run --env-file .env python eval/run_eval.py --answers
```

Retrieval-only ablations (no LLM cost): `make eval` (smoke) · `make seed-core &&
make check-golden && make eval-core` (hard-negative corpus) · `make seed-full FILL=1500 &&
make eval-full` (the 5 826-chunk scale index behind the README numbers; the default
`FILL=2000` builds a larger index and different numbers).

## 9 · Optional: local generation (confidential path + offline demo)

```bash
brew install ollama
ollama pull qwen3:8b        # ~5 GB
make demo-offline           # forces the fully-local profile — works without internet
```

## A · Full-container mode (Intel Mac / any host without a torch wheel)

Everything — API, embeddings, reranker — runs in Linux containers on CPU. No host
Python environment is needed. Do steps 0–2 (tools, clone, `.env`) as above, make sure
**Docker Desktop is running**, then:

```bash
make up                 # builds the image (first time pulls the CPU torch wheel — a few minutes)
```

Checkpoint: `docker compose ps` shows `postgres`, `redis`, `api`, `worker` all up.

```bash
make seed-container      # generates + ingests the 12-doc smoke corpus INSIDE the api container
                         # (first run downloads BGE-M3, ~2.3 GB, cached in a volume afterwards)
```

Checkpoint: 12 lines showing status `created` (each ends `via md` or `via pdf(docling)`),
then `seed complete`.

Open <http://localhost:8000> — the UI loads and the live pill turns green.

**Real model IDs** (the config ships placeholders — same as step 7, but run it in the
container since there is no host venv):

```bash
docker compose exec api python -c \
  "from openai import OpenAI; [print(m.id) for m in OpenAI().models.list().data if 'gpt' in m.id]"
```

Put a current small model in `OPENAI_MODEL_MINI` and a stronger one in
`OPENAI_MODEL_STRONG` in `.env`, then `docker compose up -d api` to pick up the change.

**First question note:** the CPU reranker loads a second model on the first query and
runs ~28 s; the answer cache makes repeats instant. For a snappy live demo, pre-ask
your demo questions once beforehand, or set `RERANKER_BACKEND=fake` in `.env` (retrieval
+ real LLM answer stay real; only the cross-encoder re-ranking step is skipped).

**Eval in the container** (optional): `docker compose exec api python eval/run_eval.py --answers`.

Stop everything with `make down` (the model cache and DB survive in named volumes).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `cd: no such file or directory` | Step 1 not done — the repo isn't cloned yet. |
| `docker: command not found` / compose hangs | Docker Desktop not installed or not RUNNING (whale icon). |
| `port 5432 already in use` on `make dev` | A local Postgres is running: `brew services stop postgresql` (or change the port mapping in `docker-compose.yml` AND `DATABASE_URL`). |
| `port 8000 already in use` | Another dev server: `lsof -ti :8000 \| xargs kill`. |
| Model download very slow / fails | Hugging Face hiccup — rerun `make seed`; downloads resume from cache (`~/.cache/huggingface`). |
| `401` from OpenAI | Key typo in `.env`, or the shell env overrides it — check `printenv OPENAI_API_KEY`. |
| `model not found` on first question | Step 7 skipped — the config still has placeholder model IDs. |
| UI answers but citations look wrong | DB from an older corpus state: `make seed` re-ingests idempotently; changed docs bump the corpus version and invalidate caches. |
