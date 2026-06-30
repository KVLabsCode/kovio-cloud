# Live Custom-Display Metrics (OEM "Hawkeye" for custom campaigns)

**Goal:** Let an OEM click into one of their custom displays and see the real
interaction metrics it is picking up — reach, attention/looked, dwell, crowd,
phones-out, and the interaction breakdown — attributed to *that* display, on the
robots actually showing it, live. Replace the synthetic Hawkeye (seeded RNG +
`Math.random` feed) with real, attributed data.

**The core problem:** Paid campaigns get attributed because the robot emits an
`ad_played` event carrying `campaign_id`, and the spend processor correlates the
concurrent `scene_observed` / `interaction_observed` events onto it
(`spend_processor.py:_scene_for_event` / `_interactions_for_event`). Custom
displays emit **no such marker** — the `/display/v1/{code}` player is pure
content delivery — so the perception events that flow while a custom display is
on screen are orphaned. There is nothing to attribute them to.

## Architecture decision (settled)

Attribution is a **join**, not a new data stream. Every `events_raw` row already
carries `robot_id`, `fleet_id`, and `timestamp` (`sdk.py` ingest). The only
missing fact is *"which custom display was robot R showing at time T."* We store
exactly that as a small assignment-history table and compute the live view as an
**on-the-fly join over `events_raw`** — no robot-side change, no spend processor,
no cost.

- **Granularity:** robot-level. A fleet-wide assignment is just one row per robot.
- **v1 is pure-cloud.** No `kovio-py` change. (A robot-emitted `display_played`
  marker is a deferred precision upgrade, only needed if a robot ever mixes paid
  and custom creatives on the same screen within one attribution window.)
- **Insight-only.** Custom displays never touch budgets, balances, the ledger,
  the `impressions` table, or Stripe. The money path is untouched.
- **Live latency** is governed by the SDK's event flush cadence (~30s default),
  not by the binding. Tightening that is a separate, optional lever.

### End-to-end shape

```
robot screen → /display/<code>            (existing player, unchanged)
robot agent  → scene_observed /            (existing, already streamed with
                interaction_observed         robot_id + timestamp)
                       │
OEM assigns display D → robots [R1..Rn]    (NEW: display_assignments rows)
                       │
live view = events_raw  ⋈  display_assignments
            (by robot_id, timestamp ∈ [effective_from, effective_to))
            → aggregate the 007 perception fields per display
```

## Global constraints

- **No money, ever.** Nothing in this feature reads or writes balances,
  `impressions`, `transactions`, or campaign budgets. Keep the insight path and
  the billing path strictly separate.
- **Migrations are forward-only + `IF NOT EXISTS`** (see `migrations/README.md`).
  The ORM in `models.py` mirrors each migration.
- **No em-dashes in user-facing copy** (repo convention; use commas / `·`).
- **kovio-web is a non-standard Next.js** — read `node_modules/next/dist/docs/`
  before writing web code (see `kovio-web/AGENTS.md`). Test env is `node`: only
  pure helpers get unit tests; components are verified via a preview route.
- **Local verification is `py_compile` + pure-logic pytest** — kovio-cloud's full
  runtime/SQLAlchemy is not installed on the robot host; correctness of SQL is
  reviewed against the migration, which is the source of truth.

---

### Task 1: Schema — `display_assignments` (DONE)

**Files:**
- Create: `migrations/008_display_assignments.sql` ✓
- Modify: `src/kovio_cloud/models.py` — add `DisplayAssignment` ORM ✓

The table is a robot↔display history with a half-open `[effective_from,
effective_to)` interval (NULL `effective_to` = still active), plus:
- `ix_display_assignments_robot (robot_id, effective_from DESC)` — the attribution join.
- `ix_display_assignments_display (display_id, effective_from DESC)` — per-display rollup.
- `ux_display_assignments_open_robot` — **partial unique** on `robot_id WHERE
  effective_to IS NULL`, so a robot has at most one open assignment (never
  ambiguously showing two displays).

