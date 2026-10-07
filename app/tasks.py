import uuid
from datetime import UTC, datetime

from celery import Celery
from celery.utils.log import get_task_logger
from geoalchemy2.shape import from_shape
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.geo import FeatureResult, GeoFileError, read_features
from app.models import Feature, FileStatus, GeoFile

celery = Celery("geo", broker=settings.redis_url)
celery.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=300,
    task_time_limit=330,
)

logger = get_task_logger(__name__)

# matches the varchar(255) columns for layer and crs
MAX_TEXT = 255


@celery.task(name="process_file")
def process_file(file_id: str) -> None:
    with SessionLocal() as db:
        geo_file = db.get(GeoFile, uuid.UUID(file_id))
        # PROCESSING is allowed through: with acks_late a crashed run gets redelivered
        if geo_file is None or geo_file.status in (FileStatus.COMPLETED, FileStatus.FAILED):
            return

        geo_file.status = FileStatus.PROCESSING
        db.commit()

        # saving is inside the try too: a failed insert must not leave the file in PROCESSING
        try:
            results = read_features(geo_file.path)
            _save_features(db, geo_file, results)
            _finish(db, geo_file, FileStatus.COMPLETED)
        except GeoFileError as exc:
            db.rollback()
            _finish(db, geo_file, FileStatus.FAILED, error=str(exc))
        except Exception:
            db.rollback()
            logger.exception("unexpected error processing file %s", file_id)
            _finish(db, geo_file, FileStatus.FAILED, error="unexpected error while processing file")


def _save_features(db: Session, geo_file: GeoFile, results: list[FeatureResult]) -> None:
    db.execute(delete(Feature).where(Feature.file_id == geo_file.id))
    if results:
        db.execute(insert(Feature), [_feature_row(geo_file.id, r) for r in results])
    geo_file.feature_count = len(results)
    geo_file.crs = _fit(", ".join(sorted({r.crs for r in results if r.crs}))) or None


def _fit(text: str | None) -> str | None:
    return None if text is None else text[:MAX_TEXT]


def _feature_row(file_id: uuid.UUID, result: FeatureResult) -> dict:
    m = result.measurement
    return {
        "file_id": file_id,
        "index": result.index,
        "layer": _fit(result.layer),
        "geometry_type": result.geometry_type,
        "geometry": None if result.geometry is None else from_shape(result.geometry, srid=4326),
        "crs": _fit(result.crs),
        "properties": result.properties,
        "measure_type": m.measure_type,
        "value": m.value,
        "unit": m.unit,
        "projected_crs": m.projected_crs,
        "status": m.status,
        "note": m.note,
    }


def _finish(db: Session, geo_file: GeoFile, status: FileStatus, error: str | None = None) -> None:
    geo_file.status = status
    geo_file.error = error
    geo_file.processed_at = datetime.now(UTC)
    db.commit()
