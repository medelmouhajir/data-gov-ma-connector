"""
Simple benchmark for the data-gov-ma-connector.

Assumes the skill is running locally with SKIP_AUTH=true on the given base URL.
It compares first-call (upstream) vs second-call (cached) latency and reports
payload sizes and cache hit behavior.
"""
from __future__ import annotations

import json
import sys
import time
from urllib.request import Request, urlopen

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def post(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}/{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    start = time.perf_counter()
    with urlopen(req, timeout=30) as resp:
        body = resp.read()
    elapsed = (time.perf_counter() - start) * 1000
    result = json.loads(body.decode("utf-8"))
    return {
        "duration_ms": elapsed,
        "body_bytes": len(body),
        "cached": result.get("metadata", {}).get("cached"),
        "upstream_ms": (result.get("timing") or {}).get("upstream_ms"),
        "count": (result.get("metadata") or {}).get("count"),
        "fields": (result.get("metadata") or {}).get("fields"),
    }


def run(label: str, endpoint: str, payload: dict) -> None:
    first = post(endpoint, payload)
    second = post(endpoint, payload)
    print(
        f"{label:35s}  "
        f"first={first['duration_ms']:7.1f}ms (up={first['upstream_ms'] or 0:6.1f}ms, bytes={first['body_bytes']:,}, cached={first['cached']})  "
        f"second={second['duration_ms']:7.1f}ms (up={second['upstream_ms'] or 0 if second['upstream_ms'] else 'None':>6}, bytes={second['body_bytes']:,}, cached={second['cached']})"
    )


print(f"Benchmarking against {BASE_URL}\n")

run("search_datasets (default fields)", "search_datasets", {"q": "population", "rows": 10})
run("search_datasets (full fields)", "search_datasets", {"q": "population", "rows": 10, "fields": "full"})
run("search_by_organization (anrt)", "search_by_organization", {"organization": "anrt", "rows": 10})
run("search_by_group (agriculture)", "search_by_group", {"group": "agriculture", "rows": 10})
run("list_organizations", "list_organizations", {"limit": 50})
run("list_groups", "list_groups", {"limit": 50})
run("list_resources", "list_resources", {"dataset_id": "statistiques-economiques-et-financieres-au-20-05-2015"})

print("\nDone.")
