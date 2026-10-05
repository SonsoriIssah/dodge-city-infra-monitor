# Build handover (internal — delete `docs/internal/` before merging to `main`)

This branch (`feature/full-stack-prototype`) upgrades the original static prototype into the full system:
GIS data → PostGIS → Python pipeline → simulated sensors → anomaly detection → spatial analysis → FastAPI → MapLibre
3D dashboard. Read, in this order:

1. this file;
2. `docs/internal/build-contract.md` — the engineering contract every stage was built against. Section 14
   (Amendments) overrides earlier text;
3. `docs/internal/requirements.md` — the requirements checklist R1–R23 and the final-output list (acceptance standard).

`docs/internal/stage-prompts.js` holds the detailed task descriptions for the stages below (`BUILD_DOCKER`,
`BUILD_DOCS`). It was written for a Windows workstation: ignore its absolute paths and the `.venv/Scripts/python.exe`
launcher, and treat its statements about a running local database as not applying here.

## Ground rules
- Data honesty is the core requirement. Simulated is never presented as real. The labels "Simulated Sensor Data",
  "Prototype Anomaly Detection" and "Derived Asset Health Score" are mandatory; the words "live" and "real-time" are
  not used to describe this system. Real attributes come only from OpenStreetMap tags and FHWA National Bridge
  Inventory fields. Isolation Forest wording follows amendment A5.
- Push only to `feature/full-stack-prototype`. Do not merge, open a pull request, push to `main` or deploy unless the
  owner asks. Never commit credentials; `.env` is git-ignored and `.env.example` holds placeholders only.
- Work sequentially and economically; verify what you write by running it.

## State of the build
| Stage | State |
|---|---|
| 1–3 download / process / seed (PostGIS schema, real GIS data, LiDAR-measured heights) | done, independently verified |
| 4 sensors (placement, simulator, source abstraction, ingestion) | done, independently verified |
| 5–6 detection + spatial analysis (clusters, risk zones, health) | done, independently verified |
| 7 API + static snapshot (`dashboard/data/snapshot/`, committed) | done; contract tests pass |
| Dashboard (shell + Assets / Anomalies / Sensors panels + charts) | done; verified in API mode and static mode |
| Tests | 1,523 pytest tests pass (`-m "not slow"`); 84 node tests pass; ruff clean |
| Docker: `backend/entrypoint.py`, `backend/Dockerfile`, `backend` service in `docker-compose.yml`, `.dockerignore` | done; entrypoint verified on the host against a fresh PostGIS (seed 25 s, restart 1.6 s, failure exit 2); **image build (`docker compose up --build`) not yet run** |
| CI: `.github/workflows/ci.yml` (`pages.yml` untouched) | done; steps emulated locally (ruff, 1,523 pytest, 84 node) — **first GitHub Actions run pending** |
| `README.md` (20 sections) and `docs/*.md` (6 guides) | done; numbers checked against GET /meta, links checked |
| Requirements audit and final review | done; status-sentence threshold made visible; `process()` length listed as tech debt; dashboard checked end to end in a browser (API and static mode) |

Default dataset (seed 42) — use these only via the API or the snapshot, never hard-code them in the UI:
706 assets (678 real + 28 simulated water mains), 112 monitored, 128 sensors, 92,028 readings, 42 anomalies
(3 critical / 17 high / 17 medium / 5 low; 6 active at the final hour), 3 clusters, 88 risk cells; at the final hour
126 sensors reporting, 2 offline, 1 critical alert, 6 assets at risk; building heights 419 LiDAR-measured, 39
estimated. Detection self-check on the simulated ground truth: recall 40/40, precision 0.952 (2 false, both low).
All of these are in `dashboard/data/snapshot/meta.json` and `playback.json` without needing a database.

## Environment setup in a fresh checkout
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e . --no-deps
cp .env.example .env            # then set POSTGRES_PASSWORD to a throwaway value
```
PostGIS, in order of preference:
1. Docker available → `docker compose up -d db` (host port 5433), then `python run_pipeline.py --skip-download`.
2. No Docker but packages can be installed → install PostgreSQL + PostGIS, create a superuser role and database
   matching `.env`, set `DATABASE_URL` / `TEST_DATABASE_URL`, then run the pipeline.
3. Neither → run `pytest -q -m "not db and not slow"` (712 tests, no database needed) and `node --test dashboard/tests`,
   and rely on the GitHub Actions run of `ci.yml` (PostGIS service container) for the database tests: push, then
   read the run with `gh run list` / `gh run view --log-failed`.
Say plainly in the final report which of these was possible and what therefore remains unverified.

## Remaining work, in order
1. **Docker + CI** (contract §13; `BUILD_DOCKER` in `stage-prompts.js`). Entrypoint: wait for the database, apply
   migrations, when `AUTO_SEED=true` and no finished detection run exists run the pipeline in-process
   (`--skip-download --skip-export`), then start uvicorn (`backend.app.main:create_app --factory`). Compose `backend`
   service sets `DATABASE_URL` explicitly to the in-network address (`@db:5432`) and does not use `env_file`.
   CI: ruff + pytest with `REQUIRE_DB=1` against `postgis/postgis:16-3.4` + node tests. Push and make the CI run green.
2. **README + docs** (contract §13 and A5; `BUILD_DOCS` in `stage-prompts.js`). README headings are exactly the 16
   titles of requirement R18, numbered 1–16 in order, then 17 Deployment, 18 What was reused (old path → new path),
   19 What is new, 20 Remaining limitations; Mermaid architecture diagram; `docs/{data-provenance,health-score,
   anomaly-detection,api,deployment,real-sensor-integration}.md`. Every command, path, endpoint, variable and number
   must be true — take them from the code, `pipeline/config.py`, `.env.example` and `meta.json`.
3. **Requirements audit** against `requirements.md` R1–R23 + final-output list; fix what is missing. Known items:
   - `pipeline/gis/process.py::process()` is ~430 lines — split it if time allows, otherwise list it as tech debt.
   - The status sentence says "K assets need attention" (health < 90) next to the KPI "Assets at Risk" (health < 70):
     make sure the wording cannot be read as a contradiction.
   - Three LiDAR heights do not describe the building (see `tools/README.md`) — must appear in README limitations.
   - `detection_runs.finished_at` is wall-clock, so `meta.json` and `manifest.json` change on every pipeline run (A2).
   - The US-56 NBI record with mismatched coordinates is deliberately excluded (documented in the processing report).
4. **Leave for the owner's workstation** (needs local Docker and a browser) and list as exact commands in the final
   report: `docker compose up --build` from a clean `.env`, then open http://localhost:8000 and walk through the
   dashboard; `pytest -q -m "not slow"`; `node --test dashboard/tests`.
5. **Final report** in plain language: what works and how it was verified, what could not be verified here, how to
   run it, what was reused from the original repository, what is new, remaining limitations, and the decisions that
   are the owner's: merge to `main` (which redeploys GitHub Pages with the new dashboard), and removal of
   `docs/internal/`.
