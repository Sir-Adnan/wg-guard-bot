# mock_wg_panel

An **in-memory mock of the WG-Guard panel REST API** for automated integration tests.
It is a faithful test double of the contract in
[`docs/upstream-api/wg-guard-openapi.json`](../../docs/upstream-api/wg-guard-openapi.json)
(paths, scopes, request/response shapes and the single error envelope) — it is **not** the real panel:
no VPN, no database, no network egress. All state lives in one process and disappears on restart.

Dependencies: `fastapi`, `uvicorn`, `pydantic` (plus the stdlib). Nothing is imported from the parent project.

## Run it

The package lives in `tools/` (which has no `__init__.py`), so run the module from `tools/` — or put `tools/`
on `PYTHONPATH` first:

```powershell
# from <repo>\tools
..\.venv\Scripts\python.exe -m mock_wg_panel --host 127.0.0.1 --port 8787

# from the repo root
$env:PYTHONPATH = "$PWD\tools"; .venv\Scripts\python.exe -m mock_wg_panel --host 127.0.0.1 --port 8787

# equivalent uvicorn factory form
.venv\Scripts\python.exe -m uvicorn "mock_wg_panel.app:create_app" --factory --host 127.0.0.1 --port 8787
```

In tests, build an app directly — every app is a fresh, isolated store:

```python
from fastapi.testclient import TestClient
from mock_wg_panel import MockConfig, create_app

client = TestClient(create_app())
client.get("/healthz").json()                      # {"status": "ok"}
client.get("/api/v1/users", headers=AUTH).json()   # 401 without a token
```

```python
config = MockConfig(
    tokens={"wg_test_token": frozenset({"*"}), "wg_readonly": frozenset({"users.read"})},
    clock_offset_seconds=-90 * 24 * 3600,   # travel to the past (or + to the future)
)
app = create_app(config)
```

`MockConfig` fields: `tokens` (token → scopes, `"*"` = all), `plans` / `interfaces` (partial seed documents),
`clock_offset_seconds`, `seed_users`, `settings`, `node_id`, `version`, `ready`.

## Authentication

`Authorization: Bearer wg_<token>`; scopes come from the config.

| Token | Scopes |
| --- | --- |
| `wg_test_token` (default) | all |
| `wg_readonly_token` | every `*.read` scope |

Missing/unknown token → `401 UNAUTHORIZED`; known token without the required scope → `403 FORBIDDEN`.
Every error uses the documented envelope:

```json
{"error": {"code": "USER_NOT_FOUND", "message": "user usr_… not found", "request_id": "req_…"}}
```

Codes emitted by the mock: `UNAUTHORIZED`, `FORBIDDEN`, `INVALID_REQUEST`, `CONFLICT`, `USER_NOT_FOUND`,
`DEVICE_NOT_FOUND`, `TEMPLATE_NOT_FOUND`, `INTERFACE_NOT_FOUND`, `WEBHOOK_NOT_FOUND`, `DELIVERY_NOT_FOUND`,
`OPERATION_NOT_FOUND`, `USERNAME_EXISTS`, `DEVICE_LIMIT_REACHED`, `NODE_UNAVAILABLE`, `INTERNAL_ERROR`
(plus `NOT_FOUND`/`METHOD_NOT_ALLOWED` for framework-level misses). Any other documented code
(`TRAFFIC_EXCEEDED`, `RATE_LIMITED`, …) can be injected verbatim through `POST /__mock__/fail`.
Response bodies never carry private keys, webhook secrets or subscription tokens; request
logging emits method/path/status only (never bodies or secrets).

## Endpoints covered

* **Public** — `GET /healthz`, `GET /readyz`, `GET /api/v1/node/health`, `GET /openapi.json`, `GET /docs`.
* **Node** — `GET /api/v1/node`, `/node/stats`, `/node/telemetry` (`points` clamped to 1…180).
* **Purchases / operations** — `POST /api/v1/purchases` (atomic user + first device + subscription link,
  `Idempotency-Key` required, identical replay → same 201 + `Idempotency-Replayed: true`, different body → 409),
  `GET /api/v1/operations/result`; atomic `/users/{id}/quota/add` with before/after snapshots.
