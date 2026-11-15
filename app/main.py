from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from . import domain as d
from . import schemas as s
from .auth import DB, CurrentUser, current_session, issue_token, limit_auth, passwords, redis_connection
from .config import get_settings
from .models import (
    AuthSession,
    Block,
    Booking,
    BookingEvent,
    Business,
    Hours,
    Notification,
    Professional,
    Service,
    User,
    Waitlist,
)

app = FastAPI(
    title="Booking API",
    version="1.0.0",
    description="Appointments, fair waitlists and durable in-app reminders. All prices are BRL.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
Page = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]


@app.exception_handler(IntegrityError)
async def integrity_error(request: Request, exc: IntegrityError):
    code = getattr(exc.orig, "sqlstate", None)
    if code in ("23505", "23P01"):
        return JSONResponse(
            status_code=409, content={"detail": "Duplicate record or overlapping appointment."}
        )
    # Do not expose SQL statements, credentials or database internals.
    return JSONResponse(status_code=500, content={"detail": "Database constraint failure."})


@app.get("/health", tags=["Operations"])
def health():
    return {"status": "ok"}


@app.get("/ready", tags=["Operations"])
def ready(db: DB):
    try:
        db.execute(text("SELECT 1"))
        redis_connection().ping()
    except Exception:
        raise HTTPException(503, "Dependencies unavailable.")
    return {"status": "ready"}


@app.post(
    "/auth/register",
    response_model=s.UserOut,
    status_code=201,
    dependencies=[Depends(limit_auth)],
    tags=["Authentication"],
)
def register(payload: s.Credentials, db: DB):
    user = User(username=payload.username, password_hash=passwords.hash(payload.password))
    db.add(user)
    db.commit()
    return user


_DUMMY_HASH = passwords.hash("dummy-password-for-timing-only")