- [x] Migration written, matches forward-only / `IF NOT EXISTS` style.
- [x] ORM `DisplayAssignment` added, mirrors the DDL, `py_compile` clean.
- [ ] Apply to prod Supabase (`acughqaekwknfowlntcl`) in order, after review.

---

### Task 2: Assignment management API (OEM)

**Files:**
- Modify: `src/kovio_cloud/routes/oem.py`
- Modify: `src/kovio_cloud/schemas.py` (or wherever OEM request/response models live)

**Reassignment protocol (the critical correctness bit):** assigning display D to
robot R must, in ONE transaction:
1. Close any open assignment for R: `UPDATE display_assignments SET effective_to
   = now() WHERE robot_id = R AND effective_to IS NULL`.
2. Insert the new open row `(D, R, effective_from = now(), effective_to = NULL)`.

This keeps the partial-unique invariant and makes the boundary unambiguous: an
event at the switch instant attributes to exactly one display.

**Endpoints** (all `require_oem_auth`, scoped so the OEM owns both the display
and the robots/fleet):
- `POST /oem/v1/displays/{code}/assign` — body `{ robot_ids?: [...], fleet_id?:
  uuid }`. `fleet_id` fans out to every robot in that fleet (one row each).
  Returns the now-active assignment count.
- `POST /oem/v1/displays/{code}/unassign` — body `{ robot_ids?, fleet_id? }`.
  Closes the open assignments (`effective_to = now()`). Omit body to unassign all.
- `GET /oem/v1/displays/{code}/assignments` — current open assignments (robot +
  since when), for the dashboard.

- [ ] Add request/response schemas.
- [ ] Implement assign/unassign with the close-then-open transaction.
- [ ] Authz: 404 if the display's `org_id` != caller org; reject robots not in
      the caller's fleets.
- [ ] Pure-logic test for the fan-out + close-then-open (a fake session or a
      thin SQL-level test).

---

### Task 3: Attribution + live aggregation (the join)

**Files:**
- Create: `src/kovio_cloud/display_insights.py`
- Modify: `src/kovio_cloud/audience.py` — generalize `audience_summary`.

**3a. Generalize `audience_summary`.** It is currently bound to `Impression`.
Add an optional `model` param defaulting to `Impression` so existing callers are
untouched, then call it with a lightweight row source for display events. Because
the perception fields live in `events_raw.payload` (JSONB), not columns, the
display path needs its own aggregation rather than reusing the Impression
columns directly — see 3b. Keep `audience_summary` for the Impression path;
write a parallel `display_audience_summary` that reads the JSONB.

**3b. The attribution query** (`display_insights.py`). For a display `code` and a
window `[start, end]`:

```sql
-- robots currently/previously assigned to this display, with their intervals
WITH spans AS (
  SELECT a.robot_id, a.effective_from, COALESCE(a.effective_to, 'infinity') AS effective_to
  FROM display_assignments a
  JOIN custom_displays d ON d.id = a.display_id
  WHERE d.code = :code
),
attributed AS (
  SELECT e.event_type, e.payload, e.timestamp
  FROM events_raw e
  JOIN spans s
    ON e.robot_id = s.robot_id
   AND e.timestamp >= s.effective_from
   AND e.timestamp <  s.effective_to
  WHERE e.event_type IN ('scene_observed', 'interaction_observed')
    AND e.timestamp >= :start AND e.timestamp < :end
)
SELECT ... -- aggregate below
```

Aggregate from `attributed`, mirroring `audience.py` but reading JSONB keys:
- reach/attention: `avg/max (payload->>'person_count')`, `avg
  (payload->>'attended_count')`.
- looked / dwell: `sum (payload->>'looked_count')`, `avg
  (payload->>'mean_dwell_s')` (0.0 sentinel = no data, same contract as today).
- crowd: `avg/max (payload->>'people_nearby')`, nearest distance.
- funnel: total reach → looked; `interaction_observed` rows grouped by
  `payload->>'kind'` → `phones_out` (kind = `phone_out`), total interactions,
  and the `{kind: count}` breakdown.

Return the **same AudienceSummary-shaped dict** the web already consumes
(`lib/types.ts`), so the frontend swap is minimal.

