export const meta = {
  name: 'build-remaining',
  description: 'Finish the dashboard (panels + charts), complete the test suite, package with Docker/CI, and write the documentation — one stage at a time',
  phases: [
    { title: 'Dashboard panels', detail: 'Assets / Anomalies / Sensors tabs, charts, frontend tests, whole-dashboard verification' },
    { title: 'Tests', detail: 'remaining API test modules, amendment A1, coverage' },
    { title: 'Docker + CI', detail: 'entrypoint, Dockerfile, compose backend service, CI workflow, verified docker compose up' },
    { title: 'Docs', detail: 'README (20 sections) and docs/' },
  ],
}

const ROOT = 'C:/Users/HP/OneDrive/Documents/dodge-city-infrastructure-monitor'
const SPEC = `${ROOT}/.claude/build/SPEC.md`
const BRIEF = `${ROOT}/.claude/build/BRIEF.md`

const RULES = `
GROUND RULES (apply to everything you do)
- The build contract is ${SPEC} — read it IN FULL before writing anything, including section 14 (Amendments), which
  overrides earlier text. The client's requirements are in ${BRIEF}. The contract wins over your preferences; if it is
  provably wrong, do the right thing AND list the deviation in your report.
- Repo root: ${ROOT}. ALWAYS use absolute paths or cd to the root inside every command (working directories drift).
  Python: .venv/Scripts/python.exe (python/py on PATH are broken). Windows; Git Bash or PowerShell. Node 24 available.
- NEVER git commit, push, stash, reset, checkout or create branches. NEVER print, log or commit the database password
  or the contents of .env.
- State of the project (verified today): pipeline stages 1-7 run end to end ('run_pipeline.py --skip-download', 47 s);
  the PostGIS database 'infra' (Docker container dodge-city-infra-db-1, localhost:5433) holds the full default dataset
  — do NOT run seed/generate/detect/analyze or run_pipeline against it. The pytest suite is green:
  '.venv/Scripts/python.exe -m pytest -q -m "not db and not slow"' → 712 passed (51 s) and '-m db' → 289 passed (53 s,
  uses its own database infra_test). Keep it green.
- Only touch the files your task owns unless told otherwise. Real code: no stubs, TODOs, placeholders, pseudocode.
- BE ECONOMICAL: this project runs under a tight usage limit. Read what you need, not everything; do not re-read
  files you already read; avoid dumping large files or long command output into your context (use head/tail/grep,
  wc, targeted reads); no exploratory detours. Write files to disk early and often so progress survives an interruption.
- Do not stop at "code written": run it, check the result, fix what breaks. Reports must contain the commands you
  actually ran and real output.`

const REPORT = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    files: { type: 'array', items: { type: 'string' }, description: 'files created / modified / deleted (prefix +, ~, -)' },
    evidence: { type: 'array', items: { type: 'object', properties: { command: { type: 'string' }, result: { type: 'string' } }, required: ['command', 'result'] } },
    numbers: { type: 'string' },
    deviations: { type: 'array', items: { type: 'string' } },
    open_issues: { type: 'array', items: { type: 'string' } },
    defects_found_in_existing_code: { type: 'array', items: { type: 'string' } },
  },
  required: ['summary', 'files', 'evidence', 'numbers', 'deviations', 'open_issues', 'defects_found_in_existing_code'],
}

