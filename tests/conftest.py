import os
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import geopandas as gpd
import pytest
import shapely
from shapely.geometry import LineString, Point, box

# must run before anything imports app.config, which reads these once
_base_url = os.environ.get("DATABASE_URL", "postgresql+psycopg://geo:geo@localhost:5432/geo")
os.environ["DATABASE_URL"] = _base_url.rsplit("/", 1)[0] + "/geo_test"
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="geo-uploads-")

UTM_43N = "EPSG:32643"

# built in UTM metres near Bengaluru so the expected measurements are known exactly
SQUARE = box(780_000, 1_430_000, 781_000, 1_431_000)
SQUARE_2 = box(782_000, 1_430_000, 783_000, 1_431_000)
LINE = LineString([(780_000, 1_432_000), (781_000, 1_432_000)])
POINT = Point(780_500, 1_433_000)


def to_wgs84(geom: shapely.Geometry) -> shapely.Geometry:
    return gpd.GeoSeries([geom], crs=UTM_43N).to_crs(4326).iloc[0]


def _kml_coords(geom: shapely.Geometry) -> str:
    coords = geom.exterior.coords if geom.geom_type == "Polygon" else geom.coords
    return " ".join(f"{x:.10f},{y:.10f},0" for x, y in coords)


def _kml_polygon(geom: shapely.Geometry) -> str:
    return (
        "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
        f"{_kml_coords(geom)}"
        "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
    )


def _placemark(name: str, body: str) -> str:
    return f"<Placemark><name>{name}</name>{body}</Placemark>"


@pytest.fixture
def sample_kml(tmp_path: Path) -> Path:
    square, square_2, line, point = map(to_wgs84, (SQUARE, SQUARE_2, LINE, POINT))
    line_kml = f"<LineString><coordinates>{_kml_coords(line)}</coordinates></LineString>"
    point_kml = f"<Point><coordinates>{_kml_coords(point)}</coordinates></Point>"
    boundaries = _placemark("plot-a", _kml_polygon(square)) + _placemark(
        "plot-b", f"<MultiGeometry>{_kml_polygon(square)}{_kml_polygon(square_2)}</MultiGeometry>"
    )
    survey = (
        _placemark("road", line_kml)
        + _placemark("marker", point_kml)
        + _placemark("mixed", f"<MultiGeometry>{point_kml}{line_kml}</MultiGeometry>")
    )
    path = tmp_path / "survey.kml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<Folder><name>Boundaries</name>{boundaries}</Folder>"
        f"<Folder><name>Survey</name>{survey}</Folder>"
        "</Document></kml>"
    )
    return path


MakeZip = Callable[..., Path]


@pytest.fixture
def make_shapefile_zip(tmp_path: Path) -> MakeZip:
    def make(
        layers: dict[str, gpd.GeoDataFrame],
        *,
        folder: str = "",
        drop: tuple[str, ...] = (),
        junk: bool = False,
    ) -> Path:
        zip_path = tmp_path / "upload.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            for name, gdf in layers.items():
                out = tmp_path / name
                out.mkdir()
                gdf.to_file(out / f"{name}.shp")
                for file in out.iterdir():
                    if file.suffix not in drop:
                        archive.write(file, f"{folder}{file.name}")
            if junk:
                archive.writestr("__MACOSX/._parcels.shp", b"\x00\x05\x16\x07")
        return zip_path

    return make


@pytest.fixture
def parcels() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": ["a", "b"]}, geometry=[SQUARE, SQUARE_2], crs=UTM_43N)


@pytest.fixture(scope="session")
def database() -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from app.config import settings

    admin = create_engine(
        settings.database_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = 'geo_test'")):
            conn.execute(text("CREATE DATABASE geo_test"))
    admin.dispose()

    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def client(database) -> Iterator:
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from app.db import engine
    from app.main import app
    from app.tasks import celery

    celery.conf.task_always_eager = True
    with TestClient(app) as test_client:
        yield test_client
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE geo_files CASCADE"))
