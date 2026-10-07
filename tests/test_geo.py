import zipfile
from datetime import date

import geopandas as gpd
import numpy as np
import pytest
from pyproj import Geod
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box

from app.geo import GeoFileError, MeasureStatus, measure_layer, read_features
from tests.conftest import LINE, POINT, SQUARE, SQUARE_2, UTM_43N, to_wgs84

GEOD = Geod(ellps="WGS84")


def measure_one(geom, crs=4326):
    [result] = measure_layer(gpd.GeoDataFrame(geometry=[geom], crs=crs))
    return result.measurement


def test_polygon_area_is_in_square_metres_not_degrees():
    square = to_wgs84(SQUARE)
    m = measure_one(square)

    assert square.area < 0.001  # what you'd get measuring raw lon/lat
    assert m.status == MeasureStatus.OK
    assert (m.measure_type, m.unit, m.projected_crs) == ("area", "m2", "EPSG:32643")
    assert m.value == pytest.approx(1_000_000, rel=1e-6)


def test_area_and_length_agree_with_geodesic_calculation():
    square, line = to_wgs84(SQUARE), to_wgs84(LINE)
    geodesic_area = abs(GEOD.geometry_area_perimeter(square)[0])

    assert measure_one(square).value == pytest.approx(geodesic_area, rel=0.002)
    assert measure_one(line).value == pytest.approx(GEOD.geometry_length(line), rel=0.002)


def test_line_length():
    m = measure_one(to_wgs84(LINE))
    assert (m.status, m.measure_type, m.unit) == (MeasureStatus.OK, "length", "m")
    assert m.value == pytest.approx(1000, rel=1e-6)


def test_point_has_no_measurement():
    m = measure_one(to_wgs84(POINT))
    assert m.status == MeasureStatus.NOT_APPLICABLE
    assert m.value is None


def test_projected_input_is_not_measured_in_its_own_units():
    web_mercator = gpd.GeoSeries([to_wgs84(SQUARE)], crs=4326).to_crs(3857).iloc[0]

    assert web_mercator.area > 1_040_000  # mercator inflates area ~5% at this latitude
    assert measure_one(web_mercator, crs=3857).value == pytest.approx(1_000_000, rel=1e-6)


@pytest.mark.parametrize(
    ("lon", "lat", "epsg"),
    [(77.5, 12.9, "EPSG:32643"), (83.5, 20.3, "EPSG:32644"), (133.0, -23.0, "EPSG:32753")],
)
def test_each_feature_gets_its_own_utm_zone(lon, lat, epsg):
    assert measure_one(box(lon, lat, lon + 0.01, lat + 0.01)).projected_crs == epsg


def test_features_in_one_file_can_use_different_zones():
    gdf = gpd.GeoDataFrame(
        geometry=[box(77.5, 12.9, 77.51, 12.91), box(83.5, 20.3, 83.51, 20.31)], crs=4326
    )
    zones = [r.measurement.projected_crs for r in measure_layer(gdf)]
    assert zones == ["EPSG:32643", "EPSG:32644"]


def test_mixed_geometry_collection_is_unsupported():
    mixed = GeometryCollection([to_wgs84(POINT), to_wgs84(LINE)])
    m = measure_one(mixed)
    assert m.status == MeasureStatus.UNSUPPORTED
    assert "GeometryCollection" in m.note


def test_collection_of_polygons_is_measured_as_multipolygon():
    [result] = measure_layer(
        gpd.GeoDataFrame(
            geometry=[GeometryCollection([to_wgs84(SQUARE), to_wgs84(SQUARE_2)])], crs=4326
        )
    )
    assert result.geometry_type == "MultiPolygon"
    assert result.measurement.value == pytest.approx(2_000_000, rel=1e-6)


@pytest.mark.parametrize("geom", [None, Polygon()])
def test_missing_or_empty_geometry_is_skipped(geom):
    m = measure_one(geom)
    assert m.status == MeasureStatus.SKIPPED
    assert m.note == "empty geometry"


def test_self_intersecting_polygon_is_repaired():
    bowtie = Polygon([(77.5, 12.9), (77.51, 12.91), (77.51, 12.9), (77.5, 12.91)])
    m = measure_one(bowtie)
    assert m.status == MeasureStatus.OK
    assert m.value > 0
    assert "repaired" in m.note


def test_missing_crs_with_lonlat_coordinates_assumes_wgs84():
    [result] = measure_layer(gpd.GeoDataFrame(geometry=[to_wgs84(SQUARE)]))
    assert result.crs == "EPSG:4326"
    assert result.measurement.value == pytest.approx(1_000_000, rel=1e-6)
    assert "assumed EPSG:4326" in result.measurement.note


