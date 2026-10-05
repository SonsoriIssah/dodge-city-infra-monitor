# Build handover (internal — delete `docs/internal/` before merging to `main`)

This branch (`feature/full-stack-prototype`) upgrades the original static prototype into the full system:
GIS data → PostGIS → Python pipeline → simulated sensors → anomaly detection → spatial analysis → FastAPI → MapLibre
3D dashboard. Read, in this order:

1. this file;
2. `docs/internal/build-contract.md` — the engineering contract every stage was built against. Section 14
   (Amendments) overrides earlier text;
3. `docs/internal/requirements.md` — the requirements checklist R1–R23 and the final-output list (acceptance standard).

**All build stages are finished.** This file supersedes `.claude/build/STATUS.md` on the workstation. Do not run
`docs/internal/stage-prompts.js` or `.claude/build/workflows/build-remaining.js` again: the workflow runs all four
stages unconditionally and would rebuild finished work. Keep them for reference only.

## Ground rules
- Data honesty is the core requirement. Simulated is never presented as real. The labels "Simulated Sensor Data",
  "Prototype Anomaly Detection" and "Derived Asset Health Score" are mandatory; the words "live" and "real-time" are
  not used to describe this system. Real attributes come only from OpenStreetMap tags and FHWA National Bridge
  Inventory fields. Isolation Forest wording follows amendment A5.
- Work on `feature/full-stack-prototype`. Do not merge to `main`, push to `main` or deploy unless the owner asks.
  Never commit credentials; `.env` is git-ignored and `.env.example` holds placeholders only.
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
| Docker: `backend/entrypoint.py`, `backend/Dockerfile`, `backend` service in `docker-compose.yml`, `.dockerignore` | done; entrypoint verified on the host against a fresh PostGIS (seed 25 s, restart 1.6 s, failure exit 2), including a password with special characters; image built and run end to end on the Windows workstation on 2026-10-05 (isolated project on an empty volume: seed 8 s, `/statistics` = default dataset; also against the existing volume without re-seeding) |
| CI: `.github/workflows/ci.yml` (`pages.yml` untouched) | done; green on GitHub Actions (ruff, 1,523 pytest against PostGIS, 84 node) |
| `README.md` (20 sections) and `docs/*.md` (6 guides) | done; numbers checked against GET /meta, links checked |
| Requirements audit and final review | done; status-sentence threshold made visible; `process()` length listed as tech debt; dashboard checked end to end in a browser (API and static mode) |

Default dataset (seed 42) — use these only via the API or the snapshot, never hard-code them in the UI:
706 assets (678 real + 28 simulated water mains), 112 monitored, 128 sensors, 92,028 readings, 42 anomalies
(3 critical / 17 high / 17 medium / 5 low; 6 active at the final hour), 3 clusters, 88 risk cells; at the final hour
126 sensors reporting, 2 offline, 1 critical alert, 6 assets at risk; building heights 419 LiDAR-measured, 39
estimated. Detection self-check on the simulated ground truth: recall 40/40, precision 0.952 (2 false, both low).
All of these are in `dashboard/data/snapshot/meta.json` and `playback.json` without needing a database.

## Continuing on the Windows workstation
The Docker/CI, docs and review work was done in a cloud session on branch `claude/vibrant-gates-1wsc1f`
(pull request #1 into `feature/full-stack-prototype`).

1. **Get the work.** Check `git status` first (commit or set aside local changes). If PR #1 is merged:
   `git checkout feature/full-stack-prototype` then `git pull`. If not:
   `git fetch origin` then `git checkout feature/full-stack-prototype` then
   `git merge --ff-only origin/claude/vibrant-gates-1wsc1f`.
2. **Replace the old status file** so a local session does not redo finished stages (Git Bash):
   `cp docs/internal/HANDOVER.md .claude/build/STATUS.md`
3. **Nothing to reinstall or reseed.** Requirements, migrations (`001`, `002`), pipeline code and the committed
   snapshot did not change, so the existing `.venv`, `.env` and the `dodge-city-infra_pgdata` volume keep working.
   On this machine `python`/`py` on PATH are broken: use `.venv/Scripts/python.exe`.
4. **`.env`:** no change needed. `POSTGRES_PASSWORD` may contain any character; wrap it in single quotes if it
   contains `$`, `#` or spaces. Compose passes the connection to the backend as separate parts, not a URL.
5. **First checks:**
   - `.venv/Scripts/python.exe -m pytest -q -m "not slow"` → expect 1,523 passed (needs the db container).
   - `node --test dashboard/tests` → expect 84 passed.
   - `docker compose up --build` → open http://localhost:8000. Against the existing volume the backend logs
     "data present ... AUTO_SEED not needed" and serves at once. Stop any uvicorn already on port 8000, or set
     `API_PORT`. For a from-scratch check without touching the volume:
     `POSTGRES_HOST_PORT=5544 API_PORT=8044 docker compose -p dcim-verify up --build`, open http://localhost:8044
     (first start seeds for about 25 s), then `docker compose -p dcim-verify down -v`.

## Environment setup in a fresh checkout (any machine)
```bash
python3.12 -m venv .venv            # Windows: py -3.12 -m venv .venv
. .venv/bin/activate                # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt && pip install -e . --no-deps
cp .env.example .env                # then set POSTGRES_PASSWORD
docker compose up -d db             # PostGIS on host port 5433
python run_pipeline.py --skip-download
```
Without Docker: install PostgreSQL 16 + PostGIS 3.4, create a role and database matching `.env`, then run the
pipeline. Without any database: `pytest -q -m "not db and not slow"` (846 tests) and `node --test dashboard/tests`;
CI runs the database tests.

## What is left
Owner decisions and checks:
1. Done on the workstation, 2026-10-05: pipeline, 1,523 + 2 slow pytest, 84 node tests, ruff,
   `docker compose up --build` (isolated project and the default one), browser walk-through at 1366x768 in API mode
   (served by the container) and static mode, basemap fallback, the README's PowerShell command variants. No code
   defects found; README and `docs/deployment.md` were updated (Docker verification status, password wording).
2. Merge PR #1 into `feature/full-stack-prototype`; later merge to `main` (redeploys GitHub Pages with the new
   dashboard) after deleting `docs/internal/`.
3. Not re-verified: a full `python run_pipeline.py --refresh` against the upstream download services, and the
   `docker run` / remote-host commands in `docs/deployment.md`.

Optional work, smallest first:
- CI runs twice per push to a PR branch (`push` and `pull_request`); restrict `push` to `main` and
  `feature/full-stack-prototype` if Actions minutes matter.
- A pytest module for `backend/entrypoint.py` (verified end to end by hand only).
- Split `pipeline/gis/process.py::process()` (~436 lines) into per-layer functions.
- Automate the repair of an interrupted first seed (today: `python run_pipeline.py --only analyze`).

Known and documented, not open work: README sections 15 and 20 (simulated data, retrospective detection, the
three wrong LiDAR heights, the excluded US-56 NBI record, `meta.json`/`manifest.json` churn per amendment A2).