// ------------------------------------------------------------------------------------------------ dashboard panels
const BUILD_PANELS = `You are the FRONTEND builder who finishes the Dodge City 3D Urban Infrastructure Monitoring dashboard:
the three right-panel tabs, the chart module, the frontend unit tests — and the verification of the whole dashboard.
${RULES}

WHAT EXISTS (written by a previous builder who was interrupted before reporting; I checked it runs):
- ${ROOT}/dashboard: index.html, config.js, css/{tokens,base,layout,components,map}.css, vendor/maplibre-gl (5.24.0),
  js/main.js, js/state/store.js, js/data/{provider,index}.js, js/map/{map,layers}.js,
  js/panels/{kpis,layers,about,intro,tabs}.js, js/timeline/timeline.js, js/ui/{format,tokens,dom}.js (~7,400 lines).
- Verified in static mode at 1366x768: title/badges/source badge, five KPI cards with data-driven numbers and the
  exact sub-captions, status sentence, intro card, tabs (Assets, Anomalies, Sensors), timeline, basemap + attribution,
  map canvas = 56 % of the viewport, no page scroll. The Assets tab currently shows its error state because
  js/panels/assets.js does not exist yet — that is your job. NOT yet verified by anyone: API mode, playback behaviour,
  layer toggles, selection/hover, basemap fallback, imagery/city-limits toggles, About dialog content, responsive
  layouts, keyboard shortcuts. You own all of dashboard/ now (except vendor/ and data/snapshot/, never edit those):
  fix shell defects where you find them and list them under defects_found_in_existing_code.
- READ FIRST: the panel-module interface comment at the top of dashboard/js/panels/tabs.js, then main.js, store.js,
  data/index.js, ui/dom.js, ui/format.js, ui/tokens.js and the CSS tokens. Use the existing helpers, tokens and DOM
  kit; do not fork them. Inspect the snapshot files in dashboard/data/snapshot/ for exact payload shapes (they follow
  SPEC 10/11): anomalies.json items already include nearby_assets; readings/<sensor_id>.json and
  health/<asset_id>.json are columnar.
- Run it both ways (start in the background, stop when finished; ports 8020/8021 may already be in use by a previous
  server — check with netstat and reuse or stop it):
  API mode:    cd ${ROOT} && .venv/Scripts/python.exe -m uvicorn backend.app.main:create_app --factory --port 8020
  static mode: cd ${ROOT} && .venv/Scripts/python.exe -m http.server 8021 -d dashboard
- Browser: load the built-in browser tools with ToolSearch (query "Claude_Browser"). PREFER javascript_tool,
  read_console_messages, find and get_page_text for checking; screenshots are expensive and may time out when the pane
  is hidden — take at most a handful, with scale 0.5. Check the console after every significant change.

YOUR SCOPE — SPEC 12.5 (the three tabs in full), 12.4 selection rules, 12.6 chart rules, 12.7 playback behaviour of
panels, 12.8 accessibility, 12.1 honesty labels, and the node tests of SPEC 13. Brief: R11, R12, R19, R22, R23.
Files to create: dashboard/js/ui/chart.js, dashboard/js/panels/{assets,anomalies,sensors}.js, dashboard/css/panels.css
(link it in index.html), dashboard/tests/*.test.js.
Before writing chart code, load the 'dataviz' skill with the Skill tool if it is available and follow it: one y-axis,
small multiples instead of dual axes, thin marks, recessive solid hairline grid, hover crosshair + tooltip, a table
twin, single series in the accent colour, status colours reserved for status.

Must-haves:
1. chart.js — dependency-free SVG time-series chart: observed line with gaps where readings are missing, expected line
   + expected band (expected_low/high), warn/crit threshold lines when inside the y-range (labelled), shaded anomaly
   windows, grey shading for benign simulated regional events labelled "Simulated regional event (benign — not
   flagged)" (getSimulationEvents, matched by sensor type), cursor at t with the part after t at 35 % opacity, hover
   crosshair + tooltip (time in the study-area zone, value, expected, z, flagged), keyboard focusable, "View as table"
   toggle, responsive width, tick formatting per unit (vibration needs 2-3 decimals), windowed mode (±48 h around an
   anomaly) and full-window mode, a compact sparkline variant for health history. Caption: "Simulated Sensor Data".
2. Assets tab — exactly SPEC 12.5: needs-attention ranking at t (monitored assets with health < 90, lowest first, max
   12; row = status pill WITH TEXT, name or id, type, health, active anomaly count; the specified empty state) and
   the detail view with the two titled blocks of SPEC 12.1. Recorded block: real OSM/NBI attributes with source and
   retrieval date from meta.data_sources; for bridges the NBI fields (structure number, year built / reconstructed,
   ADT with year, deck / superstructure / substructure / culvert ratings with their 0-9 meaning as text, the
   Good/Fair/Poor label verbatim from the data, inspection month/year, owner) — never on the same row or colour scale
   as the health score. Simulated block: health at t + the four penalty components (getAssetHealth) + band, sensor
   count, latest readings at t per sensor, anomaly count, recent anomalies as links, one small chart per sensor
   (max 4, small multiples), health sparkline. Unmonitored asset: recorded attributes + "Not monitored — no simulated
   sensors on this asset". Building height as "N m (measured from USGS 3DEP lidar | OSM levels | estimated)".
   Simulated water main: tagged simulated with the water-network label. Location = centroid lat/lon 5 dp + OSM addr:*
   tags when present, never a composed address.
3. Anomalies tab — the five filters with the SPEC defaults (severity multi, sensor type multi, status at t, asset
   select, date from/to) + Clear filters; list rules (started_at <= t; active first, then severity, then newest); row
   = severity pill, anomaly label, asset name, sensor id, started_at, status at t, "Simulated" tag; detail with EVERY
   field of SPEC 12.5 (ID, asset link, sensor link, type, severity, started/peak/ended, observed + unit, expected,
   score + components, detection method in words, explanation, nearby assets highlighted on the map with a radius
   ring through the shell's actions, cluster membership, ±48 h chart); subtitle "Prototype Anomaly Detection"; the
   specified empty state. Selecting an anomaly outside its active window moves the clock to peak_at. The KPI cards
   (Active Anomalies / Critical Alerts / Assets at Risk) already switch tab + set filters through the store — make
   sure your panels honour those filters.
4. Sensors tab — sensor type select (All + 4), asset select (monitored assets, "name (ID)"), list rows value + unit +
   status at t; detail: latest reading at t, trend vs t−24 h (arrow + rising/falling/steady with a dead-band),
   thresholds for the sensor's placement, the full history chart.
5. Everything follows the playback clock without flicker: while playing, lists re-render at most every 500 ms and
   charts only move their cursor (no refetch, no skeleton flash); immediately on pause. Lazy resources show a
   skeleton only on first load and an inline error with Retry on failure.
6. Accessibility: lists are <button> rows; filters are labelled form controls; a Back control returns focus to the
   originating row; Esc clears selection; text pills for every status/severity; charts have aria-labels + table twin.
7. dashboard/tests/*.test.js (node --test, no dependencies): pure helpers in js/data/index.js (status at t, active
   anomalies at t, every filter combination, ranking), js/ui/format.js (run under TZ=Africa/Accra and TZ=Asia/Tokyo:
   index 0 renders as Sep 1, 12:00 AM CDT; date/hour index mapping), provider mode resolution with a mocked fetch
   (auto → api; auto → static on HTML 200 / 404 / timeout / wrong service; forced api failure → error), store
   subscribe-by-keys, trend dead-band, chart scale/tick helpers, and the label test of SPEC 12.1 (the three R22 strings
   exist in the UI sources; no UI string contains "live" or "real-time").

VERIFY THE WHOLE DASHBOARD and report (both modes; console clean):
(a) load: Assets ranking matches playback at T_end (name the top three with scores); (b) lowest-health asset → detail
with all fields; (c) BRG-001 → recorded NBI block and simulated block clearly separate; (d) Anomalies: each filter
narrows correctly (give counts), open the critical anomaly → detail, map ring, chart; (e) Sensors: vibration + the
bridge → list, chart with band/thresholds/shaded windows, trend text; (f) Play from the start ~10 s: KPIs, building
colours, sensor circles, anomaly rings, risk hexes and panels update, no flicker, no console errors — report the
median and max duration of a playback tick; two concrete timestamps whose KPI values equal playback.stats;
(g) unmonitored building → empty state; (h) layer toggles, imagery, city limits, 3D/2D, Home, hex metric switch,
hover tooltip, About dialog (provenance table, method, metrics + evaluation note, A5 sentence, formulas, heights
wording with counts), "?" card, keyboard shortcuts; (i) basemap fallback: basemapStyleUrl pointing at an unreachable
URL → inline style + notice, data still drawn; (j) mode detection: API origin → "Source: PostGIS API"; static server
→ "Source: static snapshot (exported …)"; ?mode=static on the API origin; forced api on the static server → error
state with Retry and "Use static snapshot"; (k) layout at 1366x768 (map >= 50 %, no first-screen scroll — measure),
1920x1080, 768x1024, 390x844 (measure, describe); (l) static mode shows the same numbers as API mode.
Node tests: cd ${ROOT} && node --test dashboard/tests (also with TZ=Africa/Accra and TZ=Asia/Tokyo) — counts.
Finally confirm dashboard/ has no top-level entry that shadows an API route (SPEC 10.5).`

