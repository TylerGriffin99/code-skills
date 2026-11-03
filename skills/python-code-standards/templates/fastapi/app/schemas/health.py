from typing import Literal

from app.schemas.base import StrictModel


class HealthResponse(StrictModel):
    status: Literal["ok"]
