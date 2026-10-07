import json
import logging
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, UploadFile, status
from kombu.exceptions import OperationalError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import DbSession
from app.models import Feature, FileStatus, GeoFile
from app.schemas import (
    FeatureCollection,
    FileDetail,
    FileOut,
    FilePage,
    MeasurementOut,
    MeasurementPage,
    MeasurementSummary,
)
from app.tasks import process_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/files", tags=["files"])

ALLOWED_SUFFIXES = {".kml", ".zip"}
CHUNK_SIZE = 1024 * 1024

Offset = Annotated[int, Query(ge=0)]


@router.post("/", status_code=status.HTTP_202_ACCEPTED)
def upload_file(file: UploadFile, db: DbSession) -> FileOut:
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Upload a .kml file or a .zip containing a shapefile"
        )

    geo_file = GeoFile(filename=filename[-255:], file_type=suffix.lstrip("."))
    db.add(geo_file)
    db.flush()
    _save_upload(file, geo_file.path)
    db.commit()
    db.refresh(geo_file)

    try:
        process_file.delay(str(geo_file.id))
    except OperationalError:
        logger.exception("could not queue file %s", geo_file.id)
        geo_file.status = FileStatus.FAILED
        geo_file.error = "processing queue unavailable"
        db.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Processing queue unavailable, try again later"
        ) from None

    return FileOut.model_validate(geo_file)


@router.get("/")
def list_files(
    db: DbSession, limit: Annotated[int, Query(ge=1, le=100)] = 20, offset: Offset = 0
) -> FilePage:
    files = db.scalars(
        select(GeoFile).order_by(GeoFile.created_at.desc()).limit(limit).offset(offset)
    )
    return FilePage(
        items=[FileOut.model_validate(f) for f in files],
        total=db.scalar(select(func.count()).select_from(GeoFile)),
        limit=limit,
        offset=offset,
    )


@router.get("/{file_id}/")
def get_file(file_id: UUID, db: DbSession) -> FileDetail:
    geo_file = _get_file(db, file_id)
    type_counts = db.execute(
        select(func.coalesce(Feature.geometry_type, "Unknown"), func.count())
        .where(Feature.file_id == file_id)
        .group_by(Feature.geometry_type)
    )
    return FileDetail(
        **FileOut.model_validate(geo_file).model_dump(), geometry_types=dict(type_counts.all())
    )


@router.get("/{file_id}/measurements/")
def get_measurements(
    file_id: UUID,
    db: DbSession,
    geometry_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Offset = 0,
) -> MeasurementPage:
    _get_completed_file(db, file_id)
    filters = _feature_filters(file_id, geometry_type)

    rows = db.execute(
        select(
            Feature.index,
            Feature.layer,
            Feature.geometry_type,
            Feature.status,
            Feature.measure_type,
            Feature.value,
            Feature.unit,
            Feature.projected_crs,
            Feature.note,
        )
        .where(*filters)
        .order_by(Feature.index)
        .limit(limit)
        .offset(offset)
    )
    total, area, length = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Feature.value).filter(Feature.measure_type == "area"), 0.0),
            func.coalesce(func.sum(Feature.value).filter(Feature.measure_type == "length"), 0.0),
        ).where(*filters)
    ).one()
    by_status = db.execute(
        select(Feature.status, func.count()).where(*filters).group_by(Feature.status)
    )

    return MeasurementPage(
        file_id=file_id,
        summary=MeasurementSummary(
            total_area_m2=round(area, 3),
            total_area_ha=round(area / 10_000, 4),
            total_length_m=round(length, 3),
            total_length_km=round(length / 1000, 4),
            by_status=dict(by_status.all()),
        ),
        items=[MeasurementOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{file_id}/features/")
def get_features(
    file_id: UUID,
    db: DbSession,
    geometry_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
    offset: Offset = 0,
) -> FeatureCollection:
    _get_completed_file(db, file_id)
    filters = _feature_filters(file_id, geometry_type)

    rows = db.execute(
        select(
            Feature.index,
            Feature.layer,
            Feature.geometry_type,
            Feature.crs,
            Feature.properties,
            Feature.status,
            Feature.measure_type,
            Feature.value,
            Feature.unit,
            func.ST_AsGeoJSON(Feature.geometry, 7).label("geojson"),
        )
        .where(*filters)
        .order_by(Feature.index)
        .limit(limit)
        .offset(offset)
    )
    features = [
        {
            "type": "Feature",
            "id": row.index,
            "geometry": json.loads(row.geojson) if row.geojson else None,
            "properties": {
                "layer": row.layer,
                "geometry_type": row.geometry_type,
                "source_crs": row.crs,
                "measurement": {
                    "status": row.status,
                    "type": row.measure_type,
                    "value": None if row.value is None else round(row.value, 3),
                    "unit": row.unit,
                },
                "attributes": row.properties,
            },
        }
        for row in rows
    ]
    total = db.scalar(select(func.count()).where(*filters))
    return FeatureCollection(features=features, total=total, limit=limit, offset=offset)


def _get_file(db: Session, file_id: UUID) -> GeoFile:
    geo_file = db.get(GeoFile, file_id)
    if geo_file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return geo_file


def _get_completed_file(db: Session, file_id: UUID) -> GeoFile:
    geo_file = _get_file(db, file_id)
    if geo_file.status == FileStatus.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"File failed to process: {geo_file.error}")
    if geo_file.status != FileStatus.COMPLETED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"File is {geo_file.status}, try again shortly"
        )
    return geo_file


def _feature_filters(file_id: UUID, geometry_type: str | None) -> list:
    filters = [Feature.file_id == file_id]
    if geometry_type:
        filters.append(func.lower(Feature.geometry_type) == geometry_type.lower())
    return filters


def _save_upload(file: UploadFile, dest: Path) -> None:
    limit = settings.max_upload_mb * 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := file.file.read(CHUNK_SIZE):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"File is larger than {settings.max_upload_mb} MB",
                    )
                out.write(chunk)
        if written == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is empty")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
