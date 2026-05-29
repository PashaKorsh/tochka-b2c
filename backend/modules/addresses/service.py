"""
AddressService — CRUD for buyer address book (US-B2C-09 dependency).

spec b2c/openapi.yaml — POST/GET /api/v1/buyers/me/addresses
IDOR: buyer_id always from JWT; filtering by buyer_id is mandatory.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.addresses.models import Address
from backend.modules.addresses.schemas import AddressCreateRequest, AddressResponse


class AddressService:
    @staticmethod
    async def create_address(
        db: AsyncSession,
        *,
        buyer_id: UUID,
        payload: AddressCreateRequest,
    ) -> AddressResponse:
        addr = Address(
            buyer_id=buyer_id,
            country=payload.country,
            region=payload.region,
            city=payload.city,
            street=payload.street,
            building=payload.building,
            apartment=payload.apartment,
            postal_code=payload.postal_code,
            recipient_name=payload.recipient_name,
            recipient_phone=payload.recipient_phone,
            is_default=payload.is_default,
            comment=payload.comment,
        )
        db.add(addr)
        await db.commit()
        await db.refresh(addr)
        return AddressResponse.model_validate(addr)

    @staticmethod
    async def list_addresses(
        db: AsyncSession,
        *,
        buyer_id: UUID,
    ) -> list[AddressResponse]:
        result = await db.execute(
            select(Address)
            .where(Address.buyer_id == buyer_id)
            .order_by(Address.created_at.desc())
        )
        return [AddressResponse.model_validate(row) for row in result.scalars().all()]

    @staticmethod
    async def get_address(
        db: AsyncSession,
        *,
        buyer_id: UUID,
        address_id: UUID,
    ) -> AddressResponse | None:
        """Returns None when address doesn't exist or belongs to another buyer (IDOR guard)."""
        result = await db.execute(
            select(Address).where(
                Address.id == address_id,
                Address.buyer_id == buyer_id,
            )
        )
        addr = result.scalar_one_or_none()
        return AddressResponse.model_validate(addr) if addr else None
