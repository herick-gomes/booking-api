from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from redis import ConnectionError
from rq import Queue, SimpleWorker
from sqlalchemy import func, select

from app.auth import redis_connection
from app.db import SessionLocal
from app.dispatcher import dispatch_once
from app.domain import now
from app.jobs import deliver
from app.models import Booking, Notification, Outbox
from tests.conftest import book


def join(client, setup, who=2):
    return client.post(
        "/waitlist",
        headers=setup["headers"][who],
        json={"service_id": 1, "starts_at": setup["start"].isoformat(), "accept_automatic_booking": True},
    )


def test_waitlist_requires_occupied_slot_and_explicit_consent(client, setup):
    assert join(client, setup).status_code == 409
    book(client, setup)
    assert (
        client.post(
            "/waitlist",
            headers=setup["headers"][2],
            json={
                "service_id": 1,
                "starts_at": setup["start"].isoformat(),
                "accept_automatic_booking": False,
            },
        ).status_code
        == 422
    )
    assert join(client, setup).status_code == 201
    assert join(client, setup).status_code == 409
    assert client.get("/waitlist", headers=setup["headers"][3]).json() == []


def test_cancellation_promotes_fifo_and_preserves_agreed_price(client, setup):
    original = book(client, setup).json()
    first, second = join(client, setup).json(), join(client, setup, 3).json()
    assert client.patch("/services/1", headers=setup["headers"][0], json={"price": "200"}).status_code == 200
    assert client.post(f"/bookings/{original['id']}/cancel", headers=setup["headers"][1]).status_code == 200
    matched = client.get("/waitlist", headers=setup["headers"][2]).json()[0]
    assert matched["id"] == first["id"] and matched["status"] == "matched"
    waiting = client.get("/waitlist", headers=setup["headers"][3]).json()[0]
    assert waiting["id"] == second["id"] and waiting["status"] == "waiting"
    booking = client.get(f"/bookings/{matched['booking_id']}", headers=setup["headers"][2]).json()
    assert booking["price"] == "100.00"
    assert client.delete(f"/waitlist/{matched['id']}", headers=setup["headers"][2]).status_code == 409
    with SessionLocal() as db:
        event = db.scalar(select(Outbox).where(Outbox.kind == "waitlist_matched"))
        event_id = str(event.id)
    assert deliver(event_id) == "delivered"
    assert len(client.get("/notifications", headers=setup["headers"][2]).json()) == 1
    assert client.get("/notifications", headers=setup["headers"][3]).json() == []


def test_conflicted_candidate_is_skipped_without_losing_cancellation(client, setup):
    original = book(client, setup).json()
    join(client, setup)
    join(client, setup, 3)
    assert book(client, setup, who=2, service=1).status_code == 201
    assert client.post(f"/bookings/{original['id']}/cancel", headers=setup["headers"][1]).status_code == 200
    assert client.get("/waitlist", headers=setup["headers"][2]).json()[0]["status"] == "waiting"
    assert client.get("/waitlist", headers=setup["headers"][3]).json()[0]["status"] == "matched"


def test_leaving_waitlist_and_reschedule_promotion(client, setup):
    original = book(client, setup).json()
    entry = join(client, setup).json()
    join(client, setup, 3)
    assert client.delete(f"/waitlist/{entry['id']}", headers=setup["headers"][3]).status_code == 404
    assert client.delete(f"/waitlist/{entry['id']}", headers=setup["headers"][2]).status_code == 204
    response = client.post(
        f"/bookings/{original['id']}/reschedule",
        headers=setup["headers"][1],
        json={"starts_at": (setup["start"] + timedelta(hours=2)).isoformat()},
    )
    assert response.status_code == 200
    assert client.get("/waitlist", headers=setup["headers"][3]).json()[0]["status"] == "matched"


def due_event(client, setup):
    booking = book(client, setup).json()
    with SessionLocal.begin() as db:
        event = db.scalar(select(Outbox).where(Outbox.booking_id == UUID(booking["id"])))
        event.due_at = now() - timedelta(seconds=1)
        return booking, str(event.id)


