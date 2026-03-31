# domain/models/schema_base.py
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictSchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
