"""CI smoke check for the separate dispatcher and worker containers."""

import time

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Notification

for attempt in range(60):
    with SessionLocal() as db:
        found = db.scalar(select(Notification.id).where(Notification.kind == "waitlist_matched").limit(1))
    if found:
        print("PASS: dispatcher and worker delivered the waitlist confirmation.")
        break
    time.sleep(1)
else:
    raise SystemExit("No waitlist notification delivered within 60 seconds.")
