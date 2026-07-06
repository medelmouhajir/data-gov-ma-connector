# Performance Optimization Plan — data-gov-ma-connector

## Goal
Minimize end-to-end search/list latency: both the upstream fetch from `data.gov.ma` and the time the ISLI skill takes to execute and return results.

## Current bottlenecks

1. **No field projection.** `package_search` and `package_show` return every CKAN field (notes, extras, tracking_summary, relationships, all resources with full metadata). For list-style queries this is mostly wasted payload and parsing time.
2. **No caching.** Static catalog data (`organization_list`, `group_list`, `tag_list`) and repeated dataset metadata calls hit the upstream CKAN API every time.
3. **Heavy `list_resources`.** It calls `package_show` to get the full dataset just to extract `resources[]`. That is one of the heaviest CKAN endpoints for this use case.
4. **Default transport settings.** `httpx.AsyncClient` uses HTTP/1.1 defaults with no explicit connection limits, compression negotiation, or keep-alive tuning.
5. **No observability.** We cannot measure where time is spent, so we are optimizing blind.

## Proposed changes

### Phase 1 — Field projection (biggest upstream win, low risk)
- Add `fl` (field list) support to `package_search` so the tool can request only the fields it needs.
- Define a default lightweight field set for `search_datasets`, `search_by_organization`, and `search_by_group`:
  - `id,name,title,notes,metadata_modified,organization,resources,tags,license_id`
  - Still enough for browsing, but drastically smaller than full CKAN records.
- Add a `fields` parameter to the manifest for `search_datasets` / `search_by_*` so callers can request a custom projection.
- Use `fl` in the payload only when the user has not explicitly asked for the full record, preserving backward compatibility.

### Phase 2 — In-memory TTL cache (biggest repeated-query win)
- Introduce a small `TTLCache` helper (no extra service needed; use `cachetools` or a simple dict-with-expiry).
- Cache tiers:
  - **Catalogs**: `organization_list`, `group_list`, `tag_list` → TTL 1 hour (these change rarely).
  - **Dataset metadata**: `package_show` keyed by `id` → TTL 5 minutes.
  - **Search results**: keyed by hashed `{action, q, fq, rows, start, sort, fl}` → TTL 60 seconds.
- Add `use_cache` flag (default `true`) to every tool so callers can force a fresh fetch.
- Include `cached` boolean in response metadata for transparency.

### Phase 3 — Transport and connection tuning
- Configure `httpx.AsyncClient` explicitly:
  - Enable `http2=True`.
  - Set `limits=Limits(max_keepalive_connections=20, max_connections=50)`.
  - Add `Accept-Encoding: gzip, deflate, br`.
  - Keep the custom `User-Agent`.
- Lower the default read timeout to `10.0` for search/list calls (CKAN usually answers fast; keep a longer timeout for `package_show` if needed).

### Phase 4 — Lightweight `list_resources` path
- Change `list_resources` to use `package_search` with `fq=name:<dataset>` and `fl=id,title,resources` instead of `package_show`.
- Keep `get_dataset` returning the full record.
- If CKAN’s `fl` does not include nested resource details in search results, fall back to `resource_show` per resource only when filtering by `format`, or call `package_show` only as a fallback.

### Phase 5 — Batch / parallel tool (optional, higher value for agents)
- Add `search_datasets_batch` tool that accepts multiple `{q, fq, rows}` query objects and runs them concurrently with `asyncio.gather`.
- This lets a market-research agent fire "population", "telecom", "investissement" queries in one round trip instead of three sequential calls.

### Phase 6 — Observability
- Add an ASGI middleware that records:
  - total request duration
  - upstream CKAN call duration(s)
  - whether the result was cached
- Return timing in response metadata (`duration_ms`, `upstream_ms`, `cached`).
- Add a simple `/metrics` endpoint returning the last N timings or a rolling average.

## Recommended implementation scope for this iteration

Implement **Phase 1, Phase 2, and Phase 3**. They are self-contained, do not require new upstream endpoints, and give the largest speedup for the least complexity. Phase 4 is also high value and will be included if Phase 1 proves stable. Phase 5 and Phase 6 can follow.

## Files to change

1. `src/ckan_client.py`
   - Add `fl` support to `package_search`.
   - Add `limits`, `http2`, compression headers to `AsyncClient`.
   - Add a `timeout` override argument per call.
2. `src/cache.py` (new)
   - Simple `TTLCache` with `get`, `set`, `delete`, and optional cache key hashing.
3. `src/main.py`
   - Wire cache into catalog and dataset endpoints.
   - Add lightweight field sets to search endpoints.
   - Add timing middleware and metadata fields.
   - Optionally rewrite `list_resources` to use the lighter path.
4. `isli-skill.yaml`
   - Add `fields`, `use_cache` parameters to relevant tools.
   - Add `search_datasets_batch` if implementing Phase 5.
5. `requirements.txt`
   - Add `cachetools` (lightweight, no extra service).
6. `README.md`
   - Document performance params and cache behavior.

## Validation approach

1. Run the existing smoke tests to ensure no regression.
2. Add a timed benchmark script (`scripts/benchmark.py`) that compares:
   - old `search_datasets` vs new `search_datasets` with default `fl`
   - first `list_organizations` call (upstream) vs second call (cached)
   - `list_resources` via `package_show` vs the lighter path
3. Target: ≥50% reduction in payload size and ≥40% reduction in p95 latency for repeated catalog/dataset calls.
4. Confirm Docker build still passes syntax check.

## Trade-offs

- **Caching** means callers may see slightly stale data (max 5 min for datasets, 1 hr for catalogs). This is acceptable for a read-only open-data connector. Callers can set `use_cache=false` to bypass.
- **Field projection** hides some CKAN fields by default. Callers can pass `fields=[]` or use `get_dataset` for the full record.
- **HTTP/2** can occasionally cause issues with specific reverse proxies. We will keep fallback to HTTP/1.1 if the server does not negotiate h2, so this is safe.
