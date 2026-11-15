"""Transactional delivery into the customer's inbox, without external messages."""

from uuid import UUID

from sqlalchemy import select

from .db import SessionLocal
from .domain import now
from .models import Booking, Notification, Outbox


def deliver(event_id: str):
    with SessionLocal.begin() as db:
        event = db.get(Outbox, UUID(event_id))
        if event is None:
            return "missing"
        booking = db.scalar(select(Booking).where(Booking.id == event.booking_id).with_for_update())
        event = db.scalar(
            select(Outbox)
            .where(Outbox.id == event.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if event.status != "pending":
            return event.status
        if event.due_at > now():
            return "not_due"
        if (
            booking.status != "confirmed"
            or booking.version != event.booking_version
            or booking.starts_at <= now()
        ):
            event.status = "skipped"
        else:
            message = (
                "A spot opened and your appointment was confirmed."
                if event.kind == "waitlist_matched"
                else "Your appointment is coming up."
            )
            db.add(
                Notification(
                    event_id=event.id,
                    customer_id=booking.customer_id,
                    booking_id=booking.id,
                    kind=event.kind,
                    message=message,
                )
            )
            event.status = "delivered"
        event.lease_until = None
        return event.status
