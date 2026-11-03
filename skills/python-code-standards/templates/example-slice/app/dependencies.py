"""Providers: build services and repos per request from the shared Resources. No other module constructs them."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap import Resources
from app.clients.payments import PaymentGateway
from app.database.session import session_scope
from app.repos.order_repo import OrderRepo
from app.services.order_service import OrderService
from app.utils.clock import new_id, utc_now


def get_resources(request: Request) -> Resources:
    resources: Resources = request.app.state.resources
    return resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


async def get_session(resources: ResourcesDep) -> AsyncIterator[AsyncSession]:
    async for session in session_scope(resources.session_factory):
        yield session


def get_payments(resources: ResourcesDep) -> PaymentGateway:
    return resources.payments


SessionDep = Annotated[AsyncSession, Depends(get_session)]
PaymentsDep = Annotated[PaymentGateway, Depends(get_payments)]


def get_order_service(session: SessionDep, payments: PaymentsDep) -> OrderService:
    return OrderService(session=session, orders=OrderRepo(session), payments=payments, clock=utc_now, new_id=new_id)


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
