# Roadmap

## Implemented in the first version

- Business-owned professionals, services, weekly schedules and blocks.
- Booking/rescheduling/cancellation with price snapshots and customer/professional overlap constraints.
- Exact-slot opt-in waitlists with automatic promotion.
- Transactional outbox, Redis/RQ delivery, retries and private inbox.
- Revocable JWT sessions, owner/customer isolation, auth throttling.
- PostgreSQL integration/concurrency tests, migration checks, Docker and CI.

## Next useful milestone

A React/TypeScript client with public service discovery, a booking calendar, customer appointments and an owner dashboard. Keep business rules in the API and use its generated OpenAPI schema for client types. Add browser tests that exercise a real API and database.

## Before a public demonstration

- Host API, worker, dispatcher, PostgreSQL and Redis with appropriate lifecycle support.
- Configure HTTPS, trusted proxy/client-IP handling, signing secrets and allowed origins.
- Add an isolated demo dataset, controlled reset process and operational alerts.
- Verify backup restore and document failure recovery.

## Before serving real customers

- Email verification, account recovery, staff permissions and account/data deletion.
- Explicit cancellation policy and audit access controls.
- Notification-provider integration with provider idempotency, delivery status and consent/preferences.
- Outbox retention, dead-letter workflow and queue/latency monitoring.
- Product validation with real professionals before adding payments or recurring bookings.

There is no claim that these future capabilities are already implemented.
