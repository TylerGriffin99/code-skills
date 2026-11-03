from pydantic import BaseModel, ConfigDict


class ChargeResult(BaseModel):
    """Upstream response: ignore fields we do not use, so a provider addition does not break us."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    charge_id: str
