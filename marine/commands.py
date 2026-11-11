"""Shared validated business commands; independent of route registration."""

from pydantic import BaseModel, ConfigDict, Field


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: str = Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")

class CreateBatch(Command):
    batchId: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    speciesId: str = Field(min_length=1, max_length=128)
    massGrams: int = Field(gt=0, le=10**12)
    areaCode: str = Field(min_length=1, max_length=64)

class Transition(Command):
    expectedRevision: int = Field(ge=1)
    nextState: str = Field(pattern=r"^(LANDED|PROCESSED|IN_COLD_CHAIN|DELIVERED|RECALLED)$")
