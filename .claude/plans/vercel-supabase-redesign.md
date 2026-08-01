# Plan: Vercel + Supabase Migration & UI Redesign

## Context

Migrating a Flask "Locket Gold Unlocker" app from VPS (SQLite + background worker threads polling a queue) to Vercel serverless + Supabase Postgres, with a fully redesigned modern UI for **single-operator livestream demo use** (not public multi-user queue).

**Current architecture incompatibilities with Vercel serverless:**
1. Background daemon threads (`QueueManager` workers polling SQLite every 0.5s) — no persistent process
2. SQLite file `locket.db` — ephemeral/read-only filesystem, not shared across invocations
3. `.mobileconfig` file stored on disk at `locket/static/` — won't persist across deployments
4. Gunicorn + `workers=1` + long-lived process assumption — serverless is stateless per-request

**User requirements (from questions):**
- **Single-operator only**: user (owner) is the only one who accesses the site, enters customer usernames live on stream for demo — no public queue, no multi-user concurrency needed
- **Supabase**: requested for database (Postgres via Supabase client)
- **Password protection**: entire site behind one shared password (like current `/admin` login)
- **Modern, polished UI**: "to vào, đẹp vào" (big, beautiful) for livestream presentation

**Design decision**: Remove the queue system entirely (it was built for multi-user concurrency, which is now unnecessary). Convert to **fully synchronous single-request unlock flow**: user types username → submits → HTTP request blocks while backend calls Locket API → returns success/failure in one go. Simpler, Vercel-native, fits single-operator use case perfectly.

---

## Architecture Changes

### 1. Database: SQLite → Supabase Postgres

**Migration strategy:**
- Create Supabase project, export connection URL + anon/service keys to Vercel env vars
- Install `supabase>=2.31.0` (latest as of 2026-08, per PyPI search results)
- Replace all raw `sqlite3.connect(...)` with Supabase Python client calls (REST API via PostgREST)
- Schema mapping (8 tables total):

| SQLite table | Supabase table | Changes |
|---|---|---|
| `accounts` | `accounts` | Keep schema: `slot_id TEXT PK, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, added_at TIMESTAMPTZ NOT NULL` |
| `tokens` | `tokens` | `id SERIAL PK, payload JSONB NOT NULL, added_at TIMESTAMPTZ NOT NULL` |
| `queue_requests` | **REMOVED** | No longer needed (no queue in single-operator sync flow) |
| `processing_times` | **REMOVED** | Queue metrics, not needed |
| `recent_log` | `recent_log` | Keep for activity history: `id SERIAL PK, username TEXT, status TEXT, error TEXT, duration REAL, completed_at TIMESTAMPTZ` (drop `client_id`/`slot_id` — not queue-specific) |
| `site_settings` | `site_settings` | Keep: `key TEXT PK, value JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL` |
| `proxies` | `proxies` | Keep: `id SERIAL PK, url TEXT UNIQUE NOT NULL, enabled BOOLEAN DEFAULT true, added_at TIMESTAMPTZ, last_ok_at TIMESTAMPTZ, last_err_at TIMESTAMPTZ, last_err TEXT` |
| `mobileconfig_history` | `mobileconfig_history` | Keep: `id SERIAL PK, action TEXT CHECK(action IN ('upload','delete')), filename TEXT, size INT, signed BOOLEAN, created_at TIMESTAMPTZ NOT NULL` |

**New table for file storage:**
- **Supabase Storage bucket** `mobileconfigs` for the `.mobileconfig` file (replacing filesystem storage)
- Or inline as `site_settings` key `mobileconfig_file: {data: base64, size, signed, updated_at}` if keeping it simple (single file, <5MB, rarely changes) — **recommend this simpler approach**

**Thread-local connection removal:** Replace `db.get_conn()` (thread-local sqlite3.Connection) with module-level Supabase client singleton initialized once from env vars. No thread-local needed since Supabase client is stateless/HTTP-based.

