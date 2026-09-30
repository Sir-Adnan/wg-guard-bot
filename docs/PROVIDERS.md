# Panel providers

How this project talks to a VPN backend, and how to add another one.

Read this when you touch `app/panels/**`, when you add or change a node call, or
when you are wiring up a backend other than WG-Guard.

---

## 1. The shape

`app/panels` is a **port and adapter** boundary. The rest of the application
speaks the port and the canonical models; it never sees a vendor payload.

```
app/panels/
├── models.py            canonical DTOs — the only vocabulary the domain knows
│                        RemoteUser · RemoteDevice · PlanSpec · PurchaseResult …
├── base.py              the port: PanelProvider (ABC) + CAP_* capability flags
├── registry.py          kind → adapter class; drives the "panel type" dropdown
├── manager.py           node selection, one cached adapter per panel, health
├── client.py            WG-Guard HTTP transport      ┐ implementation details
├── schemas.py           WG-Guard payload models      ┘ of the adapter below
└── providers/
    ├── wgguard.py       WGGuardProvider — vendor payloads → canonical models
    └── example.py       a documented skeleton to copy
```

Three properties make this hold:

1. **A canonical vocabulary.** Vendor statuses, field names and error shapes stop
   at the adapter. `active | waiting_first_connection | disabled | expired |
   traffic_exceeded` is the only status set the domain knows, so reporting,
   reminders and the admin panel need no per-backend branch.
2. **Capabilities, not assumptions.** A backend that cannot queue a successor
   plan omits `CAP_NEXT_PLAN`; `provider.require(...)` turns an attempt into a
   clear Persian message instead of an `AttributeError`. A partial backend is a
   supported configuration.
3. **The hard guarantee lives in the port.** `purchase()` must be exactly-once
   per `idempotency_key`, and `recover_purchase()` returning `None` means "not
   committed, safe to retry". Those two sentences are the entire duplicate-account
   defence; every other vendor quirk is the adapter's problem.

`Panel.kind` selects the adapter at runtime, so a WG-Guard node and a PasarGuard
node can serve different plans in the same shop.

## 2. Adding a backend

1. Copy `app/panels/providers/example.py` to `providers/<vendor>.py`.
2. Implement the abstract methods against that vendor's HTTP API, mapping every
   response into the canonical models. Keep vendor-only fields in `.raw`.
3. Declare only the capabilities you really support (`CAP_*` in `base.py`).
4. Import the module in `app/panels/providers/__init__.py` so `@register` runs.
5. Run `pytest tests/test_panel_providers.py -q` — it pins the contract.

Nothing else changes. The admin panel's "panel type" dropdown is generated from
the registry, and provisioning, delivery, renewal, reminders and reporting keep
working because they only speak the canonical models.

### The rules that actually matter

- **Map, do not leak.** Return `app.panels.models` objects. Never return a vendor
  status string — map it onto the canonical set.
- **Be exactly-once where the port demands it.** If the vendor has no idempotency
  mechanism, persist a key → result map yourself. Never answer "probably".
- **Let `recover_purchase` answer honestly.** Returning `None` when the purchase
  *did* commit duplicates a customer's account.
- **Raise `app.core.errors` types**, not raw `httpx` errors, so the operator sees
  a Persian message instead of a traceback.
- **Never swallow `asyncio.CancelledError`.**
- **One `httpx.AsyncClient` per provider instance.** Creating one per request
  leaks connections and defeats keep-alive.

## 3. WG-Guard specifics

The vendor contract is
[`upstream-api/wg-guard-openapi.json`](upstream-api/wg-guard-openapi.json). It is
Response fields are **additive**: never assume a field is absent, and tolerate
unknown fields. Response schemas use `extra="allow"`; requests remain strict.

The current pre-installation contract names the wire resources `templates` and
`template_id`. Internal shop `Plan`, `plan_ref`, client `get_plan()` and the
legacy `wg_plan_id` column keep their names; the adapter maps them to technical
templates. This is not a migration of existing database columns. Response models
tolerate unknown fields; request models are strict and preserve explicit nulls.

Automatic template creation requires an owner-operated token with
`templates.read/write`. Renewals also need `next_plans.read/write`; quota top-ups
need `users.update` and result recovery needs `operations.read`. Reseller-bound
tokens require owner-assigned templates: automatic per-order template creation
is not currently a reseller provisioning workflow.

### October contract clarifications

Template names occupy at most 64 UTF-8 bytes; the adapter trims safely without
splitting a codepoint. Template PATCH `duration_seconds: null` is **no change**,
so finite-to-unlimited sync creates a new template and retains the old one for
existing users. Ordinary updates are read back and checked before a reference is
accepted. User duration PATCH does not recompute expiry; metadata PATCH is a
no-op, so billing metadata remains in the bot.

