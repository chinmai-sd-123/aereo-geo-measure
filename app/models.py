import enum
import uuid
from datetime import datetime
from pathlib import Path

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.db import Base
from app.geo import MeasureStatus


class FileStatus(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _str_enum(enum_cls: type[enum.StrEnum]) -> Enum:
    # varchar + check constraint instead of a native PG enum, so adding a value later
    # doesn't need an ALTER TYPE migration
    return Enum(enum_cls, native_enum=False, length=20, create_constraint=True)


class GeoFile(Base):
    __tablename__ = "geo_files"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(10))
    status: Mapped[FileStatus] = mapped_column(_str_enum(FileStatus), default=FileStatus.PENDING)
    crs: Mapped[str | None] = mapped_column(String(255))
    feature_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    features: Mapped[list["Feature"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def path(self) -> Path:
        return settings.upload_dir / f"{self.id}.{self.file_type}"


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (UniqueConstraint("file_id", "index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("geo_files.id", ondelete="CASCADE"))
    index: Mapped[int]
    layer: Mapped[str | None] = mapped_column(String(255))
    geometry_type: Mapped[str | None] = mapped_column(String(32))
    # stored in WGS84 whatever the source CRS was, so the map and spatial queries share one SRID
    geometry: Mapped[WKBElement | None] = mapped_column(Geometry(srid=4326))
    crs: Mapped[str | None] = mapped_column(String(255))
    properties: Mapped[dict] = mapped_column(JSONB, default=dict)
    measure_type: Mapped[str | None] = mapped_column(String(10))
    value: Mapped[float | None]
    unit: Mapped[str | None] = mapped_column(String(5))
    projected_crs: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[MeasureStatus] = mapped_column(_str_enum(MeasureStatus))
    note: Mapped[str | None] = mapped_column(Text)

    file: Mapped[GeoFile] = relationship(back_populates="features")
