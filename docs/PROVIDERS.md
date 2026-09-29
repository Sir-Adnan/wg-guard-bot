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
[`upstream-api/openapi-wg-guard.json`](upstream-api/openapi-wg-guard.json). It is
**additive-only**: never assume a field is absent, and tolerate unknown fields —
every schema sets `extra="allow"` for exactly this reason.

### Idempotency

- `POST /api/v1/purchases` is idempotent for 90 days. **Reuse the same
  `Idempotency-Key` when retrying the same payload.** Only derive a new key
  (`<base>-r1`) after the node has *definitively* refused the body.
- After an ambiguous failure (timeout, 5xx) ask `GET /api/v1/operations/result`
  before retrying. This is what `recover_purchase()` wraps.
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

- Never point a test at a real node. `tools/mock_wg_panel` implements 61 of the
  63 documented endpoints in-process, with fault injection
  (`POST /__mock__/fail`) and a request journal (`GET /__mock__/requests`).
- `tests/test_wg_client.py` exercises the WG-Guard transport;
  `tests/test_panel_providers.py` pins the port contract and the canonical
  mapping for any adapter.
- Test the *mapping*, not the vendor: status normalisation, unknown-field
  tolerance, and the exactly-once guarantee are what break in practice.
