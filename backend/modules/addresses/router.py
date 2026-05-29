"""
Addresses router — buyer address book.

spec b2c/openapi.yaml:
  POST /api/v1/buyers/me/addresses  → 201 AddressResponse
  GET  /api/v1/buyers/me/addresses  → 200 List[AddressResponse]
"""
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_user_id
from backend.database import get_db
from backend.modules.addresses.schemas import AddressCreateRequest, AddressResponse
from backend.modules.addresses.service import AddressService

router = APIRouter(prefix="/api/v1/buyers/me", tags=["Addresses"])


@router.post("/addresses", response_model=AddressResponse, status_code=201)
async def create_address(
    payload: AddressCreateRequest,
    db: AsyncSession = Depends(get_db),
    buyer_id: UUID = Depends(get_current_user_id),
) -> AddressResponse:
    return await AddressService.create_address(db, buyer_id=buyer_id, payload=payload)


@router.get("/addresses", response_model=List[AddressResponse])
async def list_addresses(
    db: AsyncSession = Depends(get_db),
    buyer_id: UUID = Depends(get_current_user_id),
) -> List[AddressResponse]:
    return await AddressService.list_addresses(db, buyer_id=buyer_id)
