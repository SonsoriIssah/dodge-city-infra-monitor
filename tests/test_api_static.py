"""Static mount, runtime configuration and HTTP behaviour of the application (build contract 10 and 10.5).

``/`` is the dashboard and ``/docs`` the API documentation; ``/config.js`` is an API route that shadows the
static file and always says ``mode: 'api'``; ``.js`` and ``.geojson`` have their content types;
``SERVE_DASHBOARD=false`` removes the mount and ``/config.js``; CORS follows ``CORS_ORIGINS``; large answers
are gzip-encoded; ``/playback`` carries an ETag and answers 304 to ``If-None-Match``.

The tests that only read files or the OpenAPI document use an application whose database does not exist
(its connection pool is never opened); the others run on ``infra_test``.
"""

from __future__ import annotations

import json
import mimetypes
import re
from typing import Any

import pytest
from starlette.routing import Mount

from pipeline.config import DASHBOARD_DIR
from tests import support
from tests.conftest import make_client, make_settings

NOT_FOUND = {"detail": "Not Found"}
BIG_SCRIPT = "vendor/maplibre-gl/maplibre-gl.js"
ORIGIN = "https://dashboard.example.org"
OTHER_ORIGIN = "http://localhost:8080"


def offline_app(**overrides: Any):
    """A client of the application without a database: no lifespan, so the connection pool is never opened."""
    return make_client(make_settings(DATABASE_URL=support.unreachable_dsn(), **overrides))


@pytest.fixture(scope="module")
def served():
    """``SERVE_DASHBOARD=true`` (the default), no database."""
    return offline_app()


@pytest.fixture(scope="module")
def unserved():
    """``SERVE_DASHBOARD=false``, no database."""
    return offline_app(SERVE_DASHBOARD=False)


@pytest.fixture(scope="module")
def served_with_data(api_settings):
    """The dashboard and the API together on the test database."""
    with make_client(api_settings.model_copy(update={"SERVE_DASHBOARD": True})) as test_client:
        yield test_client


def media_type(response) -> str:
    return response.headers["content-type"].split(";")[0].strip()


# --- the dashboard at "/" -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/", "/index.html"])
def test_root_serves_the_dashboard_index(served, path):
    response = served.get(path)
    assert response.status_code == 200 and media_type(response) == "text/html"
    assert response.content == (DASHBOARD_DIR / "index.html").read_bytes()
    assert b"<html" in response.content.lower()


def test_the_dashboard_mount_is_the_last_route_and_serve_dashboard_is_the_default(served):
    assert support.SPEC_DEFAULTS["SERVE_DASHBOARD"] is True and served.app.state.settings.SERVE_DASHBOARD is True
    routes = served.app.routes
    mounts = [route for route in routes if isinstance(route, Mount)]
    assert [mount.path for mount in mounts] == [""] and routes[-1] is mounts[0]  # mounted at "/" after every API route
    paths = [getattr(route, "path", None) for route in routes]
    assert paths.index("/config.js") < routes.index(mounts[0])


def test_api_routes_win_over_the_dashboard_files(served_with_data):
    assert media_type(served_with_data.get("/")) == "text/html"
    health = served_with_data.get("/health")
    assert health.status_code == 200 and health.json()["service"] == support.SERVICE_NAME
    statistics = served_with_data.get("/statistics")
    assert statistics.status_code == 200 and set(statistics.json()) == support.STATISTICS_KEYS
    assert served_with_data.get("/assets?limit=1").json()["type"] == "FeatureCollection"
    docs = served_with_data.get("/docs")
    assert docs.status_code == 200 and "swagger" in docs.text.lower()


def test_no_dashboard_entry_uses_a_name_of_the_api(served):
    entries = {path.name for path in DASHBOARD_DIR.iterdir()}
    assert entries & support.RESERVED_DASHBOARD_NAMES == set()
    assert "config.js" in entries and "index.html" in entries  # the one deliberate overlap, and the page itself
    api_segments = {template.strip("/").split("/")[0] for template in served.get("/openapi.json").json()["paths"]}
    assert api_segments <= support.RESERVED_DASHBOARD_NAMES  # the reserved list knows every API route


# --- /config.js ---------------------------------------------------------------------------------------------------
def parse_config_js(text: str) -> dict[str, Any]:
    match = re.fullmatch(r"window\.DCIM_CONFIG = (\{.*\});\n?", text)
    assert match, text
    return json.loads(match.group(1))


