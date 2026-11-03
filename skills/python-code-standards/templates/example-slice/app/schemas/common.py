from typing import Annotated

from pydantic import Field

from app.constants import MAX_ORDER_CENTS

Cents = Annotated[int, Field(gt=0, le=MAX_ORDER_CENTS)]
