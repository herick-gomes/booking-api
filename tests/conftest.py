import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

# Refuse to truncate a database unless the caller explicitly opted into testing.
if os.environ.get("ENVIRONMENT") != "test" or not os.environ.get("DATABASE_URL", "").endswith(
    "/booking_test"
):
    raise RuntimeError("Use ENVIRONMENT=test and a dedicated DATABASE_URL ending in /booking_test.")

if not os.environ.get("REDIS_URL", "").endswith("/15"):
    raise RuntimeError("Use a dedicated test Redis database /15; tests flush it.")

from app.auth import issue_token, passwords, redis_connection
from app.db import SessionLocal, engine
from app.main import app
from app.models import Business, Hours, Professional, Service, User

PASSWORD = "example-password-2026"
HASH = passwords.hash(PASSWORD)


@pytest.fixture(autouse=True)
def clean():
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE users, businesses, professionals, services, hours, blocks, bookings, waitlist, booking_events, outbox, notifications, auth_sessions RESTART IDENTITY CASCADE"
            )
        )
    redis_connection().flushdb()


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def setup():
    with SessionLocal.begin() as db:
        users = [
            User(username=name, password_hash=HASH) for name in ["owner", "customer", "second", "outsider"]
        ]
        db.add_all(users)
        db.flush()
        tokens = [issue_token(db, user)["access_token"] for user in users]
        business = Business(owner_id=users[0].id, name="Studio Demo", timezone="America/Sao_Paulo")
        db.add(business)
        db.flush()
        pros = [Professional(business_id=business.id, name=name) for name in ["Ana", "Bruno"]]
        db.add_all(pros)
        db.flush()
        services = [
            Service(professional_id=pro.id, name="Consultation", duration_minutes=60, price="100.00")
            for pro in pros
        ]
        db.add_all(services)
        db.add_all(
            [
                Hours(professional_id=pro.id, weekday=day, start_minute=480, end_minute=1080)
                for pro in pros
                for day in range(7)
            ]
        )
        db.flush()
        start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
            hour=13, minute=0, second=0, microsecond=0
        )
        return {
            "headers": [{"Authorization": f"Bearer {t}"} for t in tokens],
            "users": [u.id for u in users],
            "pros": [p.id for p in pros],
            "services": [s.id for s in services],
            "business": business.id,
            "start": start,
        }


def book(client, setup, who=1, service=0, start=None):
    return client.post(
        "/bookings",
        headers=setup["headers"][who],
        json={"service_id": setup["services"][service], "starts_at": (start or setup["start"]).isoformat()},
    )
