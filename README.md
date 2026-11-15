# Booking API

[![Backend checks](https://github.com/herick-gomes/booking-api/actions/workflows/ci.yml/badge.svg)](https://github.com/herick-gomes/booking-api/actions/workflows/ci.yml)

An appointment backend for independent professionals: personal trainers, tutors and small studios. Built with **FastAPI, PostgreSQL, SQLAlchemy, Redis and RQ**.

An appointment looks simple until two people choose the same time. Then someone cancels, a customer on the waitlist has already booked elsewhere, and an old reminder is still in the queue. This project focuses on those situations.

It is a portfolio project with fictional data, not a service operating for real businesses. There is no deployed public instance yet.

## What it does

- Businesses own professionals, services, weekly working hours and time-off blocks.
- Customers discover availability, book, reschedule and cancel appointments.
- PostgreSQL rejects overlapping bookings for both the professional **and the customer**, even when requests arrive concurrently.
- An opt-in, exact-slot waitlist fills vacancies in FIFO order, skipping customers who now have a conflicting appointment.
- Prices, names and durations are preserved when booking or joining the waitlist.
- A transactional outbox feeds background reminders to a private, database-backed inbox. No real email, SMS or WhatsApp messages are sent.
- JWT sessions can be revoked immediately. Owner operations, customer bookings and notifications are scoped to the authenticated user.
- API documentation is generated at `/docs` and `/redoc`.

## Try it locally

Requirements: Git and Docker Desktop with Docker Compose. On Windows, open Docker Desktop first.

```bash
git clone https://github.com/herick-gomes/booking-api.git
cd booking-api
docker compose up --build -d
```

Wait for `http://localhost:8000/ready` to return `{"status":"ready"}`, then open [Swagger UI](http://localhost:8000/docs).

Run a complete example without copying tokens manually:

```bash
docker compose exec api python scripts/demo.py
docker compose exec api python -m scripts.check_delivery
```

The first command creates three temporary demo accounts, a professional and a service. It books a slot, confirms that a second booking is rejected, joins the waitlist, cancels the first appointment and verifies automatic promotion. The second waits for the dispatcher and worker to deliver the confirmation. Each run adds fictional records; it does not overwrite existing data. The auth limit is shared by these requests, so wait a minute before repeating the demo.

```bash
docker compose logs -f api worker dispatcher
docker compose down
```

`down` preserves local records in Docker volumes. The example Compose configuration uses local-only credentials and exposes only the API, bound to `127.0.0.1`. It is not a production deployment configuration.

## A few deliberate choices

| Problem | Decision | Tradeoff |
| --- | --- | --- |
| Two bookings claim the same time | PostgreSQL GiST exclusion constraints plus a lock on the professional's calendar | PostgreSQL is required; SQLite cannot stand in for integration tests |
| A customer books two professionals at once | A second exclusion constraint covers the customer | A valid-looking slot can still return `409` for this customer |
| Redis fails during booking | Write a reminder intent in the booking's database transaction | Delivery is asynchronous and may be delayed |
| A worker retries the same reminder | Lock the booking and event; persist one notification per event | Idempotency covers the internal inbox, not external delivery providers |
| A service price changes | Store agreed terms on bookings and waitlist entries | Historical records deliberately keep the old price |
| A business changes working hours | Reject replacement while future confirmed bookings exist | Conservative, but avoids silently invalidating appointments |

Read [the architecture and transaction boundaries](docs/architecture.md), [the validation record](docs/validation.md), and [the Portuguese local setup guide](docs/local-setup-pt-BR.md).

## HTTP workflow

1. `POST /auth/register`, then `POST /auth/login`. Login returns a bearer token.
2. The owner creates `/businesses`, `/businesses/{id}/professionals` and `/professionals/{id}/services`.
3. Set weekly hours with `PUT /professionals/{id}/hours`. Monday is `0`; times use minutes after local midnight. `480` means 08:00.
4. Query `GET /services/{id}/availability?first_day=2026-12-01&days=1` using a date within the next 90 days.
5. The customer sends a timezone-aware timestamp to `POST /bookings`.
6. Manage `/bookings/{id}/reschedule`, `/bookings/{id}/cancel`, `/waitlist` and `/notifications`.

Use **Authorize** in Swagger UI and paste the token. Login accepts JSON, not an OAuth password form. Catalogues are public; calendars with customer data are owner-only. Collection endpoints use `offset` and `limit` (maximum 100).

A booking payload:

```json
{
  "service_id": 1,
  "starts_at": "2026-12-01T10:00:00-03:00"
}
```

Replace both values with an actual service and future available time. All prices use BRL decimals. Expected conflicts return `409`, invalid input `422`, missing authentication `401`, and inaccessible private records `404`.

## Development and tests

Python 3.12 is the CI target. Install the pinned dependencies in `requirements.txt`, configure `.env` from `.env.example`, then run:

```bash
alembic upgrade head
uvicorn app.main:app --reload --no-proxy-headers
```

Start Redis and PostgreSQL separately for this workflow. The database user running migrations needs permission to install `btree_gist`. A managed production setup should use a separate migration role.

Tests use a **dedicated disposable database named `booking_test`** and a dedicated Redis database. They truncate application tables and flush that Redis database. Never point them at real data.

```bash
# Linux/macOS; see docs/local-setup-pt-BR.md for PowerShell.
export ENVIRONMENT=test
export DATABASE_URL=postgresql+psycopg://booking:local-only@localhost:5432/booking_test
export REDIS_URL=redis://localhost:6379/15
alembic upgrade head
pytest --cov --cov-fail-under=90
ruff check .
ruff format --check .
```

The CI also checks migration upgrade/downgrade, schema drift, a Docker Compose HTTP flow, and delivery through the separate worker and dispatcher. Initial local validation: **49 tests passed, 97.96% statement coverage**. Coverage is not a claim that every concurrency or production failure has been proven safe.

## Scope and next steps

This version deliberately has one owner per business, fixed-duration services and one professional per appointment. It has no payments, recurring appointments, email verification, account recovery, customer UI or real notification provider.

Before public deployment: configure a secret signing key, HTTPS, CORS, a trusted proxy/rate-limit strategy, managed PostgreSQL/Redis, backups and monitoring. `ENVIRONMENT=production` rejects the sample JWT key. The current rate limiter uses the direct peer address and fails closed when Redis is unavailable; behind a proxy, configure trusted client-IP handling before opening registration to the public.

The next useful product step is a small React booking interface consuming this API, followed by a hosted demonstration. See [the roadmap](docs/roadmap.md).