### 2. Queue System Removal

**What's being deleted:**
- `locket/queue_manager.py` — entire file (533 lines)
- Public routes: `/api/restore` (queue add), `/api/queue/status` (poll), `/api/queue/global-status` (aggregate)
- Admin route: `/api/queue` (snapshot)
- `QueueManager` instantiation in `create_app()`
- `add_worker`/`remove_worker` calls in admin account CRUD routes

**What's being kept (extracted logic):**
- Core unlock flow from `QueueManager._process_request` (lines 494-532) → new synchronous function `unlock_user(username, slot_id)` in a new module `locket/unlock.py`:
  1. `api.getUserByUsername(username)` → extract `uid`
  2. `api.restorePurchase(uid)` → validate `Gold` entitlement `product_identifier` in `SUBSCRIPTION_IDS`
  3. `send_telegram_notification(...)` on success
  4. Insert into `recent_log` (success/failure)
  5. Return `{success: bool, message: str, duration: float}`
- `SUBSCRIPTION_IDS` constant moves to `unlock.py`
- Error messages preserved exactly (see §10 below)

**Retry/401 handling:** Keep `call_on_slot` logic (401 → `rotator.refresh`; 5xx → backoff + retry up to 8 attempts with jitter) — move it into `unlock.py` as a helper, or inline into `unlock_user` since it's now the only caller. Decision: **inline it** — simpler, one function does the whole unlock with retries built-in.

### 3. AccountRotator Simplification

**Current behavior:**
- In-memory cache of `Auth`/`LocketAPI` instances per slot, background refresher thread every 60s
- `TOKEN_TTL_SEC = 5*60` — proactive refresh when token age ≥ 5min

**New behavior (serverless-compatible):**
- **Remove background `_refresher_loop` thread** — no persistent process for it to run in
- **Lazy refresh on-demand**: `ensure_fresh(slot_id)` checks token age, refreshes if stale, returns `LocketAPI` — this is already in the code (lines 83-98), just remove the background loop
- **Cache in-process for warm container reuse**: Vercel Python functions can stay warm for ~5min between invocations (undocumented but observed behavior) — keep the in-memory `_slots` dict so warm invocations reuse tokens, but accept that cold starts rebuild from scratch (Auth login on first use)
- **Caveat**: first unlock after cold start will be ~1-2s slower (Firebase login + Locket API token fetch) — acceptable for single-operator use, show a "logging in..." spinner in UI

### 4. Vercel Deployment Structure

**New file structure:**
```
api/
  index.py          # Flask app entrypoint (Vercel auto-detects this)
locket/             # existing package, refactored
  __init__.py       # create_app() — simplified, no QueueManager/worker threads
  config.py         # keep as-is
  db.py             # NEW: Supabase client init, replaces sqlite3 logic
  rotator.py        # keep, remove _refresher_loop thread
  unlock.py         # NEW: extracted unlock_user() + SUBSCRIPTION_IDS
  locket_auth.py    # keep as-is (Auth class)
  locket_api.py     # keep as-is (LocketAPI class)
  tokens.py         # keep, replace DB queries with Supabase
  notifications.py  # keep as-is
  site_settings.py  # keep, replace DB with Supabase
  proxies.py        # keep, replace DB with Supabase
  admin/
    __init__.py
    auth.py         # keep as-is
    routes.py       # keep most routes, remove queue endpoints, simplify account add/remove (no worker spawn/teardown)
  public/
    __init__.py
    routes.py       # NEW: single synchronous /api/unlock endpoint, remove queue routes
  templates/
    index.html      # NEW: fully redesigned UI (see §5)
    admin.html      # keep structure, simplify (no queue panel)
    admin_login.html # keep
    login.html      # NEW: site-wide password gate
  static/
    (empty or minimal — Vercel serves public/ dir from CDN)
public/             # NEW: static assets served from Vercel CDN
  favicon.ico
  (any images/css if externalized)
requirements.txt    # Flask, supabase, requests, python-dotenv
vercel.json         # NEW: Flask config, maxDuration, env handling
.env.example        # updated env var list
```