@app.post(
    "/auth/login", response_model=s.TokenOut, dependencies=[Depends(limit_auth)], tags=["Authentication"]
)
def login(payload: s.Credentials, db: DB):
    user = db.scalar(select(User).where(User.username == payload.username))
    valid = passwords.verify(payload.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not valid:
        raise HTTPException(401, "Invalid credentials.")
    token = issue_token(db, user)
    db.commit()
    return token


@app.post("/auth/logout", status_code=204, tags=["Authentication"])
def logout(db: DB, session: Annotated[AuthSession, Depends(current_session)]):
    session.revoked = True
    db.commit()


@app.get("/auth/me", response_model=s.UserOut, tags=["Authentication"])
def me(user: CurrentUser):
    return user


@app.get("/businesses", response_model=list[s.BusinessOut], tags=["Catalogue"])
def businesses(db: DB, offset: Page = 0, limit: Limit = 50):
    return db.scalars(select(Business).order_by(Business.id).offset(offset).limit(limit)).all()


@app.post("/businesses", response_model=s.BusinessOut, status_code=201, tags=["Owner"])
def create_business(payload: s.BusinessInput, db: DB, user: CurrentUser):
    item = Business(owner_id=user.id, **payload.model_dump())
    db.add(item)
    db.commit()
    return item


@app.get(
    "/businesses/{business_id}/professionals", response_model=list[s.ProfessionalOut], tags=["Catalogue"]
)
def professionals(business_id: int, db: DB, offset: Page = 0, limit: Limit = 50):
    return db.scalars(
        select(Professional)
        .where(Professional.business_id == business_id)
        .order_by(Professional.id)
        .offset(offset)
        .limit(limit)
    ).all()


@app.post(
    "/businesses/{business_id}/professionals",
    response_model=s.ProfessionalOut,
    status_code=201,
    tags=["Owner"],
)
def create_professional(business_id: int, payload: s.ProfessionalInput, db: DB, user: CurrentUser):
    business = db.get(Business, business_id)
    if not business or business.owner_id != user.id:
        raise HTTPException(404, "Business not found.")
    item = Professional(business_id=business.id, **payload.model_dump())
    db.add(item)
    db.commit()
    return item


@app.get("/professionals/{professional_id}/services", response_model=list[s.ServiceOut], tags=["Catalogue"])
def services(professional_id: int, db: DB, offset: Page = 0, limit: Limit = 50):
    return db.scalars(
        select(Service)
        .where(Service.professional_id == professional_id, Service.active.is_(True))
        .order_by(Service.id)
        .offset(offset)
        .limit(limit)
    ).all()


@app.post(
    "/professionals/{professional_id}/services", response_model=s.ServiceOut, status_code=201, tags=["Owner"]
)
def create_service(professional_id: int, payload: s.ServiceInput, db: DB, user: CurrentUser):
    pro = d.professional(db, professional_id, True)
    d.owned(db, pro, user)
    item = Service(professional_id=pro.id, **payload.model_dump())
    db.add(item)
    db.commit()
    return item


@app.patch("/services/{service_id}", response_model=s.ServiceOut, tags=["Owner"])
def update_service(service_id: int, payload: s.ServiceUpdate, db: DB, user: CurrentUser):
    item = db.get(Service, service_id)
    if not item:
        raise HTTPException(404, "Service not found.")
    pro = d.professional(db, item.professional_id, True)
    d.owned(db, pro, user)
    db.refresh(item)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    db.commit()
    return item


@app.put("/professionals/{professional_id}/hours", status_code=204, tags=["Owner"])
def replace_hours(professional_id: int, payload: s.ScheduleInput, db: DB, user: CurrentUser):
    pro = d.professional(db, professional_id, True)
    d.owned(db, pro, user)
    if db.scalar(
        select(Booking.id)
        .where(Booking.professional_id == pro.id, Booking.status == "confirmed", Booking.ends_at > d.now())
        .limit(1)
    ):
        raise HTTPException(409, "Resolve future appointments before replacing working hours.")
    db.execute(delete(Hours).where(Hours.professional_id == pro.id))
    db.add_all([Hours(professional_id=pro.id, **window.model_dump()) for window in payload.windows])
    db.commit()


@app.get("/professionals/{professional_id}/hours", response_model=list[s.Window], tags=["Catalogue"])
def working_hours(professional_id: int, db: DB):
    rows = db.scalars(
        select(Hours)
        .where(Hours.professional_id == professional_id)
        .order_by(Hours.weekday, Hours.start_minute)
    ).all()
    return [
        {"weekday": row.weekday, "start_minute": row.start_minute, "end_minute": row.end_minute}
        for row in rows
    ]


@app.post(
    "/professionals/{professional_id}/blocks", response_model=s.BlockOut, status_code=201, tags=["Owner"]
)
def block(professional_id: int, payload: s.BlockInput, db: DB, user: CurrentUser):
    pro = d.professional(db, professional_id, True)
    d.owned(db, pro, user)
    if db.scalar(
        d.overlapping(Booking, pro.id, payload.starts_at, payload.ends_at)
        .where(Booking.status == "confirmed")
        .limit(1)
    ):
        raise HTTPException(409, "Block would invalidate an existing booking.")
    item = Block(professional_id=pro.id, **payload.model_dump())
    db.add(item)
    db.commit()
    return item


@app.delete("/blocks/{block_id}", status_code=204, tags=["Owner"])
def unblock(block_id: int, db: DB, user: CurrentUser):
    item = db.get(Block, block_id)
    if not item:
        raise HTTPException(404, "Block not found.")
    d.owned(db, d.professional(db, item.professional_id, True), user)
    db.delete(item)
    db.commit()


@app.get("/services/{service_id}/availability", response_model=list[s.Slot], tags=["Catalogue"])
def availability(service_id: int, first_day: date, db: DB, days: Annotated[int, Query(ge=1, le=7)] = 1):
    service, pro = d.service_and_calendar(db, service_id, False)
    return d.slots(db, service, pro, first_day, days)


@app.post("/bookings", response_model=s.BookingOut, status_code=201, tags=["Bookings"])
def book(payload: s.BookingInput, db: DB, user: CurrentUser):
    service, pro = d.service_and_calendar(db, payload.service_id)
    item = d.create_booking(db, user.id, service, pro, payload.starts_at)
    db.commit()
    return item


@app.get("/bookings", response_model=list[s.BookingOut], tags=["Bookings"])
def bookings(db: DB, user: CurrentUser, offset: Page = 0, limit: Limit = 50):
    return db.scalars(
        select(Booking)
        .where(Booking.customer_id == user.id)
        .order_by(Booking.starts_at, Booking.id)
        .offset(offset)
        .limit(limit)
    ).all()


@app.get("/professionals/{professional_id}/bookings", response_model=list[s.BookingOut], tags=["Owner"])
def calendar(professional_id: int, db: DB, user: CurrentUser, offset: Page = 0, limit: Limit = 50):
    d.owned(db, d.professional(db, professional_id), user)
    return db.scalars(
        select(Booking)
        .where(Booking.professional_id == professional_id)
        .order_by(Booking.starts_at, Booking.id)
        .offset(offset)
        .limit(limit)
    ).all()


@app.get("/bookings/{booking_id}", response_model=s.BookingOut, tags=["Bookings"])
def booking(booking_id: UUID, db: DB, user: CurrentUser):
    return d.accessible_booking(db, booking_id, user)[0]


@app.get("/bookings/{booking_id}/history", response_model=list[s.EventOut], tags=["Bookings"])
def history(booking_id: UUID, db: DB, user: CurrentUser):
    d.accessible_booking(db, booking_id, user)
    return db.scalars(
        select(BookingEvent).where(BookingEvent.booking_id == booking_id).order_by(BookingEvent.id)
    ).all()


def editable(item):
    if item.status != "confirmed" or item.starts_at <= d.now():
        raise HTTPException(409, "Only future confirmed bookings can be changed.")


@app.post("/bookings/{booking_id}/cancel", response_model=s.BookingOut, tags=["Bookings"])
def cancel(booking_id: UUID, db: DB, user: CurrentUser):
    item, pro = d.accessible_booking(db, booking_id, user, True)
    editable(item)
    item.status = "cancelled"
    item.version += 1
    d.invalidate_reminders(db, item)
    d.record(db, item, user.id, "cancelled")
    db.flush()
    d.promote_waitlist(db, pro, item.starts_at)
    db.commit()
    return item


@app.post("/bookings/{booking_id}/reschedule", response_model=s.BookingOut, tags=["Bookings"])
def reschedule(booking_id: UUID, payload: s.RescheduleInput, db: DB, user: CurrentUser):
    item, pro = d.accessible_booking(db, booking_id, user, True)
    editable(item)
    start, end = d.validate_slot(db, pro, payload.starts_at, item.duration_minutes, ignore=item.id)
    if start == item.starts_at:
        return item
    old_start = item.starts_at
    item.starts_at, item.ends_at = start, end
    item.version += 1
    d.invalidate_reminders(db, item)
    d.record(db, item, user.id, "rescheduled")
    d.reminder(db, item)
    db.flush()
    d.promote_waitlist(db, pro, old_start)
    db.commit()
    return item


@app.post("/waitlist", response_model=s.WaitlistOut, status_code=201, tags=["Waitlist"])
def join_waitlist(payload: s.WaitlistInput, db: DB, user: CurrentUser):
    service, pro = d.service_and_calendar(db, payload.service_id)
    start, end = d.validate_slot(db, pro, payload.starts_at, service.duration_minutes, check_bookings=False)
    if not db.scalar(
        d.overlapping(Booking, pro.id, start, end).where(Booking.status == "confirmed").limit(1)
    ):
        raise HTTPException(409, "Slot is available: book it directly.")
    item = Waitlist(
        customer_id=user.id,
        service_id=service.id,
        starts_at=start,
        service_name=service.name,
        duration_minutes=service.duration_minutes,
        price=service.price,
    )
    db.add(item)
    db.commit()
    return item


@app.get("/waitlist", response_model=list[s.WaitlistOut], tags=["Waitlist"])
def waitlist(db: DB, user: CurrentUser, offset: Page = 0, limit: Limit = 50):
    return db.scalars(
        select(Waitlist)
        .where(Waitlist.customer_id == user.id)
        .order_by(Waitlist.created_at, Waitlist.id)
        .offset(offset)
        .limit(limit)
    ).all()


@app.delete("/waitlist/{entry_id}", status_code=204, tags=["Waitlist"])
def leave_waitlist(entry_id: UUID, db: DB, user: CurrentUser):
    entry = db.get(Waitlist, entry_id)
    if not entry or entry.customer_id != user.id:
        raise HTTPException(404, "Waitlist entry not found.")
    service = db.get(Service, entry.service_id)
    d.professional(db, service.professional_id, True)
    db.refresh(entry)
    if entry.status != "waiting":
        raise HTTPException(409, "Entry is no longer waiting; manage its booking instead.")
    entry.status = "cancelled"
    db.commit()


@app.get("/notifications", response_model=list[s.NotificationOut], tags=["Notifications"])
def inbox(db: DB, user: CurrentUser, offset: Page = 0, limit: Limit = 50):
    return db.scalars(
        select(Notification)
        .where(Notification.customer_id == user.id)
        .order_by(Notification.created_at.desc(), Notification.id)
        .offset(offset)
        .limit(limit)
    ).all()