* **Successors** — `GET|PUT|DELETE /users/{id}/next-plan`, plus activation history. Terms are copied at queue time; reads simulate due time/quota activation and manual pause.
* **Users** — `POST /api/v1/users` (idempotent), `GET /api/v1/users` (cursor pagination `limit` ≤ 500,
  `sort`/`order`/`username`/`status`/`enabled`/`template_id`/`interface_id`/`traffic_exceeded`/`*_before`/`*_after`),
  `GET|PATCH|DELETE /api/v1/users/{id}`, `…/enable`, `…/disable`, `…/renew`,
  `…/traffic` (GET), `…/traffic/add|set|reset`.
* **Subscription** — `GET /api/v1/users/{id}/subscription`, `POST /api/v1/users/{id}/subscription/rotate`.
* **Devices** — `GET|POST /api/v1/users/{id}/devices`, `GET|PATCH|DELETE /api/v1/devices/{id}`,
  `/devices/{id}/enable|disable|regenerate`.
* **Config** — `GET /api/v1/devices/{id}/config` (`text/plain`, canonical AmneziaWG client config with
  `[Interface]`/`PrivateKey`/`Address`/`DNS`/`MTU`/`Jc`/`Jmin`/`Jmax`/`S1`/`S2`/`H1`–`H4` and `[Peer]`;
  `?format=json` → `{"config": "<same text>"}`), `GET /api/v1/devices/{id}/qr` (`image/png`).
* **Stats** — `GET /api/v1/stats`, `GET /api/v1/users/{id}/stats`, `GET /api/v1/devices/{id}/stats`.
* **Templates / interfaces** — full CRUD on `/api/v1/templates`, `/api/v1/interfaces` (delete refused while in use → 409).
* **Settings** — `GET /api/v1/settings`, `PATCH /api/v1/settings`.
* **Webhooks** — CRUD on `/api/v1/webhooks` plus `/redeliver` (202) and `/deliveries`, `/deliveries/{id}`.

Not implemented (out of scope for this mock): `/api/v1/users/bulk`, `/api/v1/users/bulk-action`.
The mock does not prove real tunnel enforcement, scheduler timing, reseller ownership,
or replay isolation across API principals/token rotation. Those require upstream integration tests.

## Test affordances (`/__mock__/*`, **not** part of the WG-Guard contract, no auth required)

| Call | Effect |
| --- | --- |
| `POST /__mock__/reset` | wipe users/devices/plans/webhooks/operations/idempotency/journal back to the seed |
| `GET /__mock__/requests` | JSON list of `{method, path, query, headers, body}` for every request received |
| `POST /__mock__/fail` | `{"path": "/api/v1/purchases", "count": 2, "status": 503, "body": {...}}` → fail the next N matching requests (omit `count` for "until reset"); a `body` of `{"error": {"code": "NODE_UNAVAILABLE"}}` is passed through with `request_id` filled in |
| `POST /__mock__/slow` | `{"path": "/api/v1/purchases", "seconds": 5, "count": 1}` → delay matching requests |
| `POST /__mock__/seed/plan` | add a plan (full `PlanPatch` body plus optional `id`) |
| `POST /__mock__/expire` | `{"user_id": "usr_…"}` (or `{"username": "…"}`) → force `status: "expired"`, `enabled: false` |

Matching is exact-path or sub-path (`/api/v1/users` also matches `/api/v1/users/{id}/…`). The `/__mock__/*`
calls themselves are journaled too — filter them out by path prefix when asserting on bot traffic.

## Notes / deliberate deviations

* `GET /api/v1/users` sorts `created_at` **descending** by default (other keys ascending); pass `order` explicitly.
* Template PATCH preserves omitted fields and clears explicit null limits; POST requires a name.
* Ordinary mutations replay for 24 hours. Purchases and quota top-ups retain results for 90 days.
* `plans` remains the internal seed-collection name for fixture compatibility; wire fields/scopes use templates.
* `PATCH /api/v1/settings` accepts unknown keys but type-checks keys that already exist in the registry.
* The client config always emits the AmneziaWG obfuscation lines (falling back to the document's example
  values) so the text endpoint is usable even for `plain` profiles.
* The user traffic series is derived from the user's charged counters (the aggregate of its devices).
* `POST /api/v1/webhooks` seeds one delivered receipt so `/deliveries` pagination is exercisable.
* The QR endpoint returns a syntactically valid PNG built with `zlib`/`struct` (no QR encoder dependency);
  it encodes the config text into a QR-shaped module matrix, not a scannable symbol.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tools/mock_wg_panel/test_mock_smoke.py -q
```

The suite drives the app through `fastapi.testclient` (and `httpx.ASGITransport` for async parity) and proves
auth, idempotent purchases, replay/conflict handling, config/QR output, pagination and fault injection.