// ------------------------------------------------------------------------------------------------ tests
const BUILD_TESTS = `You are the TEST AUTHOR completing the pytest suite of the Dodge City prototype and applying amendment A1.
${RULES}

WHAT EXISTS: ${ROOT}/tests with conftest.py, support.py, fixtures/, and 19 passing modules (test_config, test_geo,
test_gis_process, test_placement, test_simulator, test_detectors, test_events, test_explain, test_evaluate, test_health,
test_risk, test_status, test_sources_ingestion, test_detection_targets_seeds [slow], test_migrations, test_pipeline_db,
test_spatial_sql, test_api_contract, test_zz_main_database_guard). READ conftest.py and support.py first and reuse
their fixtures (test database infra_test, pipeline session fixture, app/TestClient fixture); skim test_api_contract.py
to match its style. Do not rewrite passing modules.
Files you own: tests/**, pytest/coverage config in pyproject.toml, requirements-dev.txt; you may fix defects in
pipeline/** and backend/** that your tests expose (list each under defects_found_in_existing_code). Do NOT touch
dashboard/ and never regenerate dashboard/data/snapshot (export tests write to tmp_path).

TASKS
1. Amendment A1 (SPEC 14): stored anomalies.status = 'active' ⇔ ended_at >= T_end, else 'resolved'. Change
   pipeline/detection (events.py / runner.py — find where status is assigned), make infra.v_active_anomalies and
   sql/queries agree with the API's time rule (add sql/migrations/003_*.sql only if a view/function really has to
   change), add tests (unit + db). Because this changes pipeline output, re-check that on the default dataset stored
   status equals the time rule for every anomaly.
2. Write the missing modules (SPEC 13 + the list below). Assert the CONTRACT, recomputing expected values
   independently in the test (hand-written SQL through the test connection where useful):
   - test_api_time.py: as_of default = T_end, flooring to the hour, clamping below start / above T_end, naive = UTC,
     url-encoded offsets, invalid → 422, effective as_of echoed; anomaly status at as_of incl. hidden when started
     after as_of; EVERY ISO-looking string in EVERY endpoint response matches ^\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\dZ$.
   - test_api_parity.py: playback.stats[*][i] == /statistics?as_of=timestamps[i] on >= 5 indices spread over the
     window (incl. first and last); playback sensor status chars == /sensors?as_of=; playback asset health/status ==
     /assets/{id}/health and /assets/{id}?as_of=; playback zones == /spatial/risk-zones?as_of=; /statistics equals
     independent SQL for 3 timestamps.
   - test_api_errors.py: 404 for unknown ids on every {id} route; 422 for every invalid parameter (bad enum, negative
     / oversized limit, radius 0 / huge, malformed bbox, missing sensor_id on /sensor-readings); ids containing SQL
     metacharacters (' OR 1=1 --) → 404/422 and the tables are intact; 405 for POST on GET-only paths; unknown GET path
     → 404 JSON; with a dead DSN (create_app with settings pointing at a closed port) the app still starts,
     /health → 503 {status:"degraded"}, data endpoints → 503 {"detail":"database unavailable"}, and no response body
     or log line contains the DSN or password.
   - test_api_ingest.py: INGEST_API_KEY empty → 404 with the specified detail; wrong / missing key → 401; valid key →
     rows stored via IngestionService with source 'api' (on infra_test), validation reasons returned, oversized body
     rejected, idempotent upsert, detection NOT triggered (detection_runs unchanged).
   - test_api_static.py: '/' serves the dashboard index; /config.js is the API route with mode 'api'; mimetypes for
     .js and .geojson; SERVE_DASHBOARD=false → no mount and no /config.js; CORS header for an allowed origin; gzip on a
     large response; ETag + If-None-Match → 304 on /playback; /docs and /openapi.json work.
   - test_export_snapshot.py: export to tmp_path twice — file list exactly per SPEC 11 (readings for every sensor,
     health for monitored assets only), every file byte-identical to the corresponding API response, stable between
     runs except the files amendment A2 names, manifest hashes/bytes correct, budgets (snapshot <= 8 MB, playback
     <= 2 MB), valid UTF-8 with LF.
   - test_honesty.py: no 'live' / 'real-time' / 'realtime' (case-insensitive, word-level) in the OpenAPI document or
     in any string of any endpoint response; /meta.labels literal strings; data_notice present where SPEC 10.2 says;
     is_simulated true on every sensor and anomaly item; simulated assets flagged; explanations free of the forbidden
     causal words.
3. Run everything and keep it green:
   cd ${ROOT} && .venv/Scripts/python.exe -m pytest -q -m "not slow"
   cd ${ROOT} && .venv/Scripts/python.exe -m pytest -q -m "not db and not slow"     (no database needed)
   cd ${ROOT} && .venv/Scripts/python.exe -m pytest -q -m slow
   cd ${ROOT} && .venv/Scripts/python.exe -m pytest -q -m "not slow" --cov=pipeline --cov=backend --cov-report=term | tail -60
   cd ${ROOT} && .venv/Scripts/python.exe -m ruff check tests pipeline backend
   Report counts passed/failed/skipped, wall time, total coverage and the least-covered modules. Every failure ends as
   a fixed defect (report it) or a corrected test.
4. If A1 or any fix changed pipeline output or an API response, the committed snapshot must be refreshed at the end:
   cd ${ROOT} && .venv/Scripts/python.exe run_pipeline.py --skip-download      (this ONE command is allowed for you,
   only as the last step, and only if needed) — then confirm the default-dataset numbers of SPEC amendment A7 still
   hold and report which snapshot files changed (git status --short dashboard/data/snapshot | wc -l is not useful
   because the folder is untracked; compare manifest sha256 before/after instead).
Quality bar: no test that merely asserts 'is not None'; no sleeps; no network; parametrise where natural.`

