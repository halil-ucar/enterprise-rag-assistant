# ── Run modes ─────────────────────────────────────────────────────────────────
# make up    → everything in containers (reproducible anywhere; inference on CPU)
# make dev   → postgres+redis in containers, api+worker native (Apple Silicon: MPS)
# See docs/SETUP-MACOS.md for why both modes exist and which one your Mac needs.

.PHONY: up refresh start down dev dev-api dev-worker seed seed-container seed-core seed-full \
        eval eval-core eval-full check-golden test test-all test-sovereign lint type \
        demo-offline logs psql backup restore-check

up:            ## full container stack (CPU inference)
	docker compose up -d --build

refresh:       ## one command: pull latest code, rebuild, re-seed (full-container mode)
	git pull
	docker compose up -d --build
	docker compose exec -T api python seed/make_corpus.py
	docker compose exec -T api python seed/ingest_corpus.py --corpus smoke
	@echo ""
	@echo "Updated. UI: http://localhost:8000"

start:         ## just start what's already built (e.g. after a reboot) — no rebuild
	docker compose up -d

down:
	docker compose down

dev:           ## infra in containers, app native (run api+worker in two terminals)
	docker compose up -d postgres redis
	@echo ""
	@echo "Infra is up. Now run in two terminals:"
	@echo "  make dev-api      # FastAPI with reload"
	@echo "  make dev-worker   # arq ingestion worker"

dev-api:
	uv run uvicorn rag_assistant.api:app --reload --port 8000

dev-worker:
	uv run arq rag_assistant.worker.WorkerSettings

seed:          ## smoke corpus (12 docs) — CI plumbing, fast tests
	uv run python seed/make_corpus.py
	uv run python seed/ingest_corpus.py --corpus smoke

seed-container: ## smoke corpus, seeded INSIDE the api container (full-container mode; Intel macOS)
	docker compose exec -T api python seed/make_corpus.py
	docker compose exec -T api python seed/ingest_corpus.py --corpus smoke

seed-core:     ## curated core (designed hard negatives) — retrieval QUALITY
	uv run python seed/core_spec.py
	uv run python seed/ingest_corpus.py --corpus core --reset

seed-full:     ## core + generated haystack (scale) — set FILL=<n> (default 2000)
	uv run python seed/core_spec.py
	uv run python seed/make_fill_corpus.py -n $(or $(FILL),2000)
	uv run python seed/ingest_corpus.py --corpus full --reset --quiet

check-golden:  ## verify every core golden anchor resolves against the ingested corpus
	uv run python seed/check_golden.py --golden seed/golden_set_core.yaml

eval:          ## smoke ablation (writes eval/runs/<ts>.json)
	uv run python eval/run_eval.py

eval-core:     ## ablation on the core golden set (per-category breakdown)
	uv run python eval/run_eval.py --golden seed/golden_set_core.yaml

eval-full:     ## same golden set, but scored against the full haystack index
	uv run python eval/run_eval.py --golden seed/golden_set_core.yaml

test:          ## fast unit tests (no DB, no models — fakes only)
	uv run pytest

test-all:      ## unit + integration (needs `make dev` infra running)
	uv run pytest -m ""

test-sovereign: ## sovereign protocol proof against a live local Ollama /v1 (not in CI)
	uv run pytest -m ollama -v -s

lint:
	uv run ruff check src tests eval seed
	uv run ruff format --check src tests eval seed

type:
	uv run mypy

demo-offline:  ## fully-local profile (no cloud calls) — native mode; needs a seeded DB + Ollama
	docker compose up -d postgres redis
	@echo ""
	@echo "Offline profile: EVERY LLM call routes to local Ollama, no cloud fallback."
	@echo "Seed first if empty (make seed). Ctrl-C to stop the API."
	@echo ""
	RAG_PROFILE=offline uv run uvicorn rag_assistant.api:app --reload --port 8000
# Container mode (Intel Mac): set RAG_PROFILE=offline in .env, then `make up` — the
# api service reads it via env_file. The previous `RAG_PROFILE=offline $(MAKE) dev`
# never reached the API (the separate dev-api shell didn't inherit the variable).

logs:
	docker compose logs -f --tail=100

psql:
	docker compose exec postgres psql -U rag_owner -d rag

backup:        ## pg_dump (custom format) into backups/ + git-rev sidecar (config state of the dump)
	mkdir -p backups
	docker compose exec -T postgres pg_dump -U rag_owner -d rag -Fc -f /tmp/rag.dump
	TS=$$(date +%Y%m%d-%H%M%S); \
	docker compose cp postgres:/tmp/rag.dump backups/rag_$$TS.dump; \
	git rev-parse HEAD > backups/rag_$$TS.rev.txt; \
	echo "backup written: backups/rag_$$TS.dump (+ .rev.txt)"

restore-check: ## prove restorability: dump -> throwaway restore -> count compare (inside the postgres container)
	docker compose exec -T -e PGUSER=rag_owner -e PGDATABASE=rag postgres bash < scripts/restore_check.sh
