from datetime import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Credentials(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(pattern=r"^[a-z0-9_]{3,40}$")
    password: str = Field(min_length=12, max_length=128)


class BusinessInput(Input):
    name: str = Field(min_length=2, max_length=100)
    timezone: str = "America/Sao_Paulo"

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use a valid IANA timezone.")
        return value


class ProfessionalInput(Input):
    name: str = Field(min_length=2, max_length=100)


class ServiceInput(Input):
    name: str = Field(min_length=2, max_length=100)
    duration_minutes: int = Field(ge=15, le=240, multiple_of=15)
    price: Decimal = Field(ge=0, max_digits=9, decimal_places=2)


class ServiceUpdate(Input):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    price: Decimal | None = Field(default=None, ge=0, max_digits=9, decimal_places=2)
    active: bool | None = None

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Omit unchanged fields instead of sending null.")
        return self


class Window(Input):
    weekday: int = Field(ge=0, le=6)
    start_minute: int = Field(ge=0, le=1425, multiple_of=15)
    end_minute: int = Field(ge=15, le=1440, multiple_of=15)

    @model_validator(mode="after")
    def duration(self):
        if self.end_minute <= self.start_minute:
            raise ValueError("Window end must be after start.")
        return self


class ScheduleInput(Input):
    windows: list[Window] = Field(max_length=28)

    @model_validator(mode="after")
    def overlaps(self):
        ordered = sorted(self.windows, key=lambda x: (x.weekday, x.start_minute))
        for prev, item in zip(ordered, ordered[1:]):
            if prev.weekday == item.weekday and item.start_minute < prev.end_minute:
                raise ValueError("Working windows cannot overlap.")
        return self


class BlockInput(Input):
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    reason: str = Field(default="Unavailable", max_length=160)

    @model_validator(mode="after")
    def duration(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("Block end must be after start.")
        return self


class BookingInput(Input):
    service_id: int = Field(gt=0)
    starts_at: AwareDatetime


class RescheduleInput(Input):
    starts_at: AwareDatetime


class WaitlistInput(BookingInput):
    accept_automatic_booking: bool

    @field_validator("accept_automatic_booking")
    @classmethod
    def consent(cls, value):
        if not value:
            raise ValueError("Automatic booking requires explicit acceptance.")
        return value


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(Output):
    id: int
    username: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class BusinessOut(Output):
    id: int
    name: str
    timezone: str


class ProfessionalOut(Output):
    id: int
    business_id: int
    name: str
    active: bool


class ServiceOut(Output):
    id: int
    professional_id: int
    name: str
    duration_minutes: int
    price: Decimal
    active: bool


class BlockOut(Output):
    id: int
    professional_id: int
    starts_at: datetime
    ends_at: datetime
    reason: str


class BookingOut(Output):
    id: UUID
    customer_id: int
    professional_id: int
    service_id: int
    service_name: str
    duration_minutes: int
    price: Decimal
    starts_at: datetime
    ends_at: datetime
    status: str
    version: int


class WaitlistOut(Output):
    service_name: str
    duration_minutes: int
    price: Decimal
    id: UUID
    service_id: int
    starts_at: datetime
    status: str
    booking_id: UUID | None


class EventOut(Output):
    action: str
    starts_at: datetime
    ends_at: datetime
    created_at: datetime


class NotificationOut(Output):
    id: UUID
    booking_id: UUID
    kind: str
    message: str
    created_at: datetime


class Slot(BaseModel):
    starts_at: datetime
    ends_at: datetime
