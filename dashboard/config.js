/*
 * Runtime configuration of the dashboard (static hosting, e.g. GitHub Pages).
 *
 *   mode             'static'  always the snapshot under ./data/snapshot (the default here: a static host has no
 *                              API, and probing for one would log a 404 in the browser console)
 *                    'auto'    probe `${apiBaseUrl}/health` for 3 s; use the API when it answers as the
 *                              monitoring service with status "ok", otherwise the static snapshot
 *                    'api'     always the API; when it does not answer the page shows an error with
 *                              "Retry" and "Use static snapshot"
 *                    The URL parameter ?mode=static or ?mode=api overrides this value.
 *   apiBaseUrl       ''        same origin (relative URLs); or e.g. 'https://api.example.org' (the API must
 *                              allow this page's origin in CORS_ORIGINS)
 *   basemapStyleUrl  MapLibre style of the basemap; when it cannot be fetched within 4 s the map shows the
 *                    project data on a plain background
 *
 * When the FastAPI service serves the dashboard it answers GET /config.js itself (mode 'api', same origin),
 * so this file is only used on static hosts.
 */
window.DCIM_CONFIG = {
  mode: 'static',
  apiBaseUrl: '',
  basemapStyleUrl: 'https://tiles.openfreemap.org/styles/dark',
};
