"""
Address model for the buyer address book (US-B2C-09 dependency).

spec b2c/openapi.yaml#AddressResponse → AddressCreateRequest:
  required: [country, city, street, building]
  optional: region, apartment, postal_code, recipient_name, recipient_phone, is_default, comment
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from backend.database import Base


class Address(Base):
    __tablename__ = "addresses"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buyer_id = Column(PG_UUID(as_uuid=True), nullable=False, index=True)
    country = Column(String(100), nullable=False)
    region = Column(String(200), nullable=True)
    city = Column(String(200), nullable=False)
    street = Column(String(200), nullable=False)
    building = Column(String(50), nullable=False)
    apartment = Column(String(50), nullable=True)
    postal_code = Column(String(20), nullable=True)
    recipient_name = Column(String(200), nullable=True)
    recipient_phone = Column(String(20), nullable=True)
    is_default = Column(Boolean, nullable=False, default=False)
    comment = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