Atomic purchase accepts explicit `device_count` (1–100), independently of the
`device_limit` cap. Normal bot orders still allocate one ready device. The port
exposes `CAP_MULTI_DEVICE_PURCHASE` for explicit multi-device callers; purchase
and recovery results preserve every `device_ids` entry. Legacy operation results
without the array retain their primary `device_id`. Provisioning caches every
returned config with its own ciphertext, and delivery sends every ready config
with a primary QR; individual QR downloads remain available from device actions.

Current stats use charged byte counters and optional observations. Missing
handshake/window data does not imply a fabricated offline/zero observation;
traffic utilization can exceed 100%. The transport preserves these fields.

### Units and paid lifecycle

- Quotas and charged RX/TX are exact bytes. Default shop GB is decimal
  (`100 GB = 100000000000 B`); a stored `gib` choice remains supported. Each order
  freezes its basis; legacy orders retain the historical binary interpretation.
- Speeds are decimal kilobits/s: Mbps × 1000, panel MB/s × 8000. Server RX is
  customer upload; server TX is customer download. Telemetry B/s is a separate
  unit. Duration is seconds; a 30-day product is 2592000 seconds.
- Purchases and renewals create order-specific technical snapshots. Editing a
  shop SKU later cannot change a paid order. Node/template/username/key are
  checkpointed before the purchase, including derived username-conflict keys.
- Paid renewal queues one successor; it does not call expiry-only `renew_user`.
  The current configuration, expiry and usage remain until activation. Queue
  and activation-history reads recover a lost PUT even after its ordinary replay
  cache expires. A missing or incompatible paid successor remains visible for
  review rather than being silently replaced or canceled.
- `CAP_QUOTA_TOP_UP` uses atomic `/quota/add`; it never adjusts charged usage via
  `/traffic/add` or computes an absolute limit from a stale local cache.
- Ordinary replay lasts 24 hours; purchase/quota result retention lasts 90 days.
  Automatic retries of attempted orders older than 90 days require manual review.
- Paid extra-device orders are blocked before checkout and during legacy retry:
  the device-create endpoint has no recoverable atomic billing result. Adding a
  free device within an existing entitlement remains available.
- Finite catalog stock is reserved at checkout and released when an unpaid
  order is canceled or expires. Completed provisioning does not consume it again.
  Failed/paid orders retain their reservation while their external state is
  uncertain; restocking after a paid refund requires operator review.

### Idempotency

- `POST /api/v1/purchases` is idempotent for 90 days. **Reuse the same
  `Idempotency-Key` when retrying the same payload.** Only derive a new key
  (`<base>-r1`) after the node has *definitively* refused the body.
- After an ambiguous failure (timeout, 5xx) ask `GET /api/v1/operations/result`
  before retrying. This is what `recover_purchase()` wraps.
- Only a typed `OPERATION_NOT_FOUND` proves absence. Authentication, transport,
  schema errors and unsupported recovery propagate. Recovery does not depend on
  a subsequent user read succeeding. Unknown vendor statuses remain readable,
  but never imply active access.
- Never retry a mutation that has no idempotency key and is not naturally
  idempotent — `/devices/{id}/regenerate` is the canonical example. A retry there
  issues new keys and silently breaks a customer's existing configuration.

### Secrets

`config` and `subscription` responses are credentials:

- `Cache-Control: no-store` on the way out,
- never logged,
- encrypted at rest via `encrypt_secret(..., purpose="config" | "subscription")`.

### Other traps

- Plan drift is checked field by field before an update; a node that silently
  resets `duration_seconds` would otherwise change what customers bought.
- Health is reported as `online | degraded | offline`. A node with no enabled
  interface is `degraded`, not healthy — see `WGGuardProvider.health()`.
- The QR endpoint is optional. A failure there must never block delivery; the
  application renders the QR locally when the node declines.

## 4. Testing a provider

- Never point a test at a real node. `tools/mock_wg_panel` covers current
  template, quota and successor flows in-process, with fault injection
  (`POST /__mock__/fail`) and a request journal (`GET /__mock__/requests`).
- `tests/test_wg_client.py` exercises the WG-Guard transport;
  `tests/test_panel_providers.py` pins the port contract and the canonical
  mapping for any adapter.
- Test the *mapping*, not the vendor: status normalisation, unknown-field
  tolerance, and the exactly-once guarantee are what break in practice.
- `tests/test_upstream_contract.py` checks current fields against the official
  snapshot independently of the mock; `tests/test_provisioning_safety.py` tests
  lost replies, cross-session locks, stock, payment and paid successors.
- `tests/test_wg_webhooks.py` checks signatures, concurrent dedupe and authoritative
  resource reconciliation. The mock cannot prove live tunnel enforcement or
  reseller namespace isolation. See [the audit](WG-GUARD-AUDIT.md).