def test_config_js_is_the_api_route_with_mode_api(served):
    response = served.get("/config.js")
    assert response.status_code == 200 and media_type(response) == "text/javascript"
    assert parse_config_js(response.text) == {
        "mode": "api",
        "apiBaseUrl": "",
        "basemapStyleUrl": support.SPEC_DEFAULTS["BASEMAP_STYLE_URL"],
    }
    static_file = (DASHBOARD_DIR / "config.js").read_text(encoding="utf-8")
    assert "mode: 'static'" in static_file and response.text != static_file  # the route shadows the static file
    assert "no-cache" in response.headers["cache-control"]


def test_config_js_carries_the_configured_basemap():
    style = "https://tiles.example.org/styles/night/style.json?key=a&lang=en"
    response = offline_app(BASEMAP_STYLE_URL=style).get("/config.js")
    assert parse_config_js(response.text) == {"mode": "api", "apiBaseUrl": "", "basemapStyleUrl": style}


def test_config_js_is_not_part_of_the_api_documentation(served):
    assert "/config.js" not in served.get("/openapi.json").json()["paths"]


# --- content types ------------------------------------------------------------------------------------------------
def dashboard_file(pattern: str) -> str:
    """The first dashboard file matching a glob pattern, as a POSIX path relative to the dashboard folder."""
    found = sorted(path.relative_to(DASHBOARD_DIR).as_posix() for path in DASHBOARD_DIR.glob(pattern))
    assert found, f"no dashboard file matches {pattern}"
    return found[0]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("js/main.js", "text/javascript"),
        ("js/**/*.js", "text/javascript"),
        (BIG_SCRIPT, "text/javascript"),
        ("data/snapshot/*.geojson", "application/geo+json"),
        ("data/snapshot/*.json", "application/json"),
        ("css/*.css", "text/css"),
        ("index.html", "text/html"),
    ],
)
def test_static_files_have_their_content_type_and_their_bytes(served, pattern, expected):
    name = dashboard_file(pattern)
    response = served.get(f"/{name}")
    assert response.status_code == 200, name
    assert media_type(response) == expected, name
    assert response.content == (DASHBOARD_DIR / name).read_bytes(), name


def test_content_types_are_registered_whatever_the_system_says():
    from backend.app.main import register_mimetypes

    mimetypes.add_type("text/plain", ".js")  # what a Windows registry can say
    mimetypes.add_type("application/octet-stream", ".geojson")
    register_mimetypes()
    assert mimetypes.guess_type("main.js")[0] == "text/javascript"
    assert mimetypes.guess_type("assets.geojson")[0] == "application/geo+json"


# --- unknown paths ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["/nope", "/nope.js", "/js/nope.js", "/js", "/js/", "/data/snapshot/nope.json", "/data/snapshot", "/api/assets", "/index.htm"],
)
def test_unknown_path_is_a_404_json_also_with_the_dashboard_mounted(served, path):
    response = served.get(path)
    assert response.status_code == 404 and media_type(response) == "application/json"
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize(
    "path",
    ["/../pyproject.toml", "/%2e%2e/pyproject.toml", "/..%2fpyproject.toml", "/js/../../pyproject.toml", "/js/%2e%2e/%2e%2e/pyproject.toml",
     "/..%5cpyproject.toml", "/%2e%2e/%2e%2e/.env", "/data/snapshot/../../../requirements.txt"],
)  # fmt: skip
def test_the_mount_serves_nothing_outside_the_dashboard_folder(served, path):
    response = served.get(path)
    assert response.status_code == 404, path
    assert b"[project]" not in response.content and b"fastapi" not in response.content.lower()


def test_post_on_a_static_path_is_a_405(served):
    for path in ("/", "/index.html", "/js/main.js"):
        response = served.post(path, json={})
        assert response.status_code == 405 and response.json() == {"detail": "Method Not Allowed"}


# --- SERVE_DASHBOARD=false ----------------------------------------------------------------------------------------
def test_without_serve_dashboard_there_is_no_mount_and_no_config_js(unserved):
    assert not [route for route in unserved.app.routes if isinstance(route, Mount)]
    assert "/config.js" not in [getattr(route, "path", None) for route in unserved.app.routes]
    for path in ("/", "/index.html", "/config.js", "/js/main.js", f"/{BIG_SCRIPT}", "/css/tokens.css"):
        response = unserved.get(path)
        assert response.status_code == 404 and response.json() == NOT_FOUND, path
    assert unserved.get("/docs").status_code == 200 and unserved.get("/openapi.json").status_code == 200


