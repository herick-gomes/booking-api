# Validation record

Initial local validation on 2026-11-15:

- Python 3.12, PostgreSQL 17 and Redis 8.
- 49 pytest cases passed; statement coverage 97.96% (displayed as 98%).
- Migration upgrade/downgrade round trip and Alembic schema-drift check passed.
- Ruff lint and formatting checks passed.
- Live Uvicorn HTTP demo passed: account creation, catalogue setup, booking, conflict response, waitlist and cancellation/promotion.
- Real Redis/RQ in-process worker test passed, including dispatcher leases and duplicate delivery recovery.

The [GitHub Actions workflow](https://github.com/herick-gomes/booking-api/actions/workflows/ci.yml) is the source of truth for remote validation. It additionally builds the Docker image and runs the API, PostgreSQL, Redis, worker and dispatcher as separate containers. Check the current run rather than treating this document as an evergreen guarantee.

## High-value scenarios

| Area | Evidence |
| --- | --- |
| Concurrency | Two HTTP requests race for one slot; only one succeeds |
| Database enforcement | Parallel inserts without application locks cannot overlap for a professional or a customer |
| Boundaries | Back-to-back appointments are allowed; overlapping ones roll back |
| Isolation | Another user cannot read or modify private bookings, calendars, waitlists or notifications |
| Rescheduling | Conflict leaves the original booking untouched; successful change preserves commercial terms |
| Waitlist | FIFO promotion, opt-in, original price, withdrawal and skipping a now-busy customer |
| Delivery | Real queue/worker, idempotent repeated and concurrent delivery, skipped stale events |
| Failure recovery | Consumer persistence failure rolls back; Redis outage leaves intent intact; expired leases redispatch |
| Timezones | Nonexistent spring-DST times excluded, repeated fall hour remains distinct, offset-crossing rejected |
| Authentication | Revocation, invalid tokens, password whitespace, throttling and Redis failure |

The suite uses a real disposable PostgreSQL database. It does not substitute SQLite or claim browser/UI validation. Remaining coverage gaps include the dispatcher's perpetual loop and some defensive not-found branches. The HTTP demo and container smoke checks cover startup and wiring separately from pytest.
