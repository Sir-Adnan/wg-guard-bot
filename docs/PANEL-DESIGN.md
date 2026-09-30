# Atelier admin experience

The admin panel uses the **Atelier** presentation: an ivory workspace, navy
navigation, restrained champagne accents and locally bundled Vazirmatn type.
The default is light; dark mode is supported through the same semantic tokens.
This is a server-rendered Jinja/HTML/CSS/JavaScript system with no frontend build
step or new runtime dependency.

## Design direction

The audience is the shop operator. Priorities are pending payments, service
health, sales and support. The composition distinguishes workspace navigation,
page identity, primary actions, attention states and detailed records. Revenue
and counts always come from existing services; decorative elements never invent
growth, satisfaction, security or availability metrics.

- Navy sidebar with grouped, role-aware links, persistent desktop icon rail,
  and a mobile drawer with an explicit close action.
- Quiet toolbar, page heading and action area shared by every authenticated page.
- Dashboard overview, action shortcuts, aligned metric cards and existing SVG
  reports using theme-aware chart colors.
- Consistent cards, table regions, filters, status badges, forms, pagination,
  empty states, error presentation and modal footers.
- Plan forms group package details, technical limits, pricing and advanced
  display fields. Settings have a section index. Lengthy explanatory guidance
  can be expanded without obscuring the operator's main action.
- A distinct two-column login composition becomes a compact stacked layout on
  mobile. Business copy and account security remain in the existing layers.

## Presentation boundaries

| File | Responsibility |
|---|---|
| `templates/base.html` | Shared shell, breadcrumb, headings, actions and assets |
| `templates/partials/navigation.html` | Grouped navigation and role-compatible links |
| `templates/partials/workspace-dialogs.html` | Search and confirmation dialog markup |
| `templates/macros.html` | Existing reusable UI primitives and action contracts |
| `static/css/panel.css` | Semantic tokens, component treatment, responsive rules |
| `static/js/theme.js` | Allowlisted preference loading before first paint |
| `static/js/panel.js` | Navigation, focus, modal/confirmation, search and existing drag behaviors |
| `core/locales.py` / `locales/fa.json` | Shared shipped UI copy, with service-injected operator overrides |

`data-style="atelier"` is the stable identity of this admin experience. Theme
mode is a separate choice. Color, elevation, radius and type values belong in
the token section; new feature pages use existing primitives rather than adding
a separate brand palette. Bot button-preview colors represent the real Telegram
button choices and remain independent of the admin palette.

Preferences are local to the browser: `wggb-theme` accepts only `light`/`dark`,
and `wggb-nav-compact` accepts `0`/`1`. Invalid or inaccessible storage falls back
to a usable default. Selections do not grant permissions or modify shop data.
Locale, RTL direction, reduced motion and viewport are not style variants.

## Interaction and accessibility

- Skip link, named navigation, active-page semantics and visible focus states.
- Global section search with Ctrl/Command+K, using only links the staff member
  can access. It searches navigation labels, not customer records.
- Existing `data-modal-*` hooks remain. Modals contain keyboard focus, restore
  the opener's focus and keep their background inert while open.
- Existing `data-confirm` hooks remain. A native dialog confirms the existing
  message; unsupported dialog implementations use the browser confirmation.
  Cancel sends no mutation. The original submitter and CSRF fields are preserved.
- Busy feedback retains icons and submitted button values. Repeated form
  submissions are prevented while busy; browser history return resets the state.
- Tables scroll within named, keyboard-focusable regions. Dense records keep
  their information on narrow screens rather than dropping required columns.
- Reduced-motion rules, theme-aware status colors and local fonts. No font CDN
  is contacted during normal use.

Server role checks, signed sessions, CSRF, pricing and ordering remain
authoritative. The profile shell receives the authenticated staff context, and
logout now verifies CSRF before clearing the session.

## Evidence

Chrome inspection used a separate synthetic database and localhost-only preview
with background jobs and Telegram disabled. No real node or deployment was used.

- All 22 authenticated page routes: HTTP 200 and no document-width overflow at
  1440×900 and 393×852.
- Ten representative routes: no document-width overflow at 768×900 and 320×900.
- Dashboard, plans, receipts, buttons and settings: dark-mode inspection.
- Login, dashboard, records, settings, editor, error/empty states and plan modal
  inspected through shared components; local screenshots in `output/playwright`.
- Modal initial input focus, containment, Escape and return focus checked.
- Confirmation cancel produced no POST. Mobile drawer opening/closing and
  desktop collapse persistence checked in the browser.
- `tests/test_panel_experience.py` covers shell/assets, profile context, support
  navigation and CSRF-protected logout. Existing page and drag-contract tests
  remain in the suite.

This is representative visual and keyboard inspection, not a blanket WCAG
certification or a full browser compatibility matrix. Production rollout and
real WG-Guard handshakes remain separate checks.

## Font provenance

Vazirmatn variable WOFF2, version `v33.003`, is bundled from the
[official project](https://github.com/rastikerdar/vazirmatn/tree/v33.003).
The SIL Open Font License is retained beside it in `static/fonts/OFL.txt`.

Combined handoff: `.venv/Scripts/python.exe -m pytest -q --tb=short` with `REQUIRE_DB=1`
reported **519 passed, zero failures/skips**. JavaScript syntax, Ruff and diff
whitespace checks passed. See [WG-GUARD-AUDIT.md](WG-GUARD-AUDIT.md) for the tree
digest and upstream contract provenance.

The localhost preview process and its synthetic preview database were cleaned
up after inspection; screenshots remain available under `output/playwright/`.
The pre-existing PostgreSQL container and test database were left in place.
