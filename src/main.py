"""
ISLI Skill: data-gov-ma-connector

A read-only HTTP microservice that proxies Morocco's national open data portal
(data.gov.ma, CKAN) for ISLI agents. Exposes tool endpoints defined in
`isli-skill.yaml` and validates internal JWTs from the ISLI Core.
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import jwt
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from cache import TTLCache, make_key
from ckan_client import CkanAPIError, CkanClient, build_fq

JWT_SECRET = os.getenv("JWT_SECRET")
SKIP_AUTH = os.getenv("SKIP_AUTH", "").lower() in ("1", "true", "yes")
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "..", "isli-skill.yaml")

# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

# Catalog endpoints change rarely; cache for 1 hour.
catalog_cache = TTLCache(default_ttl=3600.0, maxsize=200)
# Dataset metadata can change periodically; cache for 5 minutes.
dataset_cache = TTLCache(default_ttl=300.0, maxsize=500)
# Search results are more volatile; cache for 60 seconds.
search_cache = TTLCache(default_ttl=60.0, maxsize=1000)

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

ckan = CkanClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await ckan.close()


app = FastAPI(title="data-gov-ma-connector", lifespan=lifespan)


@app.middleware("http")
async def add_request_timing(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration = (time.perf_counter() - start) * 1000
    response.headers["X-Response-Time-Ms"] = f"{duration:.2f}"
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_manifest_cache: dict[str, Any] | None = None


def load_manifest() -> dict[str, Any]:
    global _manifest_cache
    if _manifest_cache is None:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            _manifest_cache = yaml.safe_load(f)
    return _manifest_cache


def verify_internal_auth(x_internal_auth: str | None) -> dict[str, Any]:
    if SKIP_AUTH:
        return {"skip_auth": True}
    if not x_internal_auth:
        raise HTTPException(status_code=401, detail="Missing X-Internal-Auth header")
    if not JWT_SECRET:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
    try:
        return jwt.decode(x_internal_auth, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Expired internal token") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid internal token: {exc}") from exc


def ok_response(
    result: Any,
    metadata: dict[str, Any] | None = None,
    timing: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"success": True, "result": result}
    if metadata:
        body["metadata"] = metadata
    if timing:
        body["timing"] = timing
    return JSONResponse(content=body)


def error_response(message: str, code: int = 400, upstream_error: dict | None = None) -> JSONResponse:
    body: dict[str, Any] = {"success": False, "error": {"message": message, "code": code}}
    if upstream_error:
        body["error"]["upstream_error"] = upstream_error
    return JSONResponse(status_code=code, content=body)


async def cached_call(
    cache: TTLCache,
    key: str,
    fetch: Any,
    use_cache: bool = True,
) -> tuple[Any, bool, float | None]:
    """
    Fetch from cache or execute `fetch()` coroutine.
    Returns (value, was_cached, upstream_duration_ms).
    """
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached, True, None
    start = time.perf_counter()
    value = await fetch()
    upstream_ms = (time.perf_counter() - start) * 1000
    if use_cache:
        cache.set(key, value)
    return value, False, upstream_ms


# ---------------------------------------------------------------------------
# Required skill endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/.well-known/isli-manifest")
async def manifest():
    return load_manifest()


# ---------------------------------------------------------------------------
# Tool endpoints
# ---------------------------------------------------------------------------

@app.post("/search_datasets")
async def search_datasets(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()

    q = body.get("q", "*:*")
    rows = min(int(body.get("rows", 20)), 1000)
    start = int(body.get("start", 0))
    sort = body.get("sort", "score desc")
    organization = body.get("organization")
    group = body.get("group")
    tags = body.get("tags")
    fmt = body.get("format")
    fields = body.get("fields")
    use_cache = body.get("use_cache", True)

    fq = build_fq(organization=organization, group=group, tags=tags, format=fmt)
    # Use an empty fl to request the full CKAN record when the caller asks for it.
    fl: list[str] | None = [] if fields == "full" else (fields if isinstance(fields, list) else None)

    cache_key = make_key("package_search", q, rows, start, sort, fq, fl)
    try:
        result, cached, upstream_ms = await cached_call(
            search_cache,
            cache_key,
            lambda: ckan.package_search(q=q, rows=rows, start=start, sort=sort, fq=fq or None, fl=fl),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)

    metadata = {
        "q": q,
        "fq": fq,
        "rows": rows,
        "start": start,
        "sort": sort,
        "count": result.get("count", 0),
        "cached": cached,
    }
    if fields == "full":
        metadata["fields"] = "full"
    elif isinstance(fields, list):
        metadata["fields"] = fields
    else:
        metadata["fields"] = "default"
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result.get("results", []), metadata=metadata, timing=timing)


@app.post("/get_dataset")
async def get_dataset(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    dataset_id = body.get("id")
    use_cache = body.get("use_cache", True)
    if not dataset_id:
        return error_response("Missing required parameter: id", code=422)
    try:
        result, cached, upstream_ms = await cached_call(
            dataset_cache,
            make_key("package_show", dataset_id),
            lambda: ckan.package_show(dataset_id),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)
    metadata = {"id": dataset_id, "cached": cached}
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result, metadata=metadata, timing=timing)


@app.post("/list_resources")
async def list_resources(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    dataset_id = body.get("dataset_id")
    fmt = body.get("format")
    use_cache = body.get("use_cache", True)
    if not dataset_id:
        return error_response("Missing required parameter: dataset_id", code=422)

    # Prefer a lightweight package_search projection instead of the heavy package_show.
    light_fields = ["id", "name", "title", "resources", "num_resources"]
    cache_key = make_key("list_resources", dataset_id)
    try:
        dataset, cached, upstream_ms = await cached_call(
            dataset_cache,
            cache_key,
            lambda: _fetch_dataset_for_resources(dataset_id, light_fields),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)

    resources = dataset.get("resources", [])
    if fmt:
        resources = [r for r in resources if r.get("format", "").upper() == fmt.upper()]
    metadata = {
        "dataset": dataset.get("name"),
        "title": dataset.get("title"),
        "cached": cached,
        "via": "package_search_projection",
    }
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(resources, metadata=metadata, timing=timing)


async def _fetch_dataset_for_resources(dataset_id: str, fields: list[str]) -> dict[str, Any]:
    """Try a lightweight package_search projection; fall back to package_show if needed."""
    try:
        result = await ckan.package_search(
            q=f'name:"{dataset_id}"',
            rows=1,
            fl=fields,
        )
        results = result.get("results", [])
        if results:
            return results[0]
    except CkanAPIError:
        pass
    return await ckan.package_show(dataset_id)


@app.post("/get_resource")
async def get_resource(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    resource_id = body.get("id")
    use_cache = body.get("use_cache", True)
    if not resource_id:
        return error_response("Missing required parameter: id", code=422)
    try:
        result, cached, upstream_ms = await cached_call(
            dataset_cache,
            make_key("resource_show", resource_id),
            lambda: ckan.resource_show(resource_id),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)
    metadata = {"id": resource_id, "cached": cached}
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result, metadata=metadata, timing=timing)


@app.post("/search_resources")
async def search_resources(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    query = body.get("query")
    if not query:
        return error_response("Missing required parameter: query", code=422)
    limit = min(int(body.get("limit", 100)), 1000)
    offset = int(body.get("offset", 0))
    order_by = body.get("order_by", "score desc")
    use_cache = body.get("use_cache", True)
    try:
        result, cached, upstream_ms = await cached_call(
            search_cache,
            make_key("resource_search", query, limit, offset, order_by),
            lambda: ckan.resource_search(query=query, limit=limit, offset=offset, order_by=order_by),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)
    metadata = {"query": query, "count": result.get("count", 0), "cached": cached}
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result.get("results", []), metadata=metadata, timing=timing)


@app.post("/search_by_organization")
async def search_by_organization(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    organization = body.get("organization")
    if not organization:
        return error_response("Missing required parameter: organization", code=422)
    rows = min(int(body.get("rows", 20)), 1000)
    start = int(body.get("start", 0))
    sort = body.get("sort", "metadata_modified desc")
    fields = body.get("fields")
    use_cache = body.get("use_cache", True)
    fq = build_fq(organization=organization)
    fl: list[str] | None = [] if fields == "full" else (fields if isinstance(fields, list) else None)

    cache_key = make_key("package_search", "*:*", rows, start, sort, fq, fl)
    try:
        result, cached, upstream_ms = await cached_call(
            search_cache,
            cache_key,
            lambda: ckan.package_search(q="*:*", rows=rows, start=start, sort=sort, fq=fq, fl=fl),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)
    metadata = {"organization": organization, "count": result.get("count", 0), "cached": cached}
    if fields == "full":
        metadata["fields"] = "full"
    elif isinstance(fields, list):
        metadata["fields"] = fields
    else:
        metadata["fields"] = "default"
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result.get("results", []), metadata=metadata, timing=timing)


@app.post("/search_by_group")
async def search_by_group(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    group = body.get("group")
    if not group:
        return error_response("Missing required parameter: group", code=422)
    rows = min(int(body.get("rows", 20)), 1000)
    start = int(body.get("start", 0))
    sort = body.get("sort", "score desc")
    fields = body.get("fields")
    use_cache = body.get("use_cache", True)
    fq = build_fq(group=group)
    fl: list[str] | None = [] if fields == "full" else (fields if isinstance(fields, list) else None)

    cache_key = make_key("package_search", "*:*", rows, start, sort, fq, fl)
    try:
        result, cached, upstream_ms = await cached_call(
            search_cache,
            cache_key,
            lambda: ckan.package_search(q="*:*", rows=rows, start=start, sort=sort, fq=fq, fl=fl),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)
    metadata = {"group": group, "count": result.get("count", 0), "cached": cached}
    if fields == "full":
        metadata["fields"] = "full"
    elif isinstance(fields, list):
        metadata["fields"] = fields
    else:
        metadata["fields"] = "default"
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result.get("results", []), metadata=metadata, timing=timing)


@app.post("/list_organizations")
async def list_organizations(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    all_fields = body.get("all_fields", True)
    sort = body.get("sort", "package_count")
    limit = min(int(body.get("limit", 100)), 1000)
    use_cache = body.get("use_cache", True)
    try:
        result, cached, upstream_ms = await cached_call(
            catalog_cache,
            make_key("organization_list", all_fields, sort, limit),
            lambda: ckan.organization_list(all_fields=all_fields, sort=sort, limit=limit),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)
    metadata = {"count": len(result) if isinstance(result, list) else None, "cached": cached}
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result, metadata=metadata, timing=timing)


@app.post("/list_groups")
async def list_groups(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    all_fields = body.get("all_fields", True)
    limit = min(int(body.get("limit", 100)), 1000)
    use_cache = body.get("use_cache", True)
    try:
        result, cached, upstream_ms = await cached_call(
            catalog_cache,
            make_key("group_list", all_fields, limit),
            lambda: ckan.group_list(all_fields=all_fields, limit=limit),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)
    metadata = {"count": len(result) if isinstance(result, list) else None, "cached": cached}
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result, metadata=metadata, timing=timing)


@app.post("/list_tags")
async def list_tags(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    query = body.get("query")
    limit = min(int(body.get("limit", 100)), 1000)
    use_cache = body.get("use_cache", True)
    try:
        result, cached, upstream_ms = await cached_call(
            catalog_cache,
            make_key("tag_list", query, limit),
            lambda: ckan.tag_list(query=query, limit=limit),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)
    metadata = {"count": len(result) if isinstance(result, list) else None, "cached": cached}
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result, metadata=metadata, timing=timing)


@app.post("/get_organization")
async def get_organization(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    org_id = body.get("id")
    use_cache = body.get("use_cache", True)
    if not org_id:
        return error_response("Missing required parameter: id", code=422)
    try:
        result, cached, upstream_ms = await cached_call(
            catalog_cache,
            make_key("organization_show", org_id),
            lambda: ckan.organization_show(org_id),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)
    metadata = {"id": org_id, "cached": cached}
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result, metadata=metadata, timing=timing)


@app.post("/get_group")
async def get_group(
    request: Request,
    x_internal_auth: str | None = Header(None),
):
    _ = verify_internal_auth(x_internal_auth)
    body = await request.json()
    group_id = body.get("id")
    if not group_id:
        return error_response("Missing required parameter: id", code=422)
    include_datasets = body.get("include_datasets", True)
    use_cache = body.get("use_cache", True)
    try:
        result, cached, upstream_ms = await cached_call(
            catalog_cache,
            make_key("group_show", group_id, include_datasets),
            lambda: ckan.group_show(group_id, include_datasets=include_datasets),
            use_cache=use_cache,
        )
    except CkanAPIError as exc:
        return error_response(str(exc), upstream_error=exc.upstream_error)
    metadata = {"id": group_id, "include_datasets": include_datasets, "cached": cached}
    timing = {"upstream_ms": upstream_ms} if upstream_ms is not None else None
    return ok_response(result, metadata=metadata, timing=timing)


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.exception_handler(CkanAPIError)
async def ckan_error_handler(request: Request, exc: CkanAPIError):
    return error_response(str(exc), upstream_error=exc.upstream_error)


@app.exception_handler(httpx.HTTPStatusError)
async def httpx_error_handler(request: Request, exc: httpx.HTTPStatusError):
    return error_response(
        f"Upstream data.gov.ma returned HTTP {exc.response.status_code}",
        code=exc.response.status_code,
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    return error_response(str(exc), code=500)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
