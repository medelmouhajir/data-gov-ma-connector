# data-gov-ma-connector

ISLI Skill — National Open Data Portal Connector for Morocco.

Read-only connector to [data.gov.ma](https://data.gov.ma), Morocco's national open data portal, which runs on CKAN and exposes a public REST API at `/data/api/3/action/...`. This skill lets any ISLI agent search, browse, and discover Moroccan public datasets, organizations, groups, and downloadable resources without scraping.

## Why it exists

Rather than a standalone client skill, this is a **foundational data connector** meant to be consumed by market-research, feasibility-study, telecom, economic, or sector-analysis agents. It is especially useful for consultancies or investors evaluating Moroccan markets because it surfaces authoritative public data from ANRT, AMMC, Bank Al-Maghrib, and other Moroccan public bodies.

## Runtime model

Built for the ISLI Universal Skill Runtime v2.0:

- Dockerized HTTP microservice
- Exposes `GET /health` and `GET /.well-known/isli-manifest`
- Tool endpoints are `POST /{tool_name}` as declared in `isli-skill.yaml`
- Validates the `X-Internal-Auth` JWT against the `JWT_SECRET` injected by ISLI Core (can be disabled with `SKIP_AUTH=true` for local testing)

## Tools

| Tool | Description |
|------|-------------|
| `search_datasets` | Free-text + filtered search over datasets (with field projection & caching) |
| `get_dataset` | Full metadata for a dataset by name or id |
| `list_resources` | Downloadable resources for a dataset, uses a lightweight CKAN projection |
| `get_resource` | Metadata for a single CKAN resource |
| `search_resources` | Search across all downloadable resources |
| `search_by_organization` | Datasets published by a specific organization |
| `search_by_group` | Datasets within a thematic group/category |
| `list_organizations` | Publisher organizations |
| `list_groups` | Thematic groups/categories |
| `list_tags` | Dataset tags |
| `get_organization` | Full organization metadata |
| `get_group` | Full group metadata, optionally including datasets |

## Performance features

The skill is optimized for low-latency, repeated agent queries:

- **Field projection** — dataset search tools send a default `fl` (field list) to CKAN, returning only useful metadata instead of every CKAN extra/relationship. Pass `"fields": "full"` for the complete record, or a custom list like `["name","title","resources"]`.
- **In-memory TTL cache** — no external service required.
  - Catalogs (organizations, groups, tags): 1 hour.
  - Dataset/resource metadata: 5 minutes.
  - Search results: 60 seconds.
- **HTTP/2 + keep-alive + compression** — configured on the upstream httpx client.
- **Fast `list_resources`** — uses a lightweight CKAN search projection instead of the heavy `package_show` record.
- **Request timing** — every response includes `timing.upstream_ms` and an `X-Response-Time-Ms` header. Response metadata also includes `cached: true/false`.
- **Cache bypass** — pass `"use_cache": false` on any tool to force a fresh upstream fetch.

## Quick start (local)

```bash
# Install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run locally (auth disabled). PYTHONPATH=src is needed because source lives under src/.
SKIP_AUTH=true PYTHONPATH=src uvicorn main:app --reload

# Test health
open http://localhost:8000/health

# Test a search (no JWT needed when SKIP_AUTH=true)
curl -X POST http://localhost:8000/search_datasets \
  -H "Content-Type: application/json" \
  -d '{"q":"population","rows":5}'
```

## Build and run with Docker

```bash
docker build -t isli/data-gov-ma-connector .
docker run -p 8000:8000 -e JWT_SECRET=your-secret isli/data-gov-ma-connector
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `JWT_SECRET` | — | Secret used to validate `X-Internal-Auth` JWTs from ISLI Core |
| `SKIP_AUTH` | `false` | Set to `true` to bypass JWT validation for local development |
| `CKAN_BASE_URL` | `https://data.gov.ma/data` | Upstream CKAN base URL |
| `CKAN_TIMEOUT` | `30.0` | HTTP timeout for upstream calls |
| `PORT` | `8000` | Listen port |

## Registering the skill

Add an entry to the ISLI Skill Registry `index.json`:

```json
{
  "id": "data-gov-ma-connector",
  "name": "National Open Data Portal Connector — Morocco",
  "description": "Read-only connector to data.gov.ma (CKAN). Search datasets, browse organizations/groups, and discover downloadable resources for Moroccan market research.",
  "author": "ISLI AI",
  "git_url": "https://github.com/medelmouhajir/data-gov-ma-connector",
  "tags": ["morocco", "open-data", "ckan", "market-research", "connector"]
}
```

Then open a Pull Request against `medelmouhajir/isli-skills-registry`.

## License

MIT
