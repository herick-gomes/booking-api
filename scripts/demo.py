"""Exercise the public HTTP API against a running local stack. No emails are sent."""

import os
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx


def main():
    base_url = os.getenv("BOOKING_BASE_URL", "http://localhost:8000")
    suffix = secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    with httpx.Client(base_url=base_url, timeout=15, trust_env=False) as client:

        def call(method, path, expected=200, token=None, **kwargs):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = client.request(method, path, headers=headers, **kwargs)
            if response.status_code != expected:
                raise RuntimeError(
                    f"{method} {path}: expected {expected}, received {response.status_code}: {response.text}"
                )
            return response.json() if response.content else None

        call("GET", "/ready")
        tokens = []
        for role in ("owner", "customer", "waiting"):
            credentials = {"username": f"{role}_{suffix}", "password": password}
            call("POST", "/auth/register", 201, json=credentials)
            tokens.append(call("POST", "/auth/login", json=credentials)["access_token"])
        owner, customer, waiting = tokens
        business = call(
            "POST",
            "/businesses",
            201,
            owner,
            json={"name": f"Studio Demo {suffix}", "timezone": "America/Sao_Paulo"},
        )
        professional = call(
            "POST", f"/businesses/{business['id']}/professionals", 201, owner, json={"name": "Ana Demo"}
        )
        pro_id = professional["id"]
        service = call(
            "POST",
            f"/professionals/{pro_id}/services",
            201,
            owner,
            json={"name": "Personal training", "duration_minutes": 60, "price": "100.00"},
        )
        call(
            "PUT",
            f"/professionals/{pro_id}/hours",
            204,
            owner,
            json={"windows": [{"weekday": d, "start_minute": 480, "end_minute": 1080} for d in range(7)]},
        )
        start = (datetime.now(ZoneInfo("America/Sao_Paulo")) + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        intent = {"service_id": service["id"], "starts_at": start.isoformat()}
        booking = call("POST", "/bookings", 201, customer, json=intent)
        call("POST", "/bookings", 409, waiting, json=intent)
        entry = call("POST", "/waitlist", 201, waiting, json={**intent, "accept_automatic_booking": True})
        call("POST", f"/bookings/{booking['id']}/cancel", token=customer)
        matched = call("GET", "/waitlist", token=waiting)[0]
        assert matched["id"] == entry["id"] and matched["status"] == "matched"
        print(
            "PASS: authenticated setup, booking, conflict rejection, cancellation and automatic waitlist promotion."
        )
        print(f"Business ID: {business['id']}; promoted booking: {matched['booking_id']}")
        print(
            "The dispatcher/worker will place a confirmation in the demo customer inbox. No external messages are sent."
        )
        print(
            "Demo accounts and records remain in this local database; credentials are not printed or saved."
        )


if __name__ == "__main__":
    main()
