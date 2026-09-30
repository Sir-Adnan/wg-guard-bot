# Admin panel — page contract

This document is the interface every panel page must honour. It exists so that
pages can be written independently (and by different people) without breaking
the layout, the auth model or each other.

## 1. Where things live

The current Atelier experience is documented in [PANEL-DESIGN.md](PANEL-DESIGN.md).
Use the shared shell and semantic tokens for every page; presentation does not
change business services or permission requirements. The default is ivory/light
with a supported dark mode. Shared navigation and workspace dialogs are partials.

```
app/web/
  app.py            # FastAPI factory: mounts every router under PANEL_PREFIX
  deps.py           # Page/pagination + form parsing helpers
  security.py       # session cookie auth, CSRF, role dependencies
  templating.py     # Jinja env, filters, render(), flash()
  lifespan.py       # bot + scheduler startup
  routes/
    __init__.py     # ROUTE_MODULES registry (append your module here)
    <name>.py       # one module per feature area
  static/css/panel.css
  static/js/panel.js
  templates/
    base.html       # layout: sidebar, topbar, blocks
    macros.html     # icon, badge, stat, pagination, empty_state, ...
    partials/icons.html
    <name>.html     # one template per page
```

## 2. Route module shape

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Staff, StaffRole
from app.web.deps import (Page, form_bool, form_dict, form_int, form_money,
                          form_str, pagination)
from app.web.security import get_db_session, require_any, require_manager, require_owner, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["plans"])
```

Rules:

* The router is mounted with the panel prefix already applied, so declare paths
  **without** it: `@router.get("/plans")`, never `@router.get("/panel/plans")`.
* Every page handler depends on `staff: Staff = Depends(require_*())` and
  `session: AsyncSession = Depends(get_db_session)`.
* Role helpers:
  * `require_any()` — owner, admin and support (read-mostly pages)
  * `require_manager()` — owner + admin (catalogue & money)
  * `require_owner()` — owner only (staff, destructive globals)
  * Pick the **lowest** role that makes sense.
* Render with `render(request, "<name>.html", {...})`.
* Mutating routes are **POST-only**, call `verify_csrf(request, form["csrf_token"])`
  first, and end with `return redirect(url, message="…")` (PRG pattern).
* `redirect()` and `flash()` set a short-lived cookie that `base.html` turns into
  a toast — never render a "success" page for a form post.

### Handler template

```python
@router.get("/plans")
async def list_plans(
    request: Request,
    page: Page = Depends(pagination),
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    rows, total = await catalog.search(session, page.search, limit=page.size, offset=page.offset)
    page.total = total
    return render(request, "plans.html", {
        "page_title": "پلن‌ها",
        "page_subtitle": f"{total} پلن",
        "rows": rows,
        "page": page,
    })


@router.post("/plans/{plan_id}/delete")
async def delete_plan(
    plan_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))
    ...
    return redirect(f"{settings.panel_prefix}/plans", message="پلن حذف شد.")
```

## 3. Auto-provided context

`render()` injects these into **every** template — do not pass them yourself:

| name | meaning |
|---|---|
| `request` | the Starlette request |
| `staff` | logged-in `Staff` (or `None`) |
| `csrf_token` | token to embed in every POST form |
| `flash` | `{message, level}` once, after a redirect |
| `current_path` | used by the sidebar for the active state |
| `counters` | `.receipts`, `.tickets`, `.orders` badge numbers |
| `panels_offline` | number of nodes currently offline |
| `version`, `app_name`, `panel_prefix`, `currency_label`, `env` | globals |

## 4. Template skeleton

```jinja
{% extends "base.html" %}
{% from "macros.html" import icon, badge, pagination, empty_state, csrf with context %}

{% block title %}پلن‌ها{% endblock %}
{% block header %}پلن‌ها{% endblock %}
{% block page_actions %}
  <button class="btn btn--primary" data-modal-open="plan-modal" data-title="پلن جدید">{{ icon('plus') }} پلن جدید</button>
{% endblock %}

{% block content %}
<div class="card">
  <div class="card__body card__body--flush">
    <div class="table-wrap">
      <table class="table">…</table>
    </div>
  </div>
  {{ pagination(page, panel_prefix + "/plans") }}
</div>

