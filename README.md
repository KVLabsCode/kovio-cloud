# kovio-cloud

The control plane for **Kovio**, the open ad platform for autonomous robots with
screens. A FastAPI service mediates all access to a Postgres database (Supabase
in prod, docker-compose in dev) and runs a background **spend processor** that
turns raw robot events into costed impressions and moves money in real time.

## The money loop

1. Robots play ads and `POST /sdk/v1/events/batch`. Events land in `events_raw`
   with `processed_at = NULL`.
2. Every 60s the spend processor finds unprocessed `ad_played` events, creates an
   `impressions` row with the campaign's cost split (e.g. $0.10 → $0.06 OEM +
   $0.04 Kovio), debits the advertiser's `balance_cents`, credits the OEM's
   `pending_payout_cents`, writes a `transactions` ledger pair, and marks the
   event processed.
3. When a campaign crosses `budget_total_cents` (or the advertiser runs dry), the
   processor sets `status = 'paused'`. Within the 5-minute SDK TTL, robots stop
   pulling it.

No Stripe yet — the columns and ledger are wired so it's fill-in-the-blanks later.

**Privacy:** camera frames never reach the cloud. Robots derive `person_count` /
`attended_count` locally; only those numbers travel in event payloads. There is
no image column anywhere, by design.

## API surface

- `/sdk/v1/*` — robots (bearer, `sdk` scope): `GET campaigns`, `POST events/batch`, `POST heartbeat`
- `/admin/v1/*` — Kovio team (bearer, `admin` scope): CRUD + `GET stats/summary` + `POST process-spend`
- `/advertiser/v1/*`, `/oem/v1/*` — namespace reserved (501 stubs for now)
- `/healthz` — liveness + real DB connectivity check

## Local dev

```bash
docker compose up -d db
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export KOVIO_DATABASE_URL=postgresql+asyncpg://kovio:kovio@localhost:5432/kovio
export KOVIO_DEV_AUTO_CREATE_TABLES=true
kovio-cloud bootstrap     # prints an admin key + an SDK key — save them
kovio-cloud serve
```

Or run the whole stack in containers: `docker compose up --build`, then
`docker compose exec api kovio-cloud bootstrap`.

## Production (Fly.io + Supabase)

The schema already lives in Supabase (migration `001_initial_schema_with_money`);
do **not** auto-create tables in prod. Deploy:

```bash
fly secrets set KOVIO_DATABASE_URL="$(grep KOVIO_DATABASE_URL .env.production | cut -d= -f2-)"
fly secrets set KOVIO_KEY_PEPPER="$(grep KOVIO_KEY_PEPPER .env.production | cut -d= -f2-)"
fly deploy
```

## Configuration

Every setting is a `KOVIO_*` env var — see [`.env.example`](.env.example).

## Deliberately not here yet

Stripe code, advertiser/OEM web apps, Alembic migrations, Redis/Kafka/ClickHouse,
`events_raw` partitioning. All scheduled for later milestones.