**3c. Live feed.** `recent_events(code, limit=20)` — the newest `attributed`
rows mapped to the feed shape the UI wants (`view` / `interaction`), with real
timestamps. This is the genuine replacement for `HawkeyeFeed`'s `Math.random`.

- [ ] `display_audience_summary(session, code, start, end)`.
- [ ] `recent_events(session, code, limit)`.
- [ ] Pure-logic tests for the JSONB aggregation math (feed it sample payload
      dicts; assert the funnel + breakdown).

---

### Task 4: Live + summary endpoints

**Files:** `src/kovio_cloud/routes/oem.py`

- `GET /oem/v1/displays/{code}` — display meta + `assignments` (Task 2) +
  lifetime/30-day `summary` (Task 3b). Powers the detail page on load.
- `GET /oem/v1/displays/{code}/live?window_minutes=5` — `summary` over the recent
  window + `events` (Task 3c). The web polls this every few seconds (no
  WebSocket infra; polling is the v1 transport, matches the codebase).

- [ ] Both endpoints, OEM-scoped (display `org_id` == caller).
- [ ] `window_minutes` clamped (e.g. 1..60).
- [ ] `Cache-Control: no-store` on `/live`.

---

### Task 5: kovio-web — OEM custom-display detail goes real

**Files:**
- Modify: `app/oem/campaigns/[id]/page.tsx` (the OEM custom-display detail)
- Modify/replace: `components/HawkeyeFeed.tsx` consumer, `lib/hawkeye.ts` usage
- Modify: `lib/api-client.ts` / `lib/api.ts` — add `oemDisplay(code)` and
  `oemDisplayLive(code)` fetchers.

- Real totals + funnel + dwell/crowd from `GET /oem/v1/displays/{code}`.
- A client component polls `/oem/v1/displays/{code}/live` (~3s) and renders the
  real event feed + live counters, replacing the synthetic generator.
- Keep `lib/hawkeye.ts`'s *layout* (hours/fleet split) but either (a) drive it
  from real per-robot assignment data, or (b) clearly label any still-synthetic
  panel until a backend source exists. No silently-fake "live" numbers.
- An **assignment UI**: pick robots/fleet to show this display on (calls Task 2).

- [ ] API fetchers.
- [ ] Detail page reads real summary.
- [ ] Live polling component (real feed).
- [ ] Assignment control.
- [ ] `npx tsc --noEmit` clean; preview-route screenshot at 1280/390.

---

## Rollout order

1. Apply migration 008 to prod Supabase (additive, safe — no existing code reads it).
2. Ship kovio-cloud (Tasks 2–4) — deploy `kovio-api` (Fly app, remote builder).
3. Ship kovio-web (Task 5).
4. Assign a real display to a robot, confirm metrics attribute end-to-end.

## Deferred (not v1)

- **`display_played` robot marker** — exact on-screen timing for robots that mix
  paid + custom on one screen. Add to `kovio-py` `agent.py` mirroring `ad_played`
  only if the assignment-window attribution proves too coarse in the field.
- **Materialized rollup** — if on-the-fly `events_raw` aggregation gets slow at
  scale, add a `display_impressions` rollup table populated by an
  `insight_processor` (mirrors the spend processor, cost-free).
- **Bare-tablet heartbeat** — a `/display/v1/{code}/heartbeat` presence signal
  for screens with no robot attached (no perception, so presence only).

## Plan self-review

- **Attribution:** robot+time join over existing `events_raw` (Task 3) ✓; binding
  stored as assignment history (Task 1) ✓; reassignment is unambiguous via
  close-then-open + partial-unique invariant (Task 1/2) ✓.
- **No money leak:** insight path never touches impressions/ledger/budgets
  (Global constraints; Tasks 2–4 read events_raw + assignments only) ✓.
- **Real, not synthetic:** live feed + summary from real events (Task 3/4/5);
  remaining synthetic panels must be labeled (Task 5) ✓.
- **No robot change in v1:** pure-cloud; marker explicitly deferred ✓.
- **Reuses existing infra:** fleet-scoped SDK, events_raw indexes
  (`ix_events_raw_robot_ts`), AudienceSummary web contract ✓.