// ------------------------------------------------------------------------------------------------ docker + CI
const BUILD_DOCKER = (tests) => `You are the PACKAGING engineer for the Dodge City prototype: container entrypoint, Dockerfile,
docker-compose backend service, .dockerignore, .env.example review, CI workflow.
${RULES}

The test author just reported: ${JSON.stringify(tests ? { summary: tests.summary, numbers: tests.numbers } : null)}

YOUR SCOPE — SPEC section 13 (Docker + CI paragraphs), section 4.2 (settings precedence, AUTO_SEED, SERVE_DASHBOARD),
brief R15, R20, R21. Files you own: backend/entrypoint.py, backend/Dockerfile, docker-compose.yml (a 'db' service
exists and its container is RUNNING with the project's data — keep the service name, image, volume name and port
mapping so the existing volume keeps working), .dockerignore, .github/workflows/ci.yml (keep pages.yml untouched),
and .env.example (only to add/adjust variables the backend service needs). You may fix defects in backend/** and
pipeline/** that packaging exposes (report them).

Requirements:
- backend/entrypoint.py (python -m backend.entrypoint): wait for the database (bounded retries with clear log
  lines), apply migrations, when AUTO_SEED is true and infra.detection_runs has no finished row run the pipeline
  in-process with --skip-download --skip-export (the committed data/raw is in the image), then exec uvicorn
  (backend.app.main:create_app --factory, host/port from settings). Never logs the DSN password. Exits non-zero with a
  clear message if the database never becomes reachable.
- backend/Dockerfile: python:3.12-slim, build context = repo root, COPY-only (no bind mounts), installs
  requirements.txt then 'pip install -e . --no-deps' (or a regular install), copies pipeline/, backend/, scripts/,
  sql/, data/raw/, dashboard/, run_pipeline.py, pyproject.toml; non-root user that can write what the pipeline
  writes at runtime (data/processed); ENTRYPOINT ["python","-m","backend.entrypoint"]; sensible ENV
  (PYTHONUNBUFFERED, PYTHONDONTWRITEBYTECODE); small layers ordered for caching.
- docker-compose.yml: add service 'backend' (build context ., dockerfile backend/Dockerfile; environment sets
  DATABASE_URL explicitly to the in-network address postgresql://\${POSTGRES_USER}:\${POSTGRES_PASSWORD}@db:5432/\${POSTGRES_DB}
  so a host-oriented DATABASE_URL in .env can never leak in; passes through the documented tunables; depends_on db
  healthy; ports "\${API_PORT:-8000}:8000"; healthcheck with python urllib on /health, interval 10s, start_period 240s;
  restart unless-stopped). Do NOT use env_file for the backend.
- .dockerignore per SPEC 13 (must exclude .env, .venv*, .git, .claude, data/processed, tests, caches, *.egg-info).
- .github/workflows/ci.yml: on push / pull_request: ruff + pytest (REQUIRE_DB=1) against a postgis/postgis:16-3.4
  service container + node --test dashboard/tests; Python 3.12; pip cache; no secrets in the file (CI-only throwaway
  credentials are fine and must be obviously throwaway). It cannot be executed locally — keep it simple, check its
  YAML parses, and say plainly in open_issues that it is unverified until the first push.

VERIFY for real (the Docker daemon is running; wrap docker CLI calls in 'timeout'; the build downloads the base
image and wheels — allow several minutes):
1. cd ${ROOT} && timeout 900 docker compose build backend
2. Fresh-database path WITHOUT touching the project's existing volume: run a second, isolated compose project, e.g.
   cd ${ROOT} && POSTGRES_HOST_PORT=5544 API_PORT=8044 timeout 900 docker compose -p dcim-verify up -d --build
   (same compose file, different project name → its own empty volume and network; if the port mapping variables do
   not exist yet in the compose file, add them). Wait for the backend to become healthy; show the entrypoint log
   (waiting → migrations → AUTO_SEED pipeline → uvicorn) and how long first start takes.
   Then: curl http://localhost:8044/health (status ok), /statistics (numbers equal SPEC amendment A7), /meta counts,
   / (dashboard HTML), /config.js (mode 'api'), /docs.
3. Restart path: docker compose -p dcim-verify restart backend → it must NOT re-seed (log says data present) and be
   healthy within seconds.
4. Tear the verification project down completely: docker compose -p dcim-verify down -v ; confirm the project's own
   'dodge-city-infra' db container and volume are untouched (docker ps, row counts through the venv).
5. Image size, build time, and that the image contains no .env / .git / .venv / tests (docker run --rm --entrypoint
   sh <image> -c 'ls -a /app; ...').
6. The documented two-step start (cp .env.example .env → set a password → docker compose up --build) works with only
   .env.example's variables: verify by running step 2 with an env file generated from .env.example (throwaway
   password) instead of the developer's .env — e.g. --env-file a temp copy.
Report exact commands and outputs.`

