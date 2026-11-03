from uuid import UUID

from fastapi import APIRouter, status

from app.dependencies import OrderServiceDep
from app.schemas.order import CreateOrderRequest, OrderResponse

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def place_order(body: CreateOrderRequest, service: OrderServiceDep) -> OrderResponse:
    return await service.place_order(body)


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(order_id: UUID, service: OrderServiceDep) -> OrderResponse:
    return await service.get_order(order_id)