<div class="modal" id="plan-modal" hidden>
  <div class="modal__panel">
    <form method="post" action="{{ panel_prefix }}/plans">
      {{ csrf(csrf_token) }}
      <div class="modal__head">
        <div class="modal__title" data-modal-title>پلن جدید</div>
        <div class="card__spacer"></div>
        <button class="btn btn--icon btn--ghost" type="button" data-modal-close>{{ icon('x') }}</button>
      </div>
      <div class="modal__body">
        <div class="form-grid">
          <div class="field">
            <label class="label" for="name">نام <span class="req">*</span></label>
            <input class="input" id="name" name="name" required>
          </div>
          …
        </div>
      </div>
      <div class="modal__foot">
        <button class="btn btn--ghost" type="button" data-modal-close>انصراف</button>
        <button class="btn btn--primary" type="submit">ذخیره</button>
      </div>
    </form>
  </div>
</div>
{% endblock %}
```

## 5. Available CSS classes

Layout: `card`, `card__head`, `card__title`, `card__sub`, `card__spacer`,
`card__body`, `card__body--flush`, `card__foot`, `grid`, `grid--stats`,
`grid--2`, `grid--3`, `grid--sidebar`, `row`, `row--between`, `col`.

Components: `btn` (`--primary --success --warning --danger --ghost --sm --icon
--block`), `input`, `select`, `textarea`, `textarea--code`, `input--mono`,
`switch`/`switch__track`/`switch__label`, `checkbox`, `field`, `label`, `hint`,
`form-grid`, `badge` (`--success --warning --danger --info --brand`), `dot`,
`chip`, `table`, `table-wrap`, `table--compact`, `empty`, `empty__title`,
`alert` (`--success --warning --danger --info`), `modal`, `modal__panel`
(`--sm`), `modal__head`, `modal__body`, `modal__foot`, `tabs`, `tab`,
`kv` (dl/dt/dd), `code`, `copyable`, `pagination`, `bar-list`, `bar-row`,
`tree`, `tree__row`, `tree__name`, `tree__actions`, `stat`, `stat__icon`,
`stat__label`, `stat__value`, `stat__hint`, `preview-phone`, `preview-bubble`,
`preview-btn` (`--primary --success --danger --link --default`), `emoji-grid`,
`emoji-cell`, `swatch--primary|success|danger|link|default`, `searchbar`,
`menu-board`, `menu-row`, `menu-palette`, `menu-chip` (`is-palette`,
`is-dragging`), `menu-chip__grip`, `menu-chip__label`, `menu-chip__remove`.

Utilities: `muted`, `dim`, `small`, `strong`, `mono`, `num`, `truncate`,
`text-success|danger|warning|info`, `text-center`, `mt-1|2|3`, `mb-2`, `w-100`,
`nowrap`, `sr-only`.

Icons: `{{ icon('name') }}` — see `templates/partials/icons.html`.

## 6. JavaScript behaviours (data attributes)

| attribute | effect |
|---|---|
| `data-modal-open="id"` | opens that modal; `data-set-<field>="value"` prefills inputs; `data-title="…"` replaces `[data-modal-title]` |
| `data-modal-close` | closes the nearest modal |
| `data-confirm="متن"` | confirm dialog on form submit, or on an `<a>` |
| `data-copy="value"` | click-to-copy with a toast |
| `data-filter-target="tableId"` | live client-side row filter for the input |
| `data-autosubmit` | submit the form on change/select |
| `data-nav-toggle`, `data-theme-toggle` | handled by the layout |
| `data-nav-close` | closes the mobile navigation drawer |
| `data-command-open` | opens allowlisted section navigation search |

Modals retain focus and restore their opener. `data-confirm` uses the shared
confirmation dialog with native-browser fallback; cancellation sends no POST.
Form submission keeps the original submitter's name/value and CSRF token.

Two behaviours have a contract of their own because a template and a script have
to agree on the exact attribute names. Both are **opt-in**: a page that renders
none of these attributes keeps working untouched.

### 6.1 Reordering a list — the drag-and-drop contract

Rendered by `plans.html`, `categories.html`, `channels.html`, `cards.html`,
`guides.html` and `panels.html`; handled by `initDragLists` in `panel.js`.

| attribute | meaning |
|---|---|
| `data-drag-list="<entity>"` | the container whose **direct children** are the rows; the value is the registry key in `app/services/ordering.py` |
| `data-drag-id` | one row of that list |
| `data-drag-handle` | the visible, focusable grip — a drag starts here and nowhere else |
| `data-drag-scope` | optional partition (a category's `parent_id`) |
| `data-drag-next` | optional return URL for the no-JavaScript path |

* A drop posts `entity`, `ids` (comma-separated, in the new order), `scope`,
  `next` and `csrf_token` to `POST {panel_prefix}/reorder` with
  `X-Requested-With: fetch`, and expects `{"ok", "changed", "message"}`
  (200 on success, 400 on a refused order, 403 on a bad token).
* The DOM is moved **before** the request goes out; a request that does not come
  back `ok` restores the markup captured before the move and toasts the server's
  sentence, so a failed move is never left on screen looking saved.
* `Alt`+`↑`/`↓` on the focused handle moves the row and posts through the same
  function.
* The no-JavaScript path is the same fields as an ordinary form post: 303 + flash.

### 6.2 The main-menu layout builder — the second drag contract

Rendered by `buttons.html`; handled by `initMenuBuilder` in `panel.js`.

| attribute | meaning |
|---|---|
| `data-menu-builder` | the card that owns the editor |
| `data-menu-url` | where a save goes (`POST {panel_prefix}/buttons/layout`) |
| `data-menu-max-rows`, `data-menu-max-buttons` | the caps, taken from `app/services/menu_layout.py` — never re-typed in the template |
| `data-menu-rows` | the container of the row containers |
| `data-menu-row` | one row of the menu |
| `data-menu-palette` | where the buttons that are **not** in the menu live |
| `data-menu-palette-note` | the "nothing left to add" line, shown when the palette is empty |
| `data-menu-key="<appearance key>"` | one chip (one menu button) |
| `data-menu-handle` | the focusable grip inside the chip |
| `data-menu-remove` | the ✕ inside a chip that is in a row |
| `data-menu-add-row` | appends an empty row (a drop zone) |
| `data-menu-save` | posts the whole arrangement |

Payload and answer:

* **With JavaScript** — `panel.js` posts `csrf_token` and `layout` (a JSON array
  of rows, each an array of keys) with `X-Requested-With: fetch` and reads
  `{"ok", "changed", "message"}`: 200 on success, 400 when the service refuses
  the arrangement (an unknown key, an empty menu), 403 on a bad CSRF token.
* **Without JavaScript** — the card renders each chip with a hidden
  `name="keys"` input and closes every row with `name="row_break"`; a plain form
  post therefore carries the whole arrangement in document order, and the answer
  is the usual 303 + flash. The ✕ is a real submit button
  (`name="remove_key"`), so a browser without scripts can still take one button
  out of the menu and save the rest in a single click. A chip that sits in the
  palette renders its input `disabled`, which is what keeps it out of that post.
* The rows *are* the model: a chip is only ever moved, so its label, emoji and
  hidden field travel with it. A drag or a click only edits the page — this
  editor saves the whole menu at once through the save button. `Alt`+`←`/`→`
  moves a focused chip along its row, `Alt`+`↑`/`↓` to the neighbouring row, and
  saves through that same function; a chip in the palette joins the last row
  first.
* A save that does not come back `ok` — a refusal, or an expired session, which
  answers with the login page instead of JSON — puts back the arrangement the
  server still has and toasts the message, so the page never shows an unsaved
  menu as if it were saved. On success the toast carries the server's sentence.

## 7. Formatting filters

`|money` (Rial→display unit + label), `|rial`, `|toman`, `|jdate`, `|jdatetime`,
`|jlong`, `|jshort`, `|bytes`, `|gb`, `|fa` (Persian digits), `|pct`, `|json`,
`|relative`, `|none_dash`.

**Money rule:** the database and every service call use **Rial** integers.
`|money` renders them in the shop's display unit. When an operator types a
price into a form, parse it with `form_money(form, "price")` which converts
Toman→Rial automatically (a trailing `ریال`/`تومان` overrides).

## 8. Checklist before a page is "done"

**Wiring** — a page that renders but is registered nowhere is invisible; a page
that is registered but unlisted in the smoke test breaks silently later.

1. The module is appended to `ROUTE_MODULES` in `app/web/routes/__init__.py`.
2. The path is added to `PAGES` in `tests/test_panel.py`, so the smoke test
   fetches it through a real login on every run. This is the step most often
   missed — the test-suite will not tell you that a page is missing.
3. If it is a top-level page, it has an entry in the sidebar list in
   `templates/base.html`; otherwise nobody will find it.
4. No path starts with `/panel` inside the router.

**Behaviour**

5. Every POST calls `verify_csrf` and ends in `redirect(...)`.
6. Every destructive action carries `data-confirm`.
7. Empty states use `empty_state(...)`.
8. Money goes through `form_money` / `|money` — never a raw division.
9. Persian copy is fluent, uses ZWNJ (‌) and Persian digits, and lives in the
   template (or `app/locales/fa.json` for bot-facing strings). See
   [`../AGENTS.md`](../AGENTS.md) §6.
10. The page renders inside `base.html` and does not add its own `<html>`.

**Evidence** — a page change is a T1/T3 change; see
[`VERIFICATION.md`](VERIFICATION.md) §3 for what to run. Adding or renaming a
path means `pytest tests/test_panel.py -q`.
