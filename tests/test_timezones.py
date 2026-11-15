from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app import domain
from app.db import SessionLocal
from app.models import Business, Hours


def new_york_hours():
    with SessionLocal.begin() as db:
        db.get(Business, 1).timezone = "America/New_York"
        db.execute(delete(Hours).where(Hours.professional_id == 1))
        db.add(Hours(professional_id=1, weekday=6, start_minute=0, end_minute=240))


def test_spring_dst_nonexistent_times_and_crossing_rejected(client, setup, monkeypatch):
    new_york_hours()
    monkeypatch.setattr(domain, "now", lambda: datetime(2027, 3, 13, tzinfo=timezone.utc))
    result = client.get("/services/1/availability", params={"first_day": "2027-03-14"}).json()
    starts = [datetime.fromisoformat(slot["starts_at"].replace("Z", "+00:00")) for slot in result]
    assert len(starts) == len(set(starts))
    # 01:00 EST would end at 03:00 EDT: elapsed duration is valid but policy rejects offset changes.
    response = client.post(
        "/bookings",
        headers=setup["headers"][1],
        json={"service_id": 1, "starts_at": "2027-03-14T01:00:00-05:00"},
    )
    assert response.status_code == 409
    assert all(start.hour != 6 for start in starts)


def test_fall_dst_two_real_instants_are_distinct(client, setup, monkeypatch):
    new_york_hours()
    monkeypatch.setattr(domain, "now", lambda: datetime(2027, 11, 6, tzinfo=timezone.utc))
    # Use a 15-minute service to expose both occurrences of 01:00.
    response = client.post(
        "/professionals/1/services",
        headers=setup["headers"][0],
        json={"name": "Short", "duration_minutes": 15, "price": "10"},
    )
    ident = response.json()["id"]
    result = client.get(f"/services/{ident}/availability", params={"first_day": "2027-11-07"}).json()
    starts = {datetime.fromisoformat(slot["starts_at"].replace("Z", "+00:00")) for slot in result}
    assert datetime(2027, 11, 7, 5, tzinfo=timezone.utc) in starts
    assert datetime(2027, 11, 7, 6, tzinfo=timezone.utc) in starts
    assert len(starts) == len(result)


def test_timezone_and_invalid_windows(client, setup):
    assert (
        client.post(
            "/businesses", headers=setup["headers"][0], json={"name": "Bad zone", "timezone": "Mars/Olympus"}
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/professionals/1/hours",
            headers=setup["headers"][0],
            json={"windows": [{"weekday": 0, "start_minute": 600, "end_minute": 480}]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/professionals/1/blocks",
            headers=setup["headers"][0],
            json={
                "starts_at": setup["start"].isoformat(),
                "ends_at": (setup["start"] - timedelta(hours=1)).isoformat(),
            },
        ).status_code
        == 422
    )


def test_availability_rejects_extreme_dates(client, setup):
    for first_day in ("0001-01-01", "9999-12-31"):
        response = client.get("/services/1/availability", params={"first_day": first_day, "days": 7})
        assert response.status_code == 422
