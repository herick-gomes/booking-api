from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Booking, Outbox
from tests.conftest import PASSWORD, book


def test_registration_login_and_revocation(client):
    credentials = {"username": "new_customer", "password": PASSWORD}
    assert client.post("/auth/register", json=credentials).status_code == 201
    assert client.post("/auth/register", json=credentials).status_code == 409
    token = client.post("/auth/login", json=credentials).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/auth/me", headers=headers).json()["username"] == "new_customer"
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_password_whitespace_is_significant(client):
    data = {"username": "spaces", "password": "  twelve-characters  "}
    assert client.post("/auth/register", json=data).status_code == 201
    assert client.post("/auth/login", json=data).status_code == 200
    data["password"] = data["password"].strip()
    assert client.post("/auth/login", json=data).status_code == 401


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer garbage"}])
def test_unauthenticated(client, headers):
    assert client.get("/bookings", headers=headers).status_code == 401


def test_auth_throttles_even_with_spoofed_forwarded_header(client):
    for _ in range(10):
        assert (
            client.post("/auth/login", json={"username": "missing", "password": PASSWORD}).status_code == 401
        )
    response = client.post(
        "/auth/login",
        headers={"X-Forwarded-For": "1.2.3.4"},
        json={"username": "missing", "password": PASSWORD},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"


def test_redis_auth_outage_is_closed(client, monkeypatch):
    from redis import ConnectionError

    monkeypatch.setattr("app.auth.redis_connection", lambda: (_ for _ in ()).throw(ConnectionError()))
    assert client.post("/auth/login", json={"username": "missing", "password": PASSWORD}).status_code == 503


def test_health_and_openapi(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200
    assert "/bookings" in client.get("/openapi.json").json()["paths"]


def test_owner_setup_and_public_catalogue(client, setup):
    owner = setup["headers"][0]
    business = client.post(
        "/businesses", headers=owner, json={"name": "New studio", "timezone": "Europe/Lisbon"}
    ).json()
    pro = client.post(
        f"/businesses/{business['id']}/professionals", headers=owner, json={"name": "Maria"}
    ).json()
    service = client.post(
        f"/professionals/{pro['id']}/services",
        headers=owner,
        json={"name": "Lesson", "duration_minutes": 45, "price": "90.50"},
    )
    assert service.status_code == 201
    assert len(client.get("/businesses").json()) == 2
    assert client.get(f"/businesses/{business['id']}/professionals").json()[0]["name"] == "Maria"
    assert client.get(f"/professionals/{pro['id']}/services").json()[0]["price"] == "90.50"


@pytest.mark.parametrize(
    "path,payload,method",
    [
        ("/businesses/1/professionals", {"name": "Intruder"}, "post"),
        ("/professionals/1/services", {"name": "Hack", "duration_minutes": 30, "price": "10"}, "post"),
        ("/professionals/1/hours", {"windows": []}, "put"),
        ("/services/1", {"price": "1"}, "patch"),
    ],
)
def test_owner_isolation(client, setup, path, payload, method):
    assert getattr(client, method)(path, headers=setup["headers"][3], json=payload).status_code == 404


def test_booking_visibility_and_history(client, setup):
    result = book(client, setup)
    assert result.status_code == 201, result.text
    booking_id = result.json()["id"]
    assert client.get(f"/bookings/{booking_id}", headers=setup["headers"][3]).status_code == 404
    assert client.get(f"/bookings/{booking_id}/history", headers=setup["headers"][3]).status_code == 404
    assert client.get("/bookings", headers=setup["headers"][3]).json() == []
    assert client.get("/professionals/1/bookings", headers=setup["headers"][3]).status_code == 404
    assert len(client.get("/professionals/1/bookings", headers=setup["headers"][0]).json()) == 1
    assert (
        client.get(f"/bookings/{booking_id}/history", headers=setup["headers"][1]).json()[0]["action"]
        == "created"
    )


def test_overlap_customer_and_adjacent_boundaries(client, setup):
    assert book(client, setup).status_code == 201
    assert book(client, setup, who=2).status_code == 409
    assert book(client, setup, service=1).status_code == 409
    assert book(client, setup, start=setup["start"] + timedelta(hours=1)).status_code == 201
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Booking)) == 2
        assert db.scalar(select(func.count()).select_from(Outbox)) == 2


@pytest.mark.parametrize(
    "offset", [timedelta(minutes=7), timedelta(hours=-7), timedelta(days=95), timedelta(days=-5)]
)
def test_invalid_slot(client, setup, offset):
    assert book(client, setup, start=setup["start"] + offset).status_code == 409


def test_naive_time_rejected(client, setup):
    assert (
        client.post(
            "/bookings",
            headers=setup["headers"][1],
            json={"service_id": 1, "starts_at": "2027-01-01T12:00:00"},
        ).status_code
        == 422
    )


