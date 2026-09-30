# WG-Guard integration audit

Baseline: bot `b4a11ab`, with the user's replacement OpenAPI file already present.
The fixes cover the provider boundary, order/payment/provisioning flows, delivery,
renewal and quota callbacks, inbound webhooks, and their regression tests.

## Contract checked

- [Local official snapshot](upstream-api/wg-guard-openapi.json), SHA-256
  `cdeab391d7206aa916c52254cafc027a66af7895322b51314698bcc38a40cb0e`.
- Upstream reference commit `05869315e840f8d2beb4225bf49ffacd31ee9943`:
  [API guide](https://github.com/Sir-Adnan/wg-guard/blob/05869315e840f8d2beb4225bf49ffacd31ee9943/docs/architecture/api.md),
  [interactive documentation](https://github.com/Sir-Adnan/wg-guard/blob/05869315e840f8d2beb4225bf49ffacd31ee9943/internal/api/docs.html),
  [webhook contract](https://github.com/Sir-Adnan/wg-guard/blob/05869315e840f8d2beb4225bf49ffacd31ee9943/docs/integrations/webhooks.md).
- The refreshed local snapshot exactly matches upstream JSON at this commit.
  The user-provided file was preserved. The prior audit used an older snapshot;
  October changes are covered by `test_wg_contract_updates.py` and
  `test_multi_device_delivery.py`.

This contract uses `/templates` and `template_id`; the old wire `/plans` and
`plan_id` are absent. The bot still owns shop products, prices, receipts and
orders. Internal `Plan`, `plan_ref` and `wg_plan_id` names do not require a
database rename to map correctly to the vendor's technical templates.

## Units

| Quantity | Contract and implementation |
|---|---|
| Quota / charged usage | Integer bytes; both server RX (customer upload) and TX (download) consume quota |
| Default shop GB | Decimal: 100 GB = 100,000,000,000 bytes |
| Legacy binary choice | Explicit `shop.traffic_unit=gib` remains supported; new orders freeze their basis |
| Speed limits | Decimal Kbps; Mbps × 1000; panel MB/s × 8000; 512 Kbps displays as 0.512 Mbps |
| Live transfer rates | Telemetry B/s is not Kbps and is kept separate |
| Duration | Seconds; 30 days = 2,592,000 seconds; a month shortcut means 30 days |
| Dates | Timezone-aware RFC3339 instants; UTC node responses are not localized calendar strings |
| Signature timestamp | Unix seconds over exact received bytes, with a 300-second replay window |
| Limits | API null means unlimited/clear; a zero-byte remote quota remains finite and displays as zero |
| PATCH | Omission preserves state; null clears supported caps, while duration null is no change |

Existing service byte limits are not rewritten. Orders predating the basis
snapshot retain historical binary conversion. Installations without a stored
traffic-unit choice now use decimal GB for new orders and display; owners who
intend their existing catalog to remain binary must explicitly select `gib`.

## October follow-up

The refreshed contract adds atomic multi-device creation and clarifies UTF-8
name limits, duration/metadata patch exceptions and optional statistics. The bot
now preserves multi-device recovery/config delivery, checks template update
read-back, and uses a fresh template when unlimited duration cannot be patched.
Normal initial allocation stays one, distinct from the product device cap.
The web panel was also redesigned; see [PANEL-DESIGN.md](PANEL-DESIGN.md).

## Findings and fixes

| Priority | Defect | Resulting behavior |
|---|---|---|
| P1 | Removed plan paths/fields still sent to WG-Guard | Client sends current template routes, fields and scopes; the mock and independent contract tests reject old wire names |
| P1 | Recovery failures treated as an absent purchase | Only `OPERATION_NOT_FOUND` proves absence; forbidden, unavailable, malformed and unsupported recovery remain uncertain |
| P1 | Retry reconstructed username/key/node from mutable catalog data, and random `-r` substrings truncated the base key | Exact request and derived conflict variant are checkpointed; recovery retains the original node and payload |
| P1 | Paid order used current product terms | Each order provisions an immutable technical snapshot, including volume basis and reward policy |
| P1 | Concurrent provisioning could create duplicate local rows | PostgreSQL session advisory locks survive commits; one pinned connection serves locks and work; cancellation releases them |
| P1 | Paid top-up computed an absolute limit from cached quota | Atomic `/quota/add` adds allowance once and preserves RX/TX and expiry; lost results are recoverable |
| P1 | Auto-renew queued a free successor; paid renewal changed only expiry | Both callbacks enter paid checkout; one paid successor activates at time/quota exhaustion, with queue/history reconciliation |
| P1 | Pending successor could be silently replaced, or legacy/device purchase retried without recovery | A second paid successor is blocked; missing/incompatible queues require review; paid extra-device checkout and legacy retries are blocked |
| P1 | Wallet shortfall could still mark an order paid | Full debit is required before PAID; card transfers never debit a wallet or combine with an existing wallet reservation |
| P1 | Concurrent wallet mutations lost ledger consistency | Balance is refreshed under a row lock before credit, debit or correction; each mutation keeps its ledger entry in the transaction |
| P1 | Refund accepted an unpaid or excessive amount | Refund requires a recorded payment and cannot exceed the full paid total; paid orders cannot be canceled as unpaid drafts |
| P1 | Expired orders could refund again or become payable after reservation release | Expiry and cancellation share one locked cleanup path; expired orders cannot be paid; receipts awaiting review are not expired during operator delay |
| P1 | Last stock unit could be sold to several unfinished orders | Checkout reserves finite stock under a lock; savepoint rollback prevents partial checkout; unpaid cancellation/expiry releases it once |
| P1 | Webhook token fallback, concurrent dedupe and stale event transitions | Dedicated signing secret only; namespaced transactional dedupe; resource GETs reconcile current state; failed processing returns 503 and rolls back |
| P1 | Node error/validation text could expose credentials | Error messages use reviewed locale copy; arbitrary exception payloads are not persisted by the error reporter; webhook payload storage retains only reconciliation identifiers |
| P2 | Legacy display setting could expose stored Rial to customers | Legacy `rial` settings normalize to Toman; stored amounts stay integer Rial |
| P2 | Binary speed division and hidden upload cap | Download/upload display uses decimal Mbps, including fractional values |
| P2 | CSV always used binary volume and treated zero as unlimited | Export follows the configured basis and preserves finite zero limits |
| P2 | Plan drift skipped clearing limits, enabled/name/start policy | Full technical comparison restores all intended values; typed PATCH preserves null versus omission |
| P2 | Cashback/referral/discount calculation used money floats and could repeat | Exact Decimal Rial percentages; shared reward service applies frozen policy once across payment flows |
| P2 | New volume button sent customers to buy a separate service | It creates an explicit EXTRA_TRAFFIC order for the current product's volume and price, stating that expiry does not change |
| P2 | Cached service counts and stock lost concurrent increments | Database expressions update counters atomically; renewals do not consume another node service slot |
| P2 | Unknown node states implied active access | Responses remain readable, but unknown access states map conservatively to disabled |

Known customer copy was changed only around corrected flows. New copy is in
`fa.json`; button labels remain in `appearance.py`. Shared shipped copy is read
through `core/locales.py`, while the service layer injects operator overrides into the shared reader. First
text reads load overrides before resolving copy.
No runtime dependencies, environment variables, model columns or migrations
were added. No released migration or test guardrail was weakened.

## Upgrade and operating notes

- Use current WG-Guard with owner-operated automation credentials. Required
  grants include `templates.read/write`, `next_plans.read/write`,
  `purchases.create`, `operations.read`, `users.read/update`, device/config and
  subscription scopes. See [README](../README.md#connecting-to-a-wg-guard-panel).
  An old token containing only `plans.*` cannot serve the new contract.
- Review old `auto_renew` flags and pre-existing queues: the old toggle could
  have created an unpaid successor. The upgrade does not mutate real nodes or
  infer that an externally queued plan was paid. The bot prevents silent
  replacement/cancellation and asks for review.
- Paid renewal is a queued successor, not immediate replacement. Unlimited
  accounts with neither a duration nor an expiry/quota boundary cannot activate
  one, so the customer callback rejects that purchase before payment.
- A missing webhook never proves a purchase failed. Polling resource/queue and
  activation history still reconciles state. Failed reconciliation is not ACKed.
- Paid failures retain finite-stock reservation while their remote state is
  uncertain. A paid refund does not revoke a live entitlement or automatically
  restock it; the operator must reconcile that resource before restocking.
- Awaiting-review receipts retain their order/stock while the operator reviews
  them. Reject invalid receipts promptly; the payment deadline then governs the
  reopened unpaid order.
- The automated template workflow does not provision with a reseller-bound
  token, because those tokens must use owner-assigned templates. Direct owner
  entitlements and dedicated addon pricing can be added through the provider
  boundary without changing the stored money/secret invariants.

## Evidence and limits

Final executable/asset/contract tree SHA-256:
`1c7d2c14456f4949e69223bec3051e15eacd6335d2c47227ae01f5f3330673b7`.
Verification was recorded against a working tree based on Git commit `b4a11ab`.
The digest covers app/test/mock/migration source, templates, CSS/JS/font assets,
dependency/configuration files and the official snapshot. Docs-only edits after
verification do not invalidate the run.

| Command | Result |
|---|---|
| `.venv/Scripts/python.exe -m ruff check .` | Passed |
| `.venv/Scripts/python.exe -m pytest -q --tb=short` with `REQUIRE_DB=1` | 519 passed, zero failures/skips; 132.57 seconds |
| `node --check app/web/static/js/panel.js` | Passed |
| `node --check app/web/static/js/theme.js` | Passed |
| Parse every Jinja template | All 29 parsed |
| `git diff --cached --check -- . ':(exclude)app/web/static/fonts/OFL.txt'` | Passed; the upstream OFL license is preserved verbatim, including one trailing space |
| Chrome visual matrix and keyboard/form checks | See PANEL-DESIGN.md; no document overflow in checked combinations |

One existing Starlette/httpx deprecation warning remains. No dependency or test
was altered to silence it. The earlier 483-test run covered the prior snapshot;
the combined run above covers the refreshed contract and redesigned UI.

The complete suite uses the existing throwaway PostgreSQL `wgguard_test` on
localhost:55432 and `REQUIRE_DB=1`; WG-Guard calls use the in-process mock.
No owner server, real bot token, live database or real WG-Guard node was mutated.
No Docker container was created or removed for this audit.

Mock tests prove bot behavior and mappings, not live WireGuard/AmneziaWG shaping,
real device handshakes, reseller namespace isolation, or production rollout.
Alembic checks were not required because models/migrations did not change.
Image builds, real TLS and T4 deployment checks were not run because this is a
local code handoff, not a release or live deployment.
