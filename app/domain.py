"""Scheduling rules. Every calendar mutation first locks the professional row."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .models import Block, Booking, BookingEvent, Business, Hours, Outbox, Professional, Service, Waitlist


def now():
    return datetime.now(timezone.utc)


def professional(db, ident, lock=False):
    query = select(Professional).where(Professional.id == ident)
    if lock:
        query = query.with_for_update()
    item = db.scalar(query.execution_options(populate_existing=True))
    if not item:
        raise HTTPException(404, "Professional not found.")
    return item


def owned(db, pro, user):
    business = db.get(Business, pro.business_id)
    if business.owner_id != user.id:
        raise HTTPException(404, "Professional not found.")
    return business


def service_and_calendar(db, service_id, lock=True):
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(404, "Service not found.")
    pro = professional(db, service.professional_id, lock)
    # A price/active update may have committed while we waited for the lock.
    db.refresh(service)
    if not pro.active or not service.active:
        raise HTTPException(409, "Service unavailable.")
    return service, pro


def overlapping(model, pro_id, start, end):
    return select(model).where(model.professional_id == pro_id, model.starts_at < end, model.ends_at > start)


def validate_slot(db, pro, start, duration, ignore=None, check_bookings=True, calendar=None):
    start = start.astimezone(timezone.utc)
    end = start + timedelta(minutes=duration)
    if start < now() + timedelta(minutes=15) or start > now() + timedelta(days=90):
        raise HTTPException(409, "Book between 15 minutes and 90 days in advance.")
    zone = calendar["zone"] if calendar else ZoneInfo(db.get(Business, pro.business_id).timezone)
    local, finish = start.astimezone(zone), end.astimezone(zone)
    if local.second or local.microsecond or local.minute % 15:
        raise HTTPException(409, "Start must align with the 15-minute local grid.")
    if local.date() != finish.date() or local.utcoffset() != finish.utcoffset():
        raise HTTPException(409, "Appointments cannot cross midnight or a DST transition.")
    minute = local.hour * 60 + local.minute
    windows = (
        calendar["hours"]
        if calendar
        else db.scalars(select(Hours).where(Hours.professional_id == pro.id)).all()
    )
    if not any(
        w.weekday == local.weekday() and w.start_minute <= minute and minute + duration <= w.end_minute
        for w in windows
    ):
        raise HTTPException(409, "Outside working hours.")
    if calendar:
        blocked = any(b.starts_at < end and b.ends_at > start for b in calendar["blocks"])
    else:
        blocked = db.scalar(overlapping(Block, pro.id, start, end).limit(1))
    if blocked:
        raise HTTPException(409, "Professional is unavailable.")
    if check_bookings:
        if calendar:
            occupied = any(
                b.id != ignore and b.starts_at < end and b.ends_at > start for b in calendar["bookings"]
            )
        else:
            query = overlapping(Booking, pro.id, start, end).where(Booking.status == "confirmed")
            if ignore:
                query = query.where(Booking.id != ignore)
            occupied = db.scalar(query.limit(1))
        if occupied:
            raise HTTPException(409, "This time is already booked.")
    return start, end


def record(db, booking, actor_id, action):
    db.add(
        BookingEvent(
            booking_id=booking.id,
            actor_id=actor_id,
            action=action,
            starts_at=booking.starts_at,
            ends_at=booking.ends_at,
        )
    )


def reminder(db, booking, kind="reminder"):
    due = max(now(), booking.starts_at - timedelta(hours=24)) if kind == "reminder" else now()
    db.add(Outbox(booking_id=booking.id, booking_version=booking.version, kind=kind, due_at=due))


def invalidate_reminders(db, booking):
    db.execute(
        update(Outbox)
        .where(Outbox.booking_id == booking.id, Outbox.status == "pending")
        .values(status="skipped", lease_until=None)
    )


def create_booking(db, customer_id, service, pro, start, actor_id=None, snapshot=None):
    terms = snapshot or service
    start, end = validate_slot(db, pro, start, terms.duration_minutes)
    booking = Booking(
        customer_id=customer_id,
        professional_id=pro.id,
        service_id=service.id,
        service_name=terms.service_name if snapshot else service.name,
        duration_minutes=terms.duration_minutes,
        price=terms.price,
        starts_at=start,
        ends_at=end,
    )
    db.add(booking)
    db.flush()  # PostgreSQL also enforces the customer's cross-business conflicts.
    record(db, booking, actor_id or customer_id, "created")
    reminder(db, booking)
    return booking


def accessible_booking(db, booking_id, user, lock=False):
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(404, "Booking not found.")
    pro = professional(db, booking.professional_id, lock)
    business = db.get(Business, pro.business_id)
    if user.id != booking.customer_id and user.id != business.owner_id:
        raise HTTPException(404, "Booking not found.")
    if lock:
        # Delivery workers lock bookings, but never calendars: keep this order.
        booking = db.scalar(
            select(Booking)
            .where(Booking.id == booking_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    return booking, pro


def promote_waitlist(db, pro, start):
    candidates = db.scalars(
        select(Waitlist)
        .join(Service)
        .where(Service.professional_id == pro.id, Waitlist.starts_at == start, Waitlist.status == "waiting")
        .order_by(Waitlist.created_at, Waitlist.id)
        .with_for_update(of=Waitlist)
    ).all()
    for candidate in candidates:
        service = db.get(Service, candidate.service_id)
        if not service.active:
            continue
        try:
            # A customer may have booked elsewhere since joining the queue.
            with db.begin_nested():
                booking = create_booking(db, candidate.customer_id, service, pro, start, snapshot=candidate)
                candidate.status, candidate.booking_id = "matched", booking.id
                reminder(db, booking, "waitlist_matched")
                db.flush()
            return booking
        except (HTTPException, IntegrityError):
            continue
    return None


def slots(db, service, pro, first_day, days):
    zone = ZoneInfo(db.get(Business, pro.business_id).timezone)
    lower = datetime.combine(first_day, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    upper = datetime.combine(first_day + timedelta(days=days), datetime.min.time(), tzinfo=zone).astimezone(
        timezone.utc
    )
    calendar = {
        "zone": zone,
        "hours": db.scalars(select(Hours).where(Hours.professional_id == pro.id)).all(),
        "blocks": db.scalars(overlapping(Block, pro.id, lower, upper)).all(),
        "bookings": db.scalars(
            overlapping(Booking, pro.id, lower, upper).where(Booking.status == "confirmed")
        ).all(),
    }
    result, seen = [], set()
    for day_offset in range(days):
        day = first_day + timedelta(days=day_offset)
        for minute in range(0, 1440, 15):
            naive = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)
            for fold in (0, 1):
                local = naive.replace(tzinfo=zone, fold=fold)
                start = local.astimezone(timezone.utc)
                if start in seen or start.astimezone(zone).replace(tzinfo=None) != naive:
                    continue
                seen.add(start)
                try:
                    start, end = validate_slot(db, pro, start, service.duration_minutes, calendar=calendar)
                    result.append({"starts_at": start, "ends_at": end})
                except HTTPException:
                    continue
    return sorted(result, key=lambda slot: slot["starts_at"])
