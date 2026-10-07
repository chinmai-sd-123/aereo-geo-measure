from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.geo import MeasureStatus
from app.models import FileStatus


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    file_type: str
    status: FileStatus
    feature_count: int
    crs: str | None
    error: str | None
    created_at: datetime
    processed_at: datetime | None


class FileDetail(FileOut):
    geometry_types: dict[str, int]


class FilePage(BaseModel):
    items: list[FileOut]
    total: int
    limit: int
    offset: int


class MeasurementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    feature_id: int = Field(validation_alias="index")
    layer: str | None
    geometry_type: str | None
    status: MeasureStatus
    measure_type: str | None
    value: float | None
    unit: str | None
    projected_crs: str | None
    note: str | None

    @field_serializer("value")
    def _round(self, value: float | None) -> float | None:
        return None if value is None else round(value, 3)


class MeasurementSummary(BaseModel):
    total_area_m2: float
    total_area_ha: float
    total_length_m: float
    total_length_km: float
    by_status: dict[str, int]


class MeasurementPage(BaseModel):
    file_id: UUID
    summary: MeasurementSummary
    items: list[MeasurementOut]
    total: int
    limit: int
    offset: int


class FeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    features: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
