# Security

This document describes the threat model and the controls in place. It is
written for operators deploying the bot and for reviewers auditing it.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private vulnerability reporting (Security → Report a vulnerability)
or contact the maintainer listed in the repository profile. Include:

- the affected version or commit,
- a description of the issue and its impact,
- reproduction steps or a proof of concept,
- any suggested mitigation.

You can expect an acknowledgement within 72 hours and a fix or a mitigation
plan within 14 days for high-severity issues.

## Threat model

| Asset | Who wants it | Primary control |
|---|---|---|
| WG-Guard API tokens | anyone with DB or log access | Fernet encryption at rest, never logged |
| WireGuard private configs | anyone who can read the DB | Fernet encryption, delivered only to the owning customer |
| Subscription links (bearer capabilities) | anyone who can read the DB | Fernet encryption, `no-store` on the node, never logged |
| Admin panel session | someone on the network | signed HttpOnly cookie, CSRF token, optional TLS |
| Customer wallets | a malicious customer | ledger-only balance changes, no client-supplied amounts |
| The node itself | a customer trying to escalate | all provisioning through the atomic `POST /purchases` |

## Controls

### Secrets at rest

Every secret column (`*_encrypted`) is Fernet-encrypted with a key derived from
`SECRET_KEY` through HKDF-SHA256 with a **purpose** label
(`panel-token`, `webhook-secret`, `config`, `subscription`). A ciphertext
produced for one purpose cannot be decrypted as another. `decrypt_secret()`
returns `None` on failure rather than raising, so a rotated `SECRET_KEY`
degrades into "please re-enter the token" instead of a crash loop.

Admin passwords use `hashlib.scrypt` (n=2¹⁴, r=8, p=1, 32-byte output) with a
16-byte per-password salt, compared in constant time.

### Session handling

- The panel session is a signed (`itsdangerous`) cookie holding a random
  session id, a staff id and the role. Content is authenticated, not encrypted —
  no secret is ever placed in it.
- `HttpOnly`, `SameSite=Lax`, and `Secure` when `PANEL_BEHIND_PROXY=true`.
- A role change invalidates existing sessions (the stored role is compared on
  every request).
- Sessions expire after 12 hours.
- Failed logins are counted per client IP; six failures trigger a five-minute
  lockout. A failing login still runs a dummy hash comparison so timing does not
  reveal whether a username exists.

### CSRF

Every state-changing panel route is POST-only and calls
`verify_csrf(request, form.get("csrf_token"))` **before** touching data. The
token is an HMAC of the session id, so it is bound to the session and cannot be
replayed across sessions.

### Authorisation

Three roles, enforced by FastAPI dependencies on every route:

| Role | May do |
|---|---|
| `owner` | everything, including staff management and advanced settings |
| `admin` | everything except staff management |
| `support` | review receipts, answer tickets, read the catalogue |

Guard rails enforced server-side: the last active owner cannot be demoted,
deactivated or deleted, and an operator cannot deactivate or delete themselves.

Bot-side, `IsStaff` / `IsAdmin` / `IsOwner` filters gate the operator handlers,
and staff bypass the forced-join and rate-limit middlewares.

### Untrusted input

- **Telegram messages**: user text is escaped with `html_escape` before being
  embedded in an HTML message. `parse_mode` is HTML, never Markdown.
- **Panel forms**: everything passes through typed helpers (`form_int`,
  `form_money`, `form_bool`, …); nothing is trusted, including hidden fields.
- **Templates**: Jinja autoescaping is on. The single `|safe` use is the guide
  preview, which renders operator-authored content for the operator only.
- **Callbacks**: all callback data uses typed `CallbackData` factories, so a
  malformed payload is rejected before it reaches a handler.
- **WG-Guard responses**: pydantic models with `extra="allow"` — a malicious or
  buggy node cannot inject a field that overwrites local state silently; every
  consumed field is named explicitly.

### Money integrity

- The wallet balance is only changed through `user_service.credit` /
  `debit` / `set_balance`, each of which writes a `Payment` ledger row **in the
  same transaction**. `sum(payments.amount_rial) == users.balance_rial` is an
  invariant covered by a test.
- Amounts are integer Rial; floats are never used.
- A card payment never debits the wallet (the money arrived externally), which
  prevents a customer who both tops up and pays by card from being charged
  twice.
- Discount codes are consumed inside the same transaction that creates the
  order; a failed order releases them.

### Idempotency and replay

`POST /api/v1/purchases` carries a stable `Idempotency-Key` per order. On an
ambiguous failure the client asks `GET /api/v1/operations/result` before
retrying, so a dropped connection can never produce a second VPN account for one
payment. The inbound WG-Guard webhook verifies
`X-WG-Signature: t=…,v1=HMAC-SHA256(secret, "<t>.<body>")` and rejects
timestamps outside a five-minute window; deliveries are deduplicated by
`payload.id`.

The receiver requires a dedicated encrypted webhook signing secret; the API
token is never a substitute. Deduplication is scoped to the configured panel,
inserted transactionally, and committed only after reconciliation. Delayed or
unordered events trigger authoritative resource reads; a failed read rolls back
the event and returns 503 so WG-Guard can retry. Stored event payloads retain
non-secret identifiers only. Telegram webhook requests require the configured
secret-token header, and registration logs never print the secret path.

Panel error responses and generic exception reports never persist arbitrary
vendor bodies or validation inputs, which can contain credentials. Reviewed
domain errors retain their customer explanation; generic reports retain their
type and source for operator correlation.

### Rate limiting

- Bot: 20 callbacks / 12 messages per 10 seconds per user. Staff are exempt;
  commands and media (receipt photos!) are never dropped.
- Panel login: six failed attempts per IP → five-minute lockout.
- Panel page loads: no explicit limit (they are authenticated and cheap).

### Transport

`httpx` verifies TLS certificates by default when talking to a WG-Guard node.
Do not disable it. `follow_redirects=False` prevents a compromised node from
bouncing an authenticated request to another host.

## Deployment checklist

- [ ] `SECRET_KEY` is 64 hex chars and unique to this deployment.
- [ ] `POSTGRES_PASSWORD` and `OWNER_PASSWORD` are strong and not reused.
- [ ] `OWNER_PASSWORD` was changed after the first login.
- [ ] The panel is behind TLS (`PANEL_BEHIND_PROXY=true` + a reverse proxy).
- [ ] Port 5432 (PostgreSQL) is **not** exposed to the internet.
- [ ] `ADMIN_IDS` contains only trusted Telegram ids.
- [ ] WG-Guard API tokens are scoped to the minimum needed and are separate per
      node.
- [ ] Backups are stored off the server (`backups/` is a volume).
- [ ] The panel port is firewalled if it does not need to be public.
- [ ] `docker compose logs` is reviewed after the first week of operation.

## Known limitations

- **A single process** runs the bot, the scheduler and the panel. There is no
  separation of privilege between them: whoever can read the process memory has
  every secret. This is a deliberate trade for a self-hosted single-tenant
  deployment.
- **Redis has no password** by default in the shipped compose file. It is not
  published to the host, but if you expose it, set `requirepass`.
- **The backup files are plain SQL.** They contain encrypted secrets but also
  the ledger; treat `backups/` as sensitive and copy it somewhere encrypted.
- **No 2FA** on the panel login. Use a strong unique password and keep the panel
  behind TLS.
- **The rate limiter is per process.** Scaling to multiple replicas would need a
  shared store (Redis already backs it; the FSM/limiter reads through the same
  `cache.store`).