def test_a_missing_dashboard_folder_does_not_stop_the_application(tmp_path, monkeypatch):
    import backend.app.main as main

    monkeypatch.setattr(main, "DASHBOARD_DIR", tmp_path / "no-such-folder")
    client = make_client(make_settings(DATABASE_URL=support.unreachable_dsn(), SERVE_DASHBOARD=True))
    assert client.get("/").status_code == 404 and client.get("/config.js").status_code == 404
    assert client.get("/openapi.json").status_code == 200


# --- documentation ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("app_fixture", ["served", "unserved"])
def test_docs_and_openapi_work(request, app_fixture):
    client = request.getfixturevalue(app_fixture)
    docs = client.get("/docs")
    assert docs.status_code == 200 and media_type(docs) == "text/html"
    assert "/openapi.json" in docs.text and "swagger" in docs.text.lower()
    assert client.get("/redoc").status_code == 200
    response = client.get("/openapi.json")
    assert response.status_code == 200 and media_type(response) == "application/json"
    document = response.json()
    assert document["openapi"].startswith("3.")
    assert document["info"]["version"] == "1.0.0" and document["info"]["title"]
    assert support.DATA_NOTICE in document["info"]["description"]  # the data notice of contract section 10
    gets = {template for template, operations in document["paths"].items() if "get" in operations}
    posts = {template for template, operations in document["paths"].items() if "post" in operations}
    assert (gets, posts) == (support.CONTRACT_GET_PATHS, support.CONTRACT_POST_PATHS)


def test_every_operation_is_described_and_answers_json(served):
    document = served.get("/openapi.json").json()
    for template, operations in document["paths"].items():
        for method, operation in operations.items():
            where = f"{method.upper()} {template}"
            assert operation["summary"].strip() and len(operation["description"].strip()) > 40, where
            assert operation["tags"], where
            content = operation["responses"]["200"]["content"]
            assert "application/json" in content and content["application/json"]["schema"], where
            for parameter in operation.get("parameters", []):
                assert parameter.get("description") or parameter["in"] == "header", f"{where} {parameter['name']}"


# --- CORS ---------------------------------------------------------------------------------------------------------
def test_cors_default_allows_every_origin(served):
    assert support.SPEC_DEFAULTS["CORS_ORIGINS"] == "*"
    response = served.get("/openapi.json", headers={"Origin": ORIGIN})
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-origin" not in served.get("/openapi.json").headers  # not a cross-origin request


def test_cors_header_for_an_allowed_origin_only():
    client = offline_app(SERVE_DASHBOARD=False, CORS_ORIGINS=f"{ORIGIN}, {OTHER_ORIGIN}")
    assert client.app.state.settings.cors_origin_list == [ORIGIN, OTHER_ORIGIN]
    for origin in (ORIGIN, OTHER_ORIGIN):
        response = client.get("/openapi.json", headers={"Origin": origin})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert "Origin" in response.headers["vary"]
        assert "ETag" in response.headers["access-control-expose-headers"]
    for origin in ("https://elsewhere.example.com", ORIGIN + ".evil.example", ORIGIN.replace("https", "http")):
        response = client.get("/openapi.json", headers={"Origin": origin})
        assert response.status_code == 200 and "access-control-allow-origin" not in response.headers, origin