// ------------------------------------------------------------------------------------------------ docs
const BUILD_DOCS = (panels, tests, docker) => `You are the TECHNICAL WRITER for the Dodge City 3D Urban Infrastructure Monitoring & GeoAI
Dashboard prototype. You write the README and docs a client's engineer will read first.
${RULES}

YOUR SCOPE — SPEC section 13 (Docs paragraph), section 14 amendments (A5 wording is mandatory), brief R18, R21, R22
and the FINAL OUTPUT list (items 11-15). Files you own: README.md (replace the old one completely),
docs/{data-provenance,health-score,anomaly-detection,api,deployment,real-sensor-integration}.md,
docs/architecture.md only if the README diagram needs a longer companion. Do not edit code.

What the other builders reported (use their verified numbers; do not invent any):
DASHBOARD: ${JSON.stringify(panels ? { summary: panels.summary, numbers: panels.numbers, open_issues: panels.open_issues } : null)}
TESTS: ${JSON.stringify(tests ? { summary: tests.summary, numbers: tests.numbers, open_issues: tests.open_issues } : null)}
DOCKER: ${JSON.stringify(docker ? { summary: docker.summary, numbers: docker.numbers, open_issues: docker.open_issues } : null)}

RULES FOR THE WRITING
- Every command, path, endpoint, env var, table name, formula and number in the docs must be TRUE: verify against the
  code and the running system before you write it (grep the code; read pipeline/config.py for defaults and
  .env.example; list sql/migrations; curl the API if a server is needed:
  cd ${ROOT} && .venv/Scripts/python.exe -m uvicorn backend.app.main:create_app --factory --port 8030 , stop it after;
  GET /meta gives counts, labels, formulas, data sources with licences and retrieval dates, detection metrics).
  Run the commands you document where feasible (not docker compose up — the packaging engineer verified that; quote
  their result). Where something is unverified or a limitation, say so plainly.
- Data honesty is the point of this project: real vs simulated vs derived must be unmistakable; never claim
  monitoring of real conditions, certified safety, failure prediction or authoritative condition; never use the words
  "live" or "real-time" to describe this system; use the literal labels "Simulated Sensor Data", "Prototype Anomaly
  Detection", "Derived Asset Health Score".
- Write for a senior engineer evaluating the work: plain, specific, no marketing adjectives, no emoji, no filler.
  Tables for reference material, short paragraphs for explanation. Windows AND macOS/Linux command variants where they
  differ (venv activation, python launcher).
README.md — headings exactly these, numbered, in this order:
 1 Project overview · 2 Architecture (a Mermaid diagram of GIS data → PostGIS → Python processing → simulated
 sensors → anomaly detection → spatial analysis → API → dashboard, showing the SensorSource seam and the static
 snapshot path to GitHub Pages) · 3 Technology stack · 4 Data sources · 5 Data provenance (REAL / SIMULATED /
 DERIVED table incl. building-height sources with counts and the three known LiDAR caveats, the rejected NBI record,
 the simulated water network and why the City's real utility layers are not used) · 6 Database schema (tables, key
 columns, relationships incl. the reading → anomaly FK, indexes, views, functions, the seven example queries) ·
 7 Sensor simulation (heading text must include "Simulated Sensor Data") · 8 Anomaly detection (include "Prototype
 Anomaly Detection"; method step by step, score/severity formulas, evaluation numbers with the self-consistency
 caveat, amendment A5 sentence, retrospective-analysis statement) · 9 Spatial analysis (include "Derived Asset Health
 Score"; proximity, density, clustering, risk zones, the health formula exactly + worked scenarios) · 10 Running
 locally (step by step from a clean clone: venv, install, .env, docker compose up -d db, run_pipeline, uvicorn, open
 the dashboard; and the static-only mode) · 11 Docker setup · 12 Environment variables (complete table from
 pipeline/config.py) · 13 API endpoints (table + a few curl examples; note /health is service health) · 14 Testing
 (commands, markers, counts, what needs a database, node tests) · 15 Limitations · 16 Future real-sensor integration
 (SensorSource protocol, HttpPollingSource, POST /ingest/readings, what must change for MQTT / an IoT platform, what
 does NOT change) · 17 Deployment (database initialisation, frontend on GitHub Pages incl. config.js and the committed
 snapshot, backend on any Docker host, HTTPS + CORS_ORIGINS) · 18 What was reused (old path → new path table; get the
 old file list with: git show --stat HEAD and git show HEAD:README.md) · 19 What is new · 20 Remaining limitations.
docs/: data-provenance.md (per source: what, endpoint, licence, attribution string, vintage, how it is cached;
 SOURCES.json; LiDAR height method and caveats), health-score.md (formula, constants, worked scenarios, how to read
 it, what it is not), anomaly-detection.md (baseline, peer adjustment, detectors, persistence rule, scoring,
 evaluation, known weaknesses reported by the verifiers), api.md (every endpoint with parameters and a real example
 response excerpt), deployment.md, real-sensor-integration.md (HTTP adapter contract, ingest endpoint with curl,
 a ~30-line MQTT example against the SensorSource protocol clearly marked as an example, operational notes:
 re-running detection after ingest).
VERIFY: every relative link resolves; every documented command was run or is quoted from a verified report; the
Mermaid block is syntactically valid (check carefully by hand); README numbers match GET /meta; grep the docs for
"live" and "real-time" (must be absent as descriptions of this system); list anything you could not verify.`

// ------------------------------------------------------------------------------------------------ orchestration
phase('Dashboard panels')
const panels = await agent(BUILD_PANELS, { label: 'build:dashboard-panels', phase: 'Dashboard panels', schema: REPORT })
log(`panels: ${panels ? 'reported' : 'NO RESULT'}`)

phase('Tests')
const tests = await agent(BUILD_TESTS, { label: 'build:tests-finish', phase: 'Tests', schema: REPORT })
log(`tests: ${tests ? 'reported' : 'NO RESULT'}`)

phase('Docker + CI')
const docker = await agent(BUILD_DOCKER(tests), { label: 'build:docker-ci', phase: 'Docker + CI', schema: REPORT })
log(`docker: ${docker ? 'reported' : 'NO RESULT'}`)

phase('Docs')
const docs = await agent(BUILD_DOCS(panels, tests, docker), { label: 'build:docs', phase: 'Docs', schema: REPORT })

return { panels, tests, docker, docs }