**`api/index.py`** (Vercel entrypoint):
```python
from locket import create_app
app = create_app()
```

**`vercel.json`**:
```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "functions": {
    "api/index.py": {
      "maxDuration": 60
    }
  }
}
```
Notes:
- `maxDuration: 60` — unlock can take 10-30s with retries/backoff, need headroom
- No `builds`/`routes` keys (legacy `@vercel/python` builder pattern) — modern Vercel auto-detects Flask from `api/index.py` per [official docs](https://vercel.com/docs/frameworks/backend/flask)
- Env vars set in Vercel dashboard (not in `vercel.json` — that's for config only)

### 5. UI Redesign (index.html)

**Current state:** 1744 lines (1042 CSS, 525 JS, 163 HTML), custom CSS variables + theme system, SweetAlert2 for modals, no framework, queue polling + countdown logic

**New design goals:**
- **Livestream-optimized**: large text, high contrast, clear status indicators visible on stream
- **Single-screen flow**: no multi-step modals, everything on one page
- **Real-time feel**: optimistic UI (show spinner immediately), no polling (synchronous request)
- **Modern aesthetic**: keep the gold theme + animated background, but cleaner layout, bigger CTAs, smooth transitions

**New structure (~800 lines target, simpler than current 1744):**

1. **Password gate** (if not logged in):
   - Full-screen centered card: logo, "Enter Access Code" input, submit button
   - On success: `session["authenticated"]=True`, reload to main UI
   - Styled like current admin login but with the gold theme

2. **Main UI** (after login):
   - **Hero section**: logo + animated background (keep existing `bg-glow` orbs), centered "Locket Gold Unlocker" title, logout button (top-right)
   - **Unlock card** (main CTA, large):
     - Big username input (placeholder: "Enter Locket username")
     - "Check User" button → synchronous POST `/api/get-user-info` (preview profile, existing route)
     - On success: show avatar + name confirmation card (inline below input, not modal)
     - "Unlock Gold" button (only visible after check) → POST `/api/unlock` (new route, blocks until done)
     - Progress states: idle → checking (spinner + "Looking up user...") → confirmed (avatar shown) → unlocking (spinner + "Unlocking Gold..." + elapsed timer counting up) → success (green checkmark + "Unlocked!" + duration) or error (red X + error message)
   - **Recent Activity panel** (below unlock card):
     - List of last 30 unlocks from `recent_log` (username masked, status, duration, timestamp)
     - Auto-refresh every 10s (simple `setInterval` fetch)
   - **iOS Profile Download** (bottom card):
     - "Download iOS Profile" button → `/api/mobileconfig` (existing route)
     - Instructions collapsed by default, expand on click

**Removed sections:**
- Queue status badge (no queue)
- Instructions tabs (simplify to single iOS note)
- Tutorial video button (can keep if user wants, but deprioritize)
- Platform tabs (focus on iOS since that's what `.mobileconfig` is for)

**CSS approach:**
- Keep CSS variables + gold theme from current `index.html`
- Keep animated background (`bg-glow` orbs)
- Simplify: remove 4 theme variants (aurora/sunset/mono), keep only gold — single-operator doesn't need theme switching
- Remove layout variants (stacked/split/spotlight) — one layout only
- Use CSS Grid for main layout (hero, unlock card, recent activity)
- Keep SweetAlert2 for error popups (already loaded), but prefer inline status indicators over modals

**JS approach:**
- Vanilla JS (no framework) — current codebase pattern
- `checkUserInfo()` → fetch `/api/get-user-info`, render profile confirmation inline
- `unlockGold(username)` → fetch `/api/unlock` (sync, may take 10-30s), show elapsed timer during request
- `fetchRecentActivity()` → fetch `/api/recent-history`, render list, `setInterval` every 10s
- No polling loop (was for queue status, not needed)

### 6. New Public API Endpoints

**Removed:**
- `POST /api/restore` (queue add)
- `POST /api/queue/status` (poll)
- `GET /api/queue/global-status`

**Kept:**
- `GET /` → renders `login.html` if not authenticated, else `index.html`
- `POST /api/get-user-info` — unchanged (sync user lookup)
- `GET /api/mobileconfig` — unchanged
- `GET /api/site-settings` — keep (though may simplify if removing theme/layout switching)
- `GET /api/recent-history` — unchanged

**New:**
- `POST /api/unlock` (request: `{username}`, response: `{success, message, duration, uid, product_id}` or `{success: false, error}`) — calls `unlock.unlock_user(username, slot_id)` synchronously, picks `slot_id` via round-robin (new helper in `rotator.py`: `next_slot_round_robin()` — track last-used index in module var or just random since single-operator has no contention)
- `POST /auth/login` (request: `{password}`, response: `{success}` or 401) — checks `PASSWORD` env var (single shared password), sets `session["authenticated"]=True`
- `POST /auth/logout` — clears session, redirects to `/`

**Site-wide auth decorator** (new, in `public/auth.py`):
```python
def auth_required(f):
    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return redirect("/")
    return f(*args, **kwargs)
```
Applied to all routes except `/`, `/auth/login`, `/auth/logout`.

### 7. Admin Panel Simplification

**Keep:**
- `/admin/login`, `/admin/logout`, `/admin` (dashboard)
- `/admin/api/accounts` (GET/POST/DELETE) — remove `queue_manager.add_worker`/`remove_worker` calls, just CRUD `accounts` table + `rotator` sync
- `/admin/api/accounts/test` (test login)
- `/admin/api/tokens` (GET/POST/DELETE)
- `/admin/api/popup`, `/admin/api/maintenance`, `/admin/api/theme`, `/admin/api/layout` (site settings)
- `/admin/api/proxies/**` (proxy pool management)
- `/admin/api/mobileconfig` (upload/download/delete) — change to Supabase Storage or site_settings key

**Remove:**
- `/admin/api/queue` (no queue)

**Admin UI (`admin.html`) changes:**
- Remove "Queue Status" panel (was showing processing/waiting counts, worker list)
- Keep Accounts, Tokens, Proxies, Site Settings, Mobile Config panels
- Simplify Accounts panel: no "Worker Status" column (was showing thread running/stopped)

### 8. Environment Variables

**New/changed:**
```bash
# Supabase (replaces LOCKET_DB)
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=eyJxxxx...     # service_role key for admin operations (keep secret!)

# Site auth (NEW — replaces no public auth)
SITE_PASSWORD=your-livestream-access-code

# Admin auth (existing)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin-panel-password

# Locket accounts seed (existing, optional)
EMAIL=locket-account@example.com
PASSWORD=locket-password

# Tokens fallback (existing, optional)
gist_token_url=https://gist.githubusercontent.com/.../raw/.../tokens.json

# Flask (existing)
FLASK_SECRET_KEY=random-secret-for-sessions
BEHIND_HTTPS=1              # always 1 on Vercel (HTTPS only)

# Telegram notifications (existing, optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

**Removed:**
- `LOCKET_DB` (SQLite path) — replaced by `SUPABASE_URL`/`SUPABASE_KEY`

### 9. Supabase Client Wrapper (`locket/db.py` rewrite)

**New API (replacement for `db.get_conn()`):**
```python
# locket/db.py
from supabase import create_client, Client
import os

_client: Client = None

def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        _client = create_client(url, key)
    return _client

def init():
    """Ensure schema exists (idempotent). Run once on first request per container."""
    client = get_client()
    # Supabase schema is managed via Supabase dashboard SQL editor or migrations
    # — no equivalent to CREATE TABLE IF NOT EXISTS here, assume tables exist
    # (create them manually in Supabase or via a SQL migration script checked into repo)
    pass
```

**Migration script** (one-time, run locally or via Supabase SQL editor):
```sql
-- accounts
CREATE TABLE IF NOT EXISTS accounts (
  slot_id TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_accounts_added_at ON accounts(added_at);

-- tokens
CREATE TABLE IF NOT EXISTS tokens (
  id SERIAL PRIMARY KEY,
  payload JSONB NOT NULL,
  added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- recent_log (simplified, no queue fields)
CREATE TABLE IF NOT EXISTS recent_log (
  id SERIAL PRIMARY KEY,
  username TEXT,
  status TEXT,
  error TEXT,
  duration REAL,
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_recent_log_completed ON recent_log(completed_at DESC);

-- site_settings
CREATE TABLE IF NOT EXISTS site_settings (
  key TEXT PRIMARY KEY,
  value JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- proxies
CREATE TABLE IF NOT EXISTS proxies (
  id SERIAL PRIMARY KEY,
  url TEXT UNIQUE NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_ok_at TIMESTAMPTZ,
  last_err_at TIMESTAMPTZ,
  last_err TEXT
);

-- mobileconfig_history
CREATE TABLE IF NOT EXISTS mobileconfig_history (
  id SERIAL PRIMARY KEY,
  action TEXT NOT NULL CHECK (action IN ('upload','delete')),
  filename TEXT,
  size INT,
  signed BOOLEAN,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mc_history_created ON mobileconfig_history(created_at DESC);
```

**CRUD wrappers** (replace raw SQL in each module):
- `rotator.py`: `_load_accounts()` → `client.table("accounts").select("*").order("added_at").execute()`, `add(...)` → `client.table("accounts").insert({...}).execute()`, etc.
- `tokens.py`: `_read_local()` → `client.table("tokens").select("payload").order("id").execute()`
- `site_settings.py`: `_read(key)` → `client.table("site_settings").select("value").eq("key", key).maybe_single().execute()`, `_write(...)` → upsert
- `proxies.py`: similar pattern
- `admin/routes.py`: `mobileconfig_history` inserts/selects

### 10. Critical Behavior Preservation

**Error messages** (must match exactly for any external monitoring/logs):
- `"User not found or API error"`
- `"User data not found"`
- `"UID not found for user"`
- `f"Restore purchase failed. Gold entitlement not found for {username}."`
- Success: `f"Purchase {product_identifier} for {username} successfully!"`

**SUBSCRIPTION_IDS validation:** Keep list exact:
```python
SUBSCRIPTION_IDS = [
    "locket_1600_1y",
    "locket_199_1m",
    "locket_199_1m_only",
    "locket_3600_1y",
    "locket_399_1m_only"
]
```

**Retry logic:** 401 → refresh token, retry once on same slot; 5xx → backoff with jitter, up to 8 attempts; non-transient errors → fail immediately.

**Telegram notification:** Keep exact message format (HTML, Gold entitlement JSON pretty-print).

**Username masking in recent_log:** Keep `_mask_username` helper (shows first 2 + last 2 chars, masks middle with `*`).

**Proxy pool rotation:** Keep `proxies.next_proxy()` round-robin + mark_ok/mark_err logic in `locket_auth.py` and `locket_api.py`.

---

## Implementation Order

### Phase 1: Backend Infrastructure (no UI changes yet)
1. Create Supabase project, run schema migration SQL, export env vars
2. Install `supabase` in `requirements.txt`
3. Rewrite `locket/db.py` → Supabase client wrapper + `init()` stub
4. Update `rotator.py`:
   - Replace `accounts` table queries with Supabase calls
   - Remove `_refresher_loop` thread start (delete lines 110-119)
   - Keep `ensure_fresh` lazy refresh logic
5. Update `tokens.py` → Supabase queries
6. Update `site_settings.py` → Supabase queries
7. Update `proxies.py` → Supabase queries
8. Extract `unlock.py`:
   - Copy `_process_request` logic from `queue_manager.py` (lines 494-532)
   - Inline `call_on_slot` retry logic (401/5xx handling)
   - Add `insert_recent_log(...)` helper (Supabase insert)
   - Export `unlock_user(username, slot_id) -> dict`
9. Test locally with Supabase (via `python wsgi.py` or `vercel dev`) — verify account/token CRUD, unlock flow works

### Phase 2: Remove Queue System
10. Delete `locket/queue_manager.py`
11. Update `locket/__init__.py` (`create_app`):
    - Remove `app.queue_manager = QueueManager(app.rotator)` (line 43)
    - Remove import
12. Update `locket/public/routes.py`:
    - Remove `/api/restore`, `/api/queue/status`, `/api/queue/global-status` routes
    - Add `POST /api/unlock` → calls `unlock.unlock_user(username, rotator.next_slot_round_robin())`
13. Update `locket/admin/routes.py`:
    - Remove `/api/queue` route
    - Remove `queue_manager.add_worker(slot_id)` from `POST /api/accounts` (line 86)
    - Remove `queue_manager.remove_worker(slot_id)` from `DELETE /api/accounts/<slot_id>` (line 105)
14. Add `rotator.next_slot_round_robin()` helper (module-level counter or random choice)
15. Test: unlock via new `/api/unlock` endpoint (Postman/curl)

### Phase 3: Site-Wide Auth
16. Create `locket/public/auth.py`:
    - `auth_required` decorator (checks `session["authenticated"]`)
17. Add `POST /auth/login`, `POST /auth/logout` routes to `public/routes.py`
18. Create `locket/templates/login.html` (simple password gate, gold theme)
19. Update `GET /` route: check session, render `login.html` or `index.html`
20. Apply `@auth_required` to all public routes except `/`, `/auth/*`
21. Test: login gate works, admin login still independent

### Phase 4: UI Redesign
22. Redesign `locket/templates/index.html`:
    - Simplify to ~800 lines (half of current 1744)
    - Keep gold theme + animated background
    - New single-screen layout (see §5 above)
    - Remove queue polling, add sync unlock flow
23. Simplify `locket/templates/admin.html`:
    - Remove queue panel
    - Remove worker status column from accounts table
24. Test: full flow (login → check user → unlock → see result)

### Phase 5: Vercel Deployment
25. Create `api/index.py` (Flask entrypoint: `from locket import create_app; app = create_app()`)
26. Create `vercel.json` (maxDuration: 60)
27. Update `.env.example` with new env vars
28. Create `public/` dir for static assets (if any)
29. Deploy to Vercel:
    ```bash
    vercel --prod
    ```
30. Set env vars in Vercel dashboard (all secrets)
31. Test live deployment

### Phase 6: Mobileconfig Storage Migration
32. Decide: Supabase Storage bucket vs. `site_settings` JSONB key
    - Recommend: `site_settings` key `mobileconfig_file: {data: base64, size, signed, updated_at}` (simpler, <5MB limit fine for a `.mobileconfig` plist)
33. Update `admin/routes.py` `/api/mobileconfig` routes:
    - GET: read from `site_settings`, decode base64, return as file response
    - POST: base64-encode uploaded file, store in `site_settings`
    - DELETE: remove key
34. Update `public/routes.py` `/api/mobileconfig`:
    - Read from `site_settings`, decode, return
35. Test: upload/download/delete profile

---

## Testing Checklist

**Backend (local via `vercel dev`):**
- [ ] Supabase client connects, queries work
- [ ] Account CRUD (add/remove/list)
- [ ] Token CRUD
- [ ] Proxy pool (add/mark_ok/mark_err/round-robin)
- [ ] Site settings (popup/maintenance/theme/layout get/set)
- [ ] `unlock_user(username, slot_id)` → full flow with retries, Telegram notification, recent_log insert
- [ ] Auth: login/logout, session persistence

**Frontend (local):**
- [ ] Login gate blocks unauthenticated access
- [ ] Main UI: username input, check user (profile preview), unlock (progress → success/error)
- [ ] Recent activity list loads, auto-refreshes
- [ ] Mobileconfig download works
- [ ] Responsive layout (desktop + mobile)
- [ ] Admin panel: all CRUD works, no queue panel

**Vercel deployment:**
- [ ] Cold start: first unlock slower (login), subsequent unlocks fast (warm container)
- [ ] Env vars loaded correctly
- [ ] HTTPS session cookies work
- [ ] No filesystem errors (all data in Supabase)
- [ ] `maxDuration: 60` allows long unlock requests (test with slow network)

**Edge cases:**
- [ ] Empty accounts table → graceful 503 error on unlock
- [ ] Empty tokens table + no gist URL → clear error message
- [ ] 401 from Locket API → token refresh, retry
- [ ] 5xx from Locket API → backoff, up to 8 attempts
- [ ] Unknown username → "User not found" error
- [ ] RevenueCat returns non-Gold entitlement → "Restore purchase failed" error
- [ ] Concurrent unlocks (open two tabs, submit both) → both succeed independently (no shared state corruption)

---

## Rollback Plan

If Vercel deployment fails or Supabase has issues:
1. Keep VPS deployment running until Vercel is fully tested
2. Database migration is one-way (SQLite → Supabase) — but can export Supabase to SQL dump and reimport to SQLite if needed
3. Git branch strategy: keep `main` as VPS version, work in `vercel-migration` branch, merge only after full QA

---

## Open Questions / Decisions Needed

1. **Mobileconfig storage**: Supabase Storage bucket (more "proper") vs. `site_settings` JSONB key (simpler, no extra API surface) — **Recommendation: JSONB key** unless file is >5MB (unlikely for a plist).

2. **Theme/layout switching**: Current UI has 4 themes + 3 layouts — keep all for future flexibility, or simplify to gold theme only? — **Recommendation: keep backend (site_settings), remove from new `index.html` (operator doesn't need it during livestream), can add back later if needed**.

3. **Cold start latency**: First unlock after cold start ~1-2s slower (Firebase login) — acceptable or need pre-warming strategy (scheduled ping)? — **Recommendation: acceptable, show "Logging in..." in UI, Vercel's warm window is ~5min so most real-world unlocks will be warm**.

4. **Proxy pool necessity**: Current code uses a proxy pool for Locket API calls — still needed or can simplify to direct calls? — **Recommendation: keep it** — if Locket starts rate-limiting, proxies are load-bearing; minimal code complexity to maintain.

5. **Recent activity count**: Current `RECENT_LOG_MAX=100` (DB limit) but frontend shows last 30 — keep both limits or unify? — **Recommendation: keep 100 in DB (historical), show 30 in UI (pagination not needed for livestream use)**.

---

## Summary

**Scope:** Full migration to Vercel serverless + Supabase Postgres, remove queue system (unnecessary for single-operator), redesign UI for livestream demo, add site-wide password gate.

**Complexity:** Medium-high — touches all modules, schema migration, UI rewrite. But architecture is simpler after (no threads, no queue, no SQLite file) — better fit for serverless.

**Timeline estimate:** ~2-3 days for one developer (1 day backend, 1 day UI, 0.5 day deployment/testing/polish).

**Risk mitigation:** Keep VPS running until Vercel fully tested; work in feature branch; test locally via `vercel dev` before deploying.

---

**Ready to proceed?** If approved, I'll start with Phase 1 (backend infrastructure — Supabase setup + db.py rewrite).