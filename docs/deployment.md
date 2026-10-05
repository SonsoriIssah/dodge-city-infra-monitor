# Deployment

Three deployable parts, each usable without the others:

| Part | What | Where it can run |
|---|---|---|
| Database | PostgreSQL 16 + PostGIS 3.4, schema `infra` | the compose `db` service, any Docker host, or a managed PostgreSQL with PostGIS |
| Backend | FastAPI service + the data pipeline (`backend/Dockerfile`) | any Docker host |
| Frontend | the static `dashboard/` folder with its committed snapshot | GitHub Pages (as before) or any static web server; also served by the backend at `/` |

Local development is described in the [README, section 10](../README.md#10-running-locally); this page covers
running the parts elsewhere.

**Verification status.** The commands on this page that involve `docker compose up --build`, `docker build` or a
remote host were not run in the build environment (pip inside `docker build` could not pass that sandbox's
TLS-intercepting proxy). The container entrypoint was verified on the host against a fresh `postgis/postgis:16-3.4`
container; the static export and static serving were verified locally. The reverse-proxy snippet is an example.

---

## 1. Database initialisation

### With docker compose (simplest)

`docker compose up -d db` starts `postgis/postgis:16-3.4` with the database, role and password from `.env`
(`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`; compose refuses to start without a password), a named volume
`pgdata`, and publishes it on `${POSTGRES_HOST_PORT:-5433}`. The backend container initialises it on first start
(below), or from a development machine:

```bash
python run_pipeline.py --skip-download --skip-export
```

### On an existing or managed PostgreSQL

Requirements: PostgreSQL 16 (other recent versions are likely to work but were not tested), PostGIS 3.4 with
`ST_HexagonGrid`, and a role that owns the target database.

```sql
-- as an administrator
CREATE ROLE infra LOGIN PASSWORD '<url-safe password>';
CREATE DATABASE infra OWNER infra;
\c infra
CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public;   -- if the role may not create extensions itself
```

Then, from a checkout with the virtual environment (see the README) and `DATABASE_URL` pointing at that database:

```bash
export DATABASE_URL="postgresql://infra:<password>@db.example.org:5432/infra"   # PowerShell: $env:DATABASE_URL = "..."
python scripts/seed_database.py                         # applies sql/migrations/*.sql, loads the GIS layers
python run_pipeline.py --skip-download --skip-export    # or run the whole dataset build in one go
```

The migrations are idempotent and recorded in `infra.schema_migrations`; they can also be applied by hand:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/migrations/001_schema.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/migrations/002_views_functions.sql
```

`python scripts/seed_database.py --reset-schema` drops schema `infra` first (development convenience). The database
holds one study area at a time; changing `STUDY_AREA_*` and running `python run_pipeline.py --refresh` rebuilds
everything.

---

## 2. Backend on a Docker host

### docker compose

```bash
git clone https://github.com/SonsoriIssah/dodge-city-infra-monitor.git
cd dodge-city-infra-monitor
cp .env.example .env            # set POSTGRES_PASSWORD (letters, digits, - _ . ~ only), CORS_ORIGINS, ...
docker compose up -d --build
docker compose logs -f backend  # wait for the uvicorn start line; the first start seeds the database
```

Start-up sequence of the backend container (`python -m backend.entrypoint`):

1. Waits for the database: 30 attempts, 2 s apart by default (`--wait-attempts`, `--wait-interval`); exit code 2
   with a message naming host, port, database and user if it never answers. The password is never logged.
2. Applies the migrations, holding a PostgreSQL advisory lock so that two containers never migrate or seed at the
   same time.
3. When `AUTO_SEED=true` and `infra.detection_runs` has no finished row: runs the pipeline in-process
   (`run_pipeline.py --skip-download --skip-export`) from the committed `data/raw` in the image. Nothing is
   downloaded and the dashboard snapshot in the image is not rewritten. With `AUTO_SEED=false` and an empty
   database the API starts but data endpoints answer 503.
4. Replaces itself with uvicorn on `0.0.0.0:8000` inside the container.

Measured with the entrypoint run on the host against a fresh PostGIS container: first start to `/health` = 200 in
25.3 s (about 8 s waiting for PostGIS to initialise, 14.6 s for the seeding pipeline); a restart with data present
1.6 s.

Compose details that matter in production:

- The backend gets `DATABASE_URL=postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}`
  explicitly and no `env_file`; only the variables listed in `docker-compose.yml` reach the container (an empty
  value means "application default"). Hence `POSTGRES_PASSWORD` must be URL-safe.
- `API_PORT` in `.env` is the **host** port the container's port 8000 is published on.
- The `db` port is published on the host (`POSTGRES_HOST_PORT`) for development. On a server, remove that `ports:`
  entry or bind it to `127.0.0.1` so that the database is not reachable from outside.
- Healthcheck: `GET /health` every 10 s with a 240 s start period (time for the first seeding).
- Data lives in the `pgdata` volume. `docker compose down` keeps it; `docker compose down -v` deletes it, and the next
  start seeds again.

### Without compose (external database)

```bash
docker build -f backend/Dockerfile -t dodge-city-infra-backend .
docker run -d --name dcim-backend -p 127.0.0.1:8000:8000 \
  -e DATABASE_URL="postgresql://infra:<password>@db.example.org:5432/infra" \
  -e CORS_ORIGINS="https://sonsoriissah.github.io" \
  dodge-city-infra-backend
