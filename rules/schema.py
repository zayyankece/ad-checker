from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

FieldName = Literal["model", "model_family", "msn", "engine_model", "mods_installed", "parts_installed", "external_document", "cert_date"]


class Condition(BaseModel):
    field: FieldName
    op: Literal["in", "not_in", "range", "contains", "not_contains", "requires_external", "on_or_before", "on_or_after"]
    values: list[Union[str, int]] = Field(default_factory=list)
    min: Optional[int] = None
    max: Optional[int] = None
    quote: str


class Group(BaseModel):
    conditions: list[Condition]


class Rule(BaseModel):
    doc_id: str
    scope: Literal["all", "certain", "unknown"]
    groups: list[Group]
    exclusions: list[Condition] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