def test_reschedule_snapshot_and_atomic_failure(client, setup):
    first = book(client, setup).json()
    other = book(client, setup, who=2, start=setup["start"] + timedelta(hours=2)).json()
    assert (
        client.patch(
            "/services/1", headers=setup["headers"][0], json={"price": "150", "name": "New name"}
        ).status_code
        == 200
    )
    path = f"/bookings/{first['id']}/reschedule"
    assert (
        client.post(path, headers=setup["headers"][1], json={"starts_at": other["starts_at"]}).status_code
        == 409
    )
    unchanged = client.get(f"/bookings/{first['id']}", headers=setup["headers"][1]).json()
    assert unchanged["version"] == 1
    moved = client.post(
        path,
        headers=setup["headers"][1],
        json={"starts_at": (setup["start"] + timedelta(hours=1)).isoformat()},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["price"] == "100.00"
    assert moved.json()["service_name"] == "Consultation"
    assert moved.json()["version"] == 2
    events = client.get(f"/bookings/{first['id']}/history", headers=setup["headers"][1]).json()
    assert [e["action"] for e in events] == ["created", "rescheduled"]


def test_cancel_and_no_second_cancel(client, setup):
    booking_id = book(client, setup).json()["id"]
    path = f"/bookings/{booking_id}/cancel"
    assert client.post(path, headers=setup["headers"][3]).status_code == 404
    assert client.post(path, headers=setup["headers"][0]).json()["status"] == "cancelled"
    assert client.post(path, headers=setup["headers"][1]).status_code == 409
    assert book(client, setup, who=2).status_code == 201


def test_availability_blocks_and_working_hours(client, setup):
    params = {"first_day": setup["start"].date().isoformat()}
    before = client.get("/services/1/availability", params=params).json()
    assert len(before) == 37
    payload = {
        "starts_at": setup["start"].isoformat(),
        "ends_at": (setup["start"] + timedelta(hours=1)).isoformat(),
        "reason": "Lunch",
    }
    response = client.post("/professionals/1/blocks", headers=setup["headers"][0], json=payload)
    assert response.status_code == 201
    assert book(client, setup).status_code == 409
    after = client.get("/services/1/availability", params=params).json()
    assert len(after) == 30
    block_id = response.json()["id"]
    assert client.delete(f"/blocks/{block_id}", headers=setup["headers"][3]).status_code == 404
    assert client.delete(f"/blocks/{block_id}", headers=setup["headers"][0]).status_code == 204
    assert book(client, setup).status_code == 201
    assert (
        client.post("/professionals/1/blocks", headers=setup["headers"][0], json=payload).status_code == 409
    )
    assert (
        client.put("/professionals/1/hours", headers=setup["headers"][0], json={"windows": []}).status_code
        == 409
    )


def test_schedule_validation_and_replacement(client, setup):
    path = "/professionals/1/hours"
    windows = [
        {"weekday": 0, "start_minute": 480, "end_minute": 720},
        {"weekday": 0, "start_minute": 660, "end_minute": 900},
    ]
    assert client.put(path, headers=setup["headers"][0], json={"windows": windows}).status_code == 422
    assert client.put(path, headers=setup["headers"][0], json={"windows": windows[:1]}).status_code == 204
    assert client.get(path).json() == windows[:1]


@pytest.mark.parametrize("payload", [{"price": None}, {"duration_minutes": 90}, {"price": "-1"}])
def test_service_patch_validation(client, setup, payload):
    assert client.patch("/services/1", headers=setup["headers"][0], json=payload).status_code == 422


def test_disabled_service(client, setup):
    assert client.patch("/services/1", headers=setup["headers"][0], json={"active": False}).status_code == 200
    assert book(client, setup).status_code == 409
    assert client.get("/professionals/1/services").json() == []


def test_not_found_and_pagination(client, setup):
    assert client.get(f"/bookings/{uuid4()}", headers=setup["headers"][1]).status_code == 404
    assert client.get("/businesses?limit=101").status_code == 422
    assert client.get("/businesses?offset=1&limit=1").json() == []
    assert (
        client.post(
            "/bookings",
            headers=setup["headers"][1],
            json={"service_id": 999, "starts_at": setup["start"].isoformat()},
        ).status_code
        == 404
    )


def test_production_rejects_demo_signing_key():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(environment="production", jwt_secret="local-demo-key-change-before-deployment-2026")


def test_readiness_reports_dependency_outage(client, monkeypatch):
    from redis import ConnectionError

    monkeypatch.setattr("app.main.redis_connection", lambda: (_ for _ in ()).throw(ConnectionError()))
    assert client.get("/ready").status_code == 503
