import uuid

import pytest
from kombu.exceptions import OperationalError
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Feature, GeoFile
from app.tasks import process_file


def upload(client, path, filename=None):
    with open(path, "rb") as fh:
        return client.post("/api/files/", files={"file": (filename or path.name, fh)})


def upload_and_fetch(client, path, filename=None):
    response = upload(client, path, filename)
    assert response.status_code == 202, response.text
    return client.get(f"/api/files/{response.json()['id']}/").json()


def test_upload_kml_returns_accepted_and_is_processed(client, sample_kml):
    response = upload(client, sample_kml)

    assert response.status_code == 202
    body = response.json()
    assert body["filename"] == "survey.kml"
    assert body["status"] == "PENDING"

    info = client.get(f"/api/files/{body['id']}/").json()
    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 5
    assert info["crs"] == "EPSG:4326"
    assert info["processed_at"] is not None


def test_upload_zipped_shapefile(client, make_shapefile_zip, parcels):
    info = upload_and_fetch(client, make_shapefile_zip({"parcels": parcels}), "parcels.zip")

    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 2
    assert info["crs"] == "EPSG:32643"


def test_features_are_stored_in_wgs84(client, sample_kml):
    info = upload_and_fetch(client, sample_kml)

    with SessionLocal() as db:
        file_id = uuid.UUID(info["id"])
        count = db.scalar(select(func.count()).where(Feature.file_id == file_id))
        srids = db.scalars(
            select(func.ST_SRID(Feature.geometry)).where(Feature.file_id == file_id).distinct()
        ).all()
    assert count == 5
    assert srids == [4326]


def test_stored_file_uses_generated_name(client, sample_kml):
    info = upload_and_fetch(client, sample_kml, filename="../../etc/evil.kml")

    assert info["filename"] == "evil.kml"
    assert (settings.upload_dir / f"{info['id']}.kml").exists()


def test_corrupt_zip_marks_file_failed(client, tmp_path):
    path = tmp_path / "broken.zip"
    path.write_bytes(b"not really a zip")
    info = upload_and_fetch(client, path)

    assert info["status"] == "FAILED"
    assert info["error"] == "not a valid zip archive"
    assert info["feature_count"] == 0


@pytest.mark.parametrize("name", ["data.geojson", "data.shp", "noextension"])
def test_rejects_unsupported_extension(client, tmp_path, name):
    path = tmp_path / name
    path.write_text("{}")
    response = upload(client, path)

    assert response.status_code == 400
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(GeoFile)) == 0


def test_rejects_empty_file(client, tmp_path):
    path = tmp_path / "empty.kml"
    path.touch()
    assert upload(client, path).status_code == 400