def test_cors_preflight():
    client = offline_app(SERVE_DASHBOARD=False, CORS_ORIGINS=ORIGIN)
    asked = {"Origin": ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "If-None-Match"}
    response = client.options("/playback", headers=asked)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    allowed_methods = {method.strip() for method in response.headers["access-control-allow-methods"].split(",")}
    assert {"GET", "POST"} <= allowed_methods and not {"DELETE", "PUT", "PATCH"} & allowed_methods
    allowed_headers = {header.strip().lower() for header in response.headers["access-control-allow-headers"].split(",")}
    assert {"if-none-match", "x-api-key", "content-type"} <= allowed_headers
    ingest = client.options("/ingest/readings", headers={**asked, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-API-Key, Content-Type"})
    assert ingest.status_code == 200
    refused = client.options("/playback", headers={**asked, "Origin": "https://elsewhere.example.com"})
    assert refused.status_code == 400 and "access-control-allow-origin" not in refused.headers
    assert client.options("/playback", headers={**asked, "Access-Control-Request-Method": "DELETE"}).status_code == 400


def test_error_answers_carry_the_cors_header_too():
    client = offline_app(SERVE_DASHBOARD=False, CORS_ORIGINS=ORIGIN)
    response = client.get("/nope", headers={"Origin": ORIGIN})
    assert response.status_code == 404 and response.headers["access-control-allow-origin"] == ORIGIN


# --- gzip ---------------------------------------------------------------------------------------------------------
def test_gzip_on_a_large_static_file(served):
    plain = served.get(f"/{BIG_SCRIPT}", headers={"Accept-Encoding": "identity"})
    packed = served.get(f"/{BIG_SCRIPT}", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in plain.headers and packed.headers["content-encoding"] == "gzip"
    assert packed.content == plain.content == (DASHBOARD_DIR / BIG_SCRIPT).read_bytes()
    assert packed.num_bytes_downloaded < plain.num_bytes_downloaded / 2
    assert "Accept-Encoding" in packed.headers["vary"]


@pytest.mark.parametrize("url", ["/assets", "/sensors", "/anomalies?limit=1000", "/sensor-readings?sensor_id=VIB-001&shape=columns", "/spatial/risk-zones", "/layers/roads", "/meta"])
def test_gzip_on_a_large_response(client, url):
    plain = client.get(url, headers={"Accept-Encoding": "identity"})
    packed = client.get(url, headers={"Accept-Encoding": "gzip"})
    assert plain.status_code == packed.status_code == 200
    assert "content-encoding" not in plain.headers and packed.headers["content-encoding"] == "gzip"
    assert packed.content == plain.content and len(plain.content) > 1024  # the same body once decoded
    assert packed.num_bytes_downloaded < plain.num_bytes_downloaded / 2
    assert "Accept-Encoding" in packed.headers["vary"]


def test_small_answers_are_not_compressed(client):
    for url in ("/statistics", "/health", "/assets/NOPE-999"):
        response = client.get(url, headers={"Accept-Encoding": "gzip"})
        assert len(response.content) < 1024 and "content-encoding" not in response.headers, url


# --- /playback: ETag ----------------------------------------------------------------------------------------------
def test_playback_etag_and_if_none_match(client):
    first = client.get("/playback")
    assert first.status_code == 200 and media_type(first) == "application/json"
    etag = first.headers["etag"]
    assert re.fullmatch(r'(W/)?"[^"]+"', etag)
    assert "no-cache" in first.headers["cache-control"]  # the browser revalidates instead of reusing blindly

    again = client.get("/playback")
    assert again.headers["etag"] == etag and again.content == first.content

    cached = client.get("/playback", headers={"If-None-Match": etag})
    assert cached.status_code == 304 and cached.content == b"" and cached.headers["etag"] == etag

    opaque = etag.removeprefix("W/")
    for header in (opaque, f"W/{opaque}", f'"something-else", {etag}', "*"):
        assert client.get("/playback", headers={"If-None-Match": header}).status_code == 304, header
    for header in ('"something-else"', 'W/"0"', opaque.strip('"'), ""):
        stale = client.get("/playback", headers={"If-None-Match": header})
        assert stale.status_code == 200 and stale.content == first.content, header


def test_playback_is_the_same_document_with_and_without_gzip(client):
    plain = client.get("/playback", headers={"Accept-Encoding": "identity"})
    packed = client.get("/playback", headers={"Accept-Encoding": "gzip, deflate, br"})
    assert "content-encoding" not in plain.headers and packed.headers["content-encoding"] == "gzip"
    assert packed.content == plain.content
    assert packed.headers["etag"] == plain.headers["etag"]
    assert packed.num_bytes_downloaded < plain.num_bytes_downloaded / 3
    assert "Accept-Encoding" in packed.headers["vary"]
    assert set(json.loads(plain.content)) == support.PLAYBACK_KEYS


def test_playback_etag_is_exposed_to_cross_origin_pages(client):
    response = client.get("/playback", headers={"Origin": ORIGIN, "Accept-Encoding": "gzip"})
    assert response.headers["access-control-allow-origin"] == "*"
    assert "ETag" in response.headers["access-control-expose-headers"]
    cached = client.get("/playback", headers={"Origin": ORIGIN, "If-None-Match": response.headers["etag"]})
    assert cached.status_code == 304 and cached.headers["access-control-allow-origin"] == "*"