def test_missing_crs_with_projected_coordinates_is_skipped():
    [result] = measure_layer(gpd.GeoDataFrame(geometry=[SQUARE]))
    assert result.crs is None
    assert result.geometry is None
    assert result.measurement.status == MeasureStatus.SKIPPED
    assert "unknown CRS" in result.measurement.note


def test_properties_are_json_safe():
    gdf = gpd.GeoDataFrame(
        {"name": ["a"], "surveyed": [date(2026, 1, 5)], "height": [np.nan], "count": [np.int64(3)]},
        geometry=[to_wgs84(SQUARE)],
        crs=4326,
    )
    [result] = measure_layer(gdf)
    assert result.properties == {
        "name": "a",
        "surveyed": "2026-01-05T00:00:00.000",
        "height": None,
        "count": 3,
    }


def test_geometry_is_returned_in_wgs84_and_2d():
    [result] = measure_layer(gpd.GeoDataFrame(geometry=[SQUARE], crs=UTM_43N))
    assert not result.geometry.has_z
    assert result.geometry.equals_exact(to_wgs84(SQUARE), tolerance=1e-9)


def test_kml_with_folders_and_multigeometry(sample_kml):
    results = read_features(sample_kml)
    by_name = {r.properties["Name"]: r for r in results}

    assert [r.index for r in results] == list(range(5))
    assert {r.layer for r in results} == {"Boundaries", "Survey"}
    assert {r.crs for r in results} == {"EPSG:4326"}
    assert by_name["plot-a"].measurement.value == pytest.approx(1_000_000, rel=1e-6)
    assert by_name["plot-b"].geometry_type == "MultiPolygon"
    assert by_name["plot-b"].measurement.value == pytest.approx(2_000_000, rel=1e-6)
    assert by_name["road"].measurement.value == pytest.approx(1000, rel=1e-6)
    assert by_name["marker"].measurement.status == MeasureStatus.NOT_APPLICABLE
    assert by_name["mixed"].measurement.status == MeasureStatus.UNSUPPORTED


def test_zipped_shapefile_in_subfolder(make_shapefile_zip, parcels):
    path = make_shapefile_zip({"parcels": parcels}, folder="survey/2026/", junk=True)
    results = read_features(path)

    assert [r.properties["name"] for r in results] == ["a", "b"]
    assert {r.crs for r in results} == {UTM_43N}
    assert [r.measurement.value for r in results] == pytest.approx([1_000_000] * 2, rel=1e-6)


def test_zip_with_several_shapefiles(make_shapefile_zip, parcels):
    roads = gpd.GeoDataFrame({"name": ["r1"]}, geometry=[LINE], crs=UTM_43N)
    results = read_features(make_shapefile_zip({"parcels": parcels, "roads": roads}))

    assert [r.layer for r in results] == ["parcels", "parcels", "roads"]
    assert [r.index for r in results] == [0, 1, 2]


def test_shapefile_without_prj(make_shapefile_zip):
    gdf = gpd.GeoDataFrame({"name": ["a"]}, geometry=[to_wgs84(SQUARE)], crs=4326)
    [result] = read_features(make_shapefile_zip({"parcels": gdf}, drop=(".prj",)))
    assert "assumed EPSG:4326" in result.measurement.note


@pytest.mark.parametrize("sidecar", [".dbf", ".shx"])
def test_zip_missing_sidecar_file(make_shapefile_zip, parcels, sidecar):
    with pytest.raises(GeoFileError, match=sidecar):
        read_features(make_shapefile_zip({"parcels": parcels}, drop=(sidecar,)))


def test_zip_without_shapefile(tmp_path):
    path = tmp_path / "notes.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "hello")
    with pytest.raises(GeoFileError, match=r"\.shp"):
        read_features(path)


def test_corrupt_zip(tmp_path):
    path = tmp_path / "broken.zip"
    path.write_bytes(b"definitely not a zip")
    with pytest.raises(GeoFileError, match="not a valid zip"):
        read_features(path)


def test_zip_bomb_guard(make_shapefile_zip, parcels, monkeypatch):
    monkeypatch.setattr("app.geo.MAX_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(GeoFileError, match="too large"):
        read_features(make_shapefile_zip({"parcels": parcels}))


def test_unsupported_file_type(tmp_path):
    path = tmp_path / "data.geojson"
    path.write_text("{}")
    with pytest.raises(GeoFileError, match="unsupported file type"):
        read_features(path)


def test_malformed_kml(tmp_path):
    path = tmp_path / "bad.kml"
    path.write_text("<kml><Document><Placemark>")
    with pytest.raises(GeoFileError):
        read_features(path)


def test_multipolygon_area_is_sum_of_parts():
    multi = MultiPolygon([to_wgs84(SQUARE), to_wgs84(SQUARE_2)])
    assert measure_one(multi).value == pytest.approx(2_000_000, rel=1e-6)
