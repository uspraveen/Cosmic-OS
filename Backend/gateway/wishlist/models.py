from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


DecisionType = Literal["create_new", "skip_duplicate", "append_evidence", "update_existing"]


class WishlistMergedFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    summary: str | None = None
    desired_outcome: str | None = None
    domain: str | None = None
    tags: list[str] = Field(default_factory=list)


class WishlistAdjudicationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: DecisionType
    target_capability_id: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    merged_fields: WishlistMergedFields = Field(default_factory=WishlistMergedFields)