```

The image is `python:3.12-slim`, runs as the non-root user `app` (uid 10001), contains `data/raw`, `sql`,
`pipeline`, `backend`, `scripts` and `dashboard` (with the snapshot), and exposes port 8000. Every variable of the
README's [environment table](../README.md#12-environment-variables) can be passed with `-e`.

### HTTPS, CORS and secrets

- Terminate TLS in a reverse proxy in front of port 8000 (Caddy, nginx, a cloud load balancer). Example Caddyfile:

  ```
  api.example.org {
      reverse_proxy 127.0.0.1:8000
  }
  ```

  A dashboard served over HTTPS (GitHub Pages is) can only call an API served over HTTPS.
- `CORS_ORIGINS`: the default `*` is for development. Set it to the origins of the pages that call the API, comma
  separated, for example `https://sonsoriissah.github.io` (an origin has no path). A dashboard served by the API
  itself is same-origin and needs no CORS entry.
- `INGEST_API_KEY`: leave empty unless an external gateway posts readings; when set, use a long random value and send
  it only over HTTPS.
- `SERVE_DASHBOARD=false` turns the API into a pure data service (no dashboard at `/`, no `/config.js`).
- Never commit `.env`; it is git-ignored and excluded from the Docker build context.

### Updating the data on a server

```bash
docker compose exec backend python run_pipeline.py --skip-download --skip-export   # rebuild stages 2-6
```

No restart is needed: the API caches the playback bundle per data generation (run id, the run's finish time and the
row versions of `asset_health` and `risk_zone_scores`) and rebuilds it on the next request after a new run.

---

## 3. Frontend on GitHub Pages

`.github/workflows/pages.yml` (unchanged from the original repository) uploads the `dashboard/` folder and deploys
it to GitHub Pages on every push to `main` (and on manual dispatch). For this project the site is
https://sonsoriissah.github.io/dodge-city-infra-monitor/. It serves whatever is on `main`, so the dashboard
described here appears there only after this branch is merged.

### How the static page gets its data

`dashboard/config.js`:

```js
window.DCIM_CONFIG = {
  mode: 'auto',
  apiBaseUrl: '',
  basemapStyleUrl: 'https://tiles.openfreemap.org/styles/dark',
};
```

| `mode` | Behaviour |
|---|---|
| `auto` (default) | Probes `${apiBaseUrl}/health` for 3 s and uses the API only when it answers HTTP 200 as `dodge-city-infra-monitor` with `status: "ok"`; otherwise loads `./data/snapshot/` |
| `api` | Always the API; when it does not answer, an error state with "Retry" and "Use static snapshot" (never a silent switch) |
| `static` | Always the snapshot |

The URL parameter `?mode=static` or `?mode=api` overrides the file. All URLs are relative, so the Pages sub-path
works. On Pages with the default file, `./health` does not exist and the page runs from the snapshot; the source
badge reads "Source: static snapshot (exported <date>)".

To make the published page use a deployed backend, set `apiBaseUrl: 'https://api.example.org'` (keep `mode: 'auto'`
for a fallback to the snapshot) and add the Pages origin to the backend's `CORS_ORIGINS`.

### The committed snapshot

`dashboard/data/snapshot/` holds real API responses written by stage 7 (`backend/export.py`, which drives the app
in-process): `meta.json`, `assets.geojson`, `roads.geojson`, `study-area.geojson`, `city-boundary.geojson`,
`sensors.json`, `anomalies.json`, `clusters.geojson`, `risk-zones.geojson`, `simulation-events.json`,
`playback.json`, `readings/<sensor_id>.json` (128), `health/<asset_id>.json` (112) and `manifest.json` (path, size
and sha256 of every file). 252 files, about 6.5 MB; tests enforce budgets of 8 MB for the snapshot and 2 MB for
`playback.json` (1.28 MB now), and that every file equals the API response byte for byte.

Refreshing it after a change to the data or the pipeline:

```bash
python run_pipeline.py --skip-download          # stages 2-7, or: python scripts/export_static.py
git add dashboard/data/snapshot && git commit -m "Refresh dashboard snapshot"
```

`python scripts/export_static.py --output <dir>` writes the snapshot somewhere else (for inspection) without touching
the committed one. The export is deterministic (sorted keys, compact separators, LF), so unchanged data produces no
diff, with one exception: `meta.json` (`detection_run.finished_at`) and `manifest.json` (`generated_at` and the hash
of `meta.json`) change on every pipeline run, because a run's finish time is wall-clock. Verified: an export of the
current database into a scratch folder differed from the committed snapshot only in those two files.

### Other static hosts

Any static web server can serve `dashboard/` (it must serve `.js` as JavaScript and must not require directory
listings). For a quick local check: `python -m http.server -d dashboard 8080`. Opening `index.html` from the file
system does not work (browsers block ES modules on `file:`); the page says so.

---

## 4. Checklist for a demo deployment

1. Database and backend: `docker compose up -d --build` on a host with a URL-safe `POSTGRES_PASSWORD`; database port
   not exposed publicly.
2. HTTPS reverse proxy to port 8000; `CORS_ORIGINS` set to the dashboard origin.
3. `curl -s https://api.example.org/health` answers `"status":"ok"`, `"database":"ok"` and the data window.
4. Either open the dashboard served by the backend at `https://api.example.org/`, or set `apiBaseUrl` in
   `dashboard/config.js` for GitHub Pages and merge to `main`.
5. Check the source badge in the dashboard header ("Source: PostGIS API" vs "Source: static snapshot").
