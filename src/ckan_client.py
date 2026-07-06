"""
CKAN Action API client for data.gov.ma.

Exposes a small, read-only wrapper around the upstream CKAN v3 endpoints.
All methods return the deserialized JSON payload under the CKAN `result` key,
or raise `CkanAPIError` when the upstream reports failure.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from httpx import Limits

DEFAULT_BASE_URL = os.getenv("CKAN_BASE_URL", "https://data.gov.ma/data")
DEFAULT_TIMEOUT = float(os.getenv("CKAN_TIMEOUT", "30.0"))
SEARCH_TIMEOUT = float(os.getenv("CKAN_SEARCH_TIMEOUT", "10.0"))
CATALOG_TIMEOUT = float(os.getenv("CKAN_CATALOG_TIMEOUT", "15.0"))


class CkanAPIError(Exception):
    def __init__(self, message: str, upstream_error: dict | None = None):
        super().__init__(message)
        self.upstream_error = upstream_error or {}


class CkanClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        headers = {
            "User-Agent": "ISLI-AI-Connector/1.0 (+https://isli.ai; data.gov.ma CKAN client)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
        }
        limits = Limits(
            max_keepalive_connections=20,
            max_connections=50,
        )
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=timeout,
            headers=headers,
            limits=limits,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base_url}/api/3/action/{action}"
        kwargs: dict[str, Any] = {"json": payload or {}}
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = await self._client.post(url, **kwargs)
        response.raise_for_status()
        data = response.json()
        if not data.get("success", False):
            raise CkanAPIError(
                message=data.get("error", {}).get("message", f"CKAN action {action} failed"),
                upstream_error=data.get("error", {}),
            )
        return data.get("result")

    # Default lightweight field list for dataset search results.
    # Keeps enough metadata for browsing while cutting payload size vs full CKAN records.
    DEFAULT_DATASET_FIELDS = [
        "id",
        "name",
        "title",
        "notes",
        "metadata_created",
        "metadata_modified",
        "organization",
        "groups",
        "tags",
        "license_id",
        "license_title",
        "resources",
        "num_resources",
    ]

    async def package_search(
        self,
        q: str = "*:*",
        rows: int = 20,
        start: int = 0,
        sort: str = "score desc",
        fq: str | None = None,
        fl: list[str] | None = None,
        facet: bool = False,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "q": q,
            "rows": rows,
            "start": start,
            "sort": sort,
            "facet": str(facet).lower(),
        }
        if fq:
            payload["fq"] = fq
        # CKAN field projection: comma-separated list of fields.
        # An explicit empty list means "no projection / full record".
        fields = fl if fl is not None else self.DEFAULT_DATASET_FIELDS
        if fields:
            payload["fl"] = ",".join(fields)
        # If fields is an empty list, omit `fl` entirely to let CKAN return the full record.
        payload.update(kwargs)
        return await self._call("package_search", payload, timeout=timeout or SEARCH_TIMEOUT)

    async def package_show(self, id: str, timeout: float | None = None) -> dict[str, Any]:
        return await self._call("package_show", {"id": id}, timeout=timeout or DEFAULT_TIMEOUT)

    async def resource_show(self, id: str, timeout: float | None = None) -> dict[str, Any]:
        return await self._call("resource_show", {"id": id}, timeout=timeout or DEFAULT_TIMEOUT)

    async def resource_search(
        self,
        query: str,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "score desc",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "resource_search",
            {"query": query, "limit": limit, "offset": offset, "order_by": order_by},
            timeout=timeout or SEARCH_TIMEOUT,
        )

    async def organization_list(
        self,
        all_fields: bool = True,
        sort: str = "package_count",
        limit: int | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        payload: dict[str, Any] = {"all_fields": all_fields, "sort": sort}
        if limit is not None:
            payload["limit"] = limit
        return await self._call("organization_list", payload, timeout=timeout or CATALOG_TIMEOUT)

    async def organization_show(self, id: str, timeout: float | None = None) -> dict[str, Any]:
        return await self._call("organization_show", {"id": id}, timeout=timeout or DEFAULT_TIMEOUT)

    async def group_list(
        self,
        all_fields: bool = True,
        limit: int | None = None,
        sort: str = "name",
        timeout: float | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        payload: dict[str, Any] = {"all_fields": all_fields, "sort": sort}
        if limit is not None:
            payload["limit"] = limit
        return await self._call("group_list", payload, timeout=timeout or CATALOG_TIMEOUT)

    async def group_show(self, id: str, include_datasets: bool = True, timeout: float | None = None) -> dict[str, Any]:
        return await self._call(
            "group_show",
            {"id": id, "include_datasets": include_datasets},
            timeout=timeout or DEFAULT_TIMEOUT,
        )

    async def tag_list(self, query: str | None = None, limit: int = 100, timeout: float | None = None) -> list[str] | dict[str, Any]:
        payload: dict[str, Any] = {"limit": limit}
        if query:
            payload["query"] = query
        return await self._call("tag_list", payload, timeout=timeout or CATALOG_TIMEOUT)


def build_fq(
    organization: str | None = None,
    group: str | None = None,
    tags: list[str] | None = None,
    format: str | None = None,
) -> str:
    """Build a CKAN `fq` (filter query) string from high-level filters."""
    parts: list[str] = []
    if organization:
        parts.append(f'organization:"{organization}"')
    if group:
        parts.append(f'groups:"{group}"')
    if tags:
        escaped = [f'"{t}"' for t in tags]
        parts.append(f"tags:({' OR '.join(escaped)})")
    if format:
        parts.append(f'res_format:"{format.upper()}"')
    return " AND ".join(parts) if parts else ""
