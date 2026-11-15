from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.main import app
from app.models import Booking
from tests.conftest import book


def test_two_requests_compete_for_one_slot(setup):
    barrier = Barrier(2)

    def reserve(who):
        with TestClient(app) as client:
            barrier.wait(timeout=5)
            return book(client, setup, who=who).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(reserve, [1, 2]))
    assert sorted(statuses) == [201, 409]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Booking)) == 1


@pytest.mark.parametrize("same_customer", [False, True])
def test_database_exclusion_without_application_lock(setup, same_customer):
    barrier = Barrier(2)

    def insert(index):
        with SessionLocal() as db:
            booking = Booking(
                customer_id=setup["users"][1 if same_customer else index + 1],
                professional_id=setup["pros"][index if same_customer else 0],
                service_id=setup["services"][index if same_customer else 0],
                service_name="Direct SQL test",
                duration_minutes=60,
                price="100",
                starts_at=setup["start"],
                ends_at=setup["start"] + timedelta(hours=1),
            )
            db.add(booking)
            barrier.wait(timeout=5)
            try:
                db.commit()
                return "created"
            except IntegrityError as exc:
                db.rollback()
                assert exc.orig.sqlstate == "23P01"
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(insert, [0, 1])) == ["conflict", "created"]


def test_concurrent_duplicate_delivery_creates_one_notification(client, setup):
    from app.jobs import deliver
    from app.models import Notification
    from tests.test_waitlist_jobs import due_event

    _, event_id = due_event(client, setup)
    barrier = Barrier(2)

    def run(_):
        barrier.wait(timeout=5)
        return deliver(event_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, range(2))) == ["delivered", "delivered"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 1
