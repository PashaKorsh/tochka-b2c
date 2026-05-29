"""
Address schemas — spec b2c/openapi.yaml#AddressCreateRequest / #AddressResponse.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class AddressCreateRequest(BaseModel):
    """spec b2c/openapi.yaml#AddressCreateRequest — required: [country, city, street, building]."""
    country: str = Field(..., max_length=100)
    region: Optional[str] = Field(None, max_length=200)
    city: str = Field(..., max_length=200)
    street: str = Field(..., max_length=200)
    building: str = Field(..., max_length=50)
    apartment: Optional[str] = Field(None, max_length=50)
    postal_code: Optional[str] = Field(None, max_length=20)
    recipient_name: Optional[str] = Field(None, max_length=200)
    recipient_phone: Optional[str] = None
    is_default: bool = False
    comment: Optional[str] = Field(None, max_length=500)


class AddressResponse(AddressCreateRequest):
    """
    spec b2c/openapi.yaml#AddressResponse = AddressCreateRequest + {id, created_at}.
    Used in OrderResponse.address.
    """
    id: UUID
    created_at: datetime

    model_config = {"from_attributes": True}