def test_rejects_oversized_file(client, sample_kml, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    before = set(settings.upload_dir.iterdir())
    response = upload(client, sample_kml)

    assert response.status_code == 413
    assert set(settings.upload_dir.iterdir()) == before


def test_queue_down_marks_file_failed(client, sample_kml, monkeypatch):
    def broker_down(*args, **kwargs):
        raise OperationalError("connection refused")

    monkeypatch.setattr(process_file, "delay", broker_down)
    response = upload(client, sample_kml)

    assert response.status_code == 503
    with SessionLocal() as db:
        geo_file = db.scalars(select(GeoFile)).one()
    assert geo_file.status == "FAILED"


def test_reprocessing_completed_file_is_a_no_op(client, sample_kml):
    info = upload_and_fetch(client, sample_kml)
    process_file(info["id"])

    with SessionLocal() as db:
        count = db.scalar(select(func.count()).where(Feature.file_id == uuid.UUID(info["id"])))
    assert count == 5


def test_redelivered_task_replaces_partial_features(client, sample_kml):
    info = upload_and_fetch(client, sample_kml)
    with SessionLocal() as db:
        geo_file = db.get(GeoFile, uuid.UUID(info["id"]))
        geo_file.status = "PROCESSING"
        db.commit()

    process_file(info["id"])

    assert client.get(f"/api/files/{info['id']}/").json()["feature_count"] == 5
    with SessionLocal() as db:
        count = db.scalar(select(func.count()).where(Feature.file_id == uuid.UUID(info["id"])))
    assert count == 5


def test_unknown_file_returns_404(client):
    assert client.get(f"/api/files/{uuid.uuid4()}/").status_code == 404


def test_invalid_file_id_returns_422(client):
    assert client.get("/api/files/not-a-uuid/").status_code == 422


def make_file(status, error=None):
    with SessionLocal() as db:
        geo_file = GeoFile(filename="x.kml", file_type="kml", status=status, error=error)
        db.add(geo_file)
        db.commit()
        return str(geo_file.id)


def test_list_files_newest_first(client, sample_kml, make_shapefile_zip, parcels):
    first = upload_and_fetch(client, sample_kml)
    second = upload_and_fetch(client, make_shapefile_zip({"parcels": parcels}), "parcels.zip")

    page = client.get("/api/files/", params={"limit": 1}).json()
    assert page["total"] == 2
    assert [f["id"] for f in page["items"]] == [second["id"]]

    page = client.get("/api/files/", params={"limit": 1, "offset": 1}).json()
    assert [f["id"] for f in page["items"]] == [first["id"]]


def test_file_detail_counts_geometry_types(client, sample_kml):
    info = upload_and_fetch(client, sample_kml)
    assert info["geometry_types"] == {
        "Polygon": 1,
        "MultiPolygon": 1,
        "LineString": 1,
        "Point": 1,
        "GeometryCollection": 1,
    }


def test_measurements_with_summary(client, sample_kml):
    info = upload_and_fetch(client, sample_kml)
    body = client.get(f"/api/files/{info['id']}/measurements/").json()

    assert body["total"] == 5
    assert body["summary"] == {
        "total_area_m2": pytest.approx(3_000_000, rel=1e-6),
        "total_area_ha": pytest.approx(300, rel=1e-6),
        "total_length_m": pytest.approx(1000, rel=1e-6),
        "total_length_km": pytest.approx(1, rel=1e-6),
        "by_status": {"OK": 3, "NOT_APPLICABLE": 1, "UNSUPPORTED": 1},
    }
    first = body["items"][0]
    assert first["feature_id"] == 0
    assert first["layer"] == "Boundaries"
    assert (first["measure_type"], first["unit"], first["projected_crs"]) == (
        "area",
        "m2",
        "EPSG:32643",
    )
    assert first["value"] == pytest.approx(1_000_000, rel=1e-6)


def test_measurements_filter_by_geometry_type(client, sample_kml):
    info = upload_and_fetch(client, sample_kml)
    body = client.get(
        f"/api/files/{info['id']}/measurements/", params={"geometry_type": "linestring"}
    ).json()

    assert [m["geometry_type"] for m in body["items"]] == ["LineString"]
    assert body["summary"]["total_area_m2"] == 0
    assert body["summary"]["total_length_m"] == pytest.approx(1000, rel=1e-6)


def test_measurements_pagination_keeps_whole_file_summary(client, sample_kml):
    info = upload_and_fetch(client, sample_kml)
    body = client.get(
        f"/api/files/{info['id']}/measurements/", params={"limit": 2, "offset": 2}
    ).json()

    assert [m["feature_id"] for m in body["items"]] == [2, 3]
    assert body["total"] == 5
    assert body["summary"]["total_area_m2"] == pytest.approx(3_000_000, rel=1e-6)


@pytest.mark.parametrize("endpoint", ["measurements", "features"])
def test_results_not_ready_return_409(client, endpoint):
    file_id = make_file("PROCESSING")
    response = client.get(f"/api/files/{file_id}/{endpoint}/")

    assert response.status_code == 409
    assert "PROCESSING" in response.json()["detail"]


def test_failed_file_explains_why(client):
    file_id = make_file("FAILED", error="zip does not contain a .shp file")
    response = client.get(f"/api/files/{file_id}/measurements/")

    assert response.status_code == 409
    assert "zip does not contain a .shp file" in response.json()["detail"]


def test_features_are_geojson(client, sample_kml):
    info = upload_and_fetch(client, sample_kml)
    body = client.get(f"/api/files/{info['id']}/features/").json()

    assert body["type"] == "FeatureCollection"
    assert body["total"] == len(body["features"]) == 5
    marker = next(f for f in body["features"] if f["properties"]["attributes"]["Name"] == "marker")
    assert marker["geometry"]["type"] == "Point"
    lon, lat = marker["geometry"]["coordinates"]
    assert 77 < lon < 78 and 12 < lat < 14
    plot = body["features"][0]
    assert plot["properties"]["measurement"]["value"] == pytest.approx(1_000_000, rel=1e-6)
    assert plot["properties"]["source_crs"] == "EPSG:4326"


@pytest.mark.parametrize("endpoint", ["", "measurements/", "features/"])
def test_unknown_file_404_on_every_endpoint(client, endpoint):
    assert client.get(f"/api/files/{uuid.uuid4()}/{endpoint}").status_code == 404


@pytest.mark.parametrize(
    "url", ["/api/files/?limit=0", "/api/files/?limit=101", "/api/files/?offset=-1"]
)
def test_pagination_bounds_are_validated(client, url):
    assert client.get(url).status_code == 422


def test_oversized_upload_rejected_from_header(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    path = tmp_path / "big.kml"
    path.write_bytes(b"x" * 200_000)
    response = upload(client, path)

    assert response.status_code == 413
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(GeoFile)) == 0


def test_unexpected_error_fails_file_without_leaking_details(client, sample_kml, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    def too_slow(path):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr("app.tasks.read_features", too_slow)
    info = upload_and_fetch(client, sample_kml)

    assert info["status"] == "FAILED"
    assert info["error"] == "unexpected error while processing file"


def test_failed_insert_marks_file_failed_not_stuck(client, sample_kml, monkeypatch):
    def broken_row(*args):
        raise RuntimeError("insert failed")

    monkeypatch.setattr("app.tasks._feature_row", broken_row)
    info = upload_and_fetch(client, sample_kml)

    assert info["status"] == "FAILED"
    assert info["error"] == "unexpected error while processing file"
    assert info["feature_count"] == 0


def test_long_layer_name_is_trimmed(client, sample_kml):
    long_name = "x" * 300
    sample_kml.write_text(sample_kml.read_text().replace("Boundaries", long_name))
    info = upload_and_fetch(client, sample_kml)

    assert info["status"] == "COMPLETED"
    body = client.get(f"/api/files/{info['id']}/measurements/").json()
    assert body["items"][0]["layer"] == long_name[:255]