def test_consumer_is_idempotent(client, setup):
    _, event_id = due_event(client, setup)
    assert deliver(event_id) == "delivered"
    assert deliver(event_id) == "delivered"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 1


@pytest.mark.parametrize("action", ["cancel", "reschedule"])
def test_old_reminder_cannot_be_delivered_after_change(client, setup, action):
    booking, event_id = due_event(client, setup)
    data = (
        {"starts_at": (setup["start"] + timedelta(hours=2)).isoformat()} if action == "reschedule" else None
    )
    assert (
        client.post(f"/bookings/{booking['id']}/{action}", headers=setup["headers"][1], json=data).status_code
        == 200
    )
    assert deliver(event_id) == "skipped"
    assert client.get("/notifications", headers=setup["headers"][1]).json() == []


def test_not_due_missing_and_expired_reminders(client, setup):
    assert deliver(str(uuid4())) == "missing"
    booking = book(client, setup).json()
    with SessionLocal() as db:
        event_id = str(db.scalar(select(Outbox.id)))
    assert deliver(event_id) == "not_due"
    with SessionLocal.begin() as db:
        item = db.get(Booking, UUID(booking["id"]))
        item.starts_at, item.ends_at = now() - timedelta(hours=2), now() - timedelta(hours=1)
        db.get(Outbox, UUID(event_id)).due_at = now() - timedelta(hours=3)
    assert deliver(event_id) == "skipped"


def test_real_redis_dispatch_and_worker(client, setup):
    _, event_id = due_event(client, setup)
    assert dispatch_once() == 1
    assert dispatch_once() == 0
    connection = redis_connection()
    queue = Queue("booking", connection=connection)
    job = queue.fetch_job(queue.job_ids[0])
    assert job.retries_left == 3
    worker = SimpleWorker([queue], connection=connection)
    worker.work(burst=True, logging_level="WARNING")
    assert job.get_status().value == "finished"
    with SessionLocal() as db:
        assert db.get(Outbox, UUID(event_id)).status == "delivered"
        assert db.scalar(select(func.count()).select_from(Notification)) == 1


def test_queue_outage_preserves_pending_events(client, setup, monkeypatch):
    _, event_id = due_event(client, setup)
    monkeypatch.setattr("app.dispatcher.redis_connection", lambda: (_ for _ in ()).throw(ConnectionError()))
    with pytest.raises(ConnectionError):
        dispatch_once()
    with SessionLocal() as db:
        event = db.get(Outbox, UUID(event_id))
        assert event.status == "pending" and event.lease_until is None


def test_expired_dispatch_lease_is_recovered(client, setup):
    _, event_id = due_event(client, setup)
    assert dispatch_once() == 1
    with SessionLocal.begin() as db:
        db.get(Outbox, UUID(event_id)).lease_until = now() - timedelta(seconds=1)
    assert dispatch_once() == 1
    connection = redis_connection()
    worker = SimpleWorker([Queue("booking", connection=connection)], connection=connection)
    worker.work(burst=True, logging_level="WARNING")
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 1
        assert db.get(Outbox, UUID(event_id)).dispatch_count == 2


def test_failed_delivery_rolls_back_then_recovers(client, setup):
    from sqlalchemy import event

    _, event_id = due_event(client, setup)

    def fail_notification_flush(session, context, instances):
        if any(isinstance(item, Notification) for item in session.new):
            raise RuntimeError("Simulated persistence failure")

    event.listen(SessionLocal.class_, "before_flush", fail_notification_flush)
    try:
        with pytest.raises(RuntimeError, match="Simulated"):
            deliver(event_id)
    finally:
        event.remove(SessionLocal.class_, "before_flush", fail_notification_flush)
    with SessionLocal() as db:
        assert db.get(Outbox, UUID(event_id)).status == "pending"
        assert db.scalar(select(func.count()).select_from(Notification)) == 0
    assert deliver(event_id) == "delivered"
