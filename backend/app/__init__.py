"""FastAPI application of the prototype (build contract section 10).

    main.py      create_app(settings=None): middleware, error handling, routers, dashboard mount
    deps.py      database pool, request dependencies, validated query parameters
    schemas.py   response and request models, canonical JSON encoding
    timeutil.py  the one timestamp format of the API (iso_z) and the datetime parser
    queries/     every SQL statement of the API (parameterised), returning plain dictionaries
    routers/     the HTTP endpoints, grouped by resource

Start it with ``uvicorn backend.app.main:create_app --factory``.
"""
