import json
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj import CRS

WGS84 = CRS.from_epsg(4326)
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
SHAPEFILE_SIDECARS = (".shx", ".dbf")

POLYGONAL = {"Polygon", "MultiPolygon"}
LINEAR = {"LineString", "MultiLineString", "LinearRing"}
PUNTAL = {"Point", "MultiPoint"}


class GeoFileError(ValueError):
    pass


class MeasureStatus(StrEnum):
    OK = "OK"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNSUPPORTED = "UNSUPPORTED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class Measurement:
    status: MeasureStatus
    measure_type: str | None = None
    value: float | None = None
    unit: str | None = None
    projected_crs: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class FeatureResult:
    index: int
    layer: str
    geometry_type: str | None
    geometry: shapely.Geometry | None
    crs: str | None
    properties: dict
    measurement: Measurement


def read_features(path: Path) -> list[FeatureResult]:
    results: list[FeatureResult] = []
    for source in _sources(path):
        for layer in _list_layers(source):
            gdf = _read_layer(source, layer)
            if not gdf.empty:
                results.extend(measure_layer(gdf, layer, start=len(results)))
    return results


def _sources(path: Path) -> list[str]:
    match path.suffix.lower():
        case ".kml":
            return [str(path)]
        case ".zip":
            return [f"/vsizip/{path.as_posix()}/{shp}" for shp in _shapefiles_in(path)]
        case suffix:
            raise GeoFileError(f"unsupported file type '{suffix}'")


def _shapefiles_in(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = [
                info
                for info in archive.infolist()
                if not info.is_dir() and not info.filename.startswith("__MACOSX/")
            ]
    except zipfile.BadZipFile as exc:
        raise GeoFileError("not a valid zip archive") from exc

    if sum(info.file_size for info in entries) > MAX_UNCOMPRESSED_BYTES:
        raise GeoFileError("archive is too large once uncompressed")

    names = {info.filename.lower() for info in entries}
    shapefiles = sorted(
        PurePosixPath(info.filename) for info in entries if info.filename.lower().endswith(".shp")
    )
    if not shapefiles:
        raise GeoFileError("zip does not contain a .shp file")

    for shp in shapefiles:
        missing = [
            ext for ext in SHAPEFILE_SIDECARS if str(shp.with_suffix(ext)).lower() not in names
        ]
        if missing:
            raise GeoFileError(f"{shp.name} is missing {', '.join(missing)}")
    return [str(shp) for shp in shapefiles]


def _list_layers(source: str) -> list[str]:
    try:
        return [name for name, _ in pyogrio.list_layers(source)]
    except DataSourceError as exc:
        raise GeoFileError(f"could not open file: {exc}") from exc


def _read_layer(source: str, layer: str) -> gpd.GeoDataFrame:
    try:
        return gpd.read_file(source, layer=layer, engine="pyogrio")
    except (DataSourceError, DataLayerError) as exc:
        raise GeoFileError(f"could not read layer '{layer}': {exc}") from exc


def measure_layer(gdf: gpd.GeoDataFrame, layer: str = "", start: int = 0) -> list[FeatureResult]:
    notes = pd.Series("", index=gdf.index)
    if gdf.crs is None and _within_lonlat(gdf):
        gdf = gdf.set_crs(WGS84)
        notes += "no CRS in source, assumed EPSG:4326 from coordinate range; "

    geoms = gdf.geometry.force_2d()
    collections = geoms.geom_type == "GeometryCollection"
    geoms[collections] = geoms[collections].apply(_homogenize)

    invalid = geoms.geom_type.isin(POLYGONAL) & ~geoms.is_valid
    geoms[invalid] = geoms[invalid].make_valid().apply(_polygonal)
    notes[invalid] += "invalid geometry repaired before measuring; "

    types = geoms.geom_type
    empty = geoms.isna() | geoms.is_empty
    wgs84 = geoms.to_crs(WGS84) if gdf.crs else None
    if wgs84 is not None:
        zones = _utm_epsg(wgs84)
        values = _measure(wgs84, types[~empty], zones)
    else:
        zones = values = pd.Series(np.nan, index=gdf.index)

    source_crs = _crs_label(gdf.crs)
    properties = _properties(gdf)
    return [
        FeatureResult(
            index=start + i,
            layer=layer,
            geometry_type=types[idx],
            geometry=None if wgs84 is None else wgs84[idx],
            crs=source_crs,
            properties=properties[i],
            measurement=_measurement(
                types[idx],
                empty=empty[idx],
                crs_known=wgs84 is not None,
                value=values[idx],
                epsg=zones[idx],
                note=notes[idx].rstrip("; ") or None,
            ),
        )
        for i, idx in enumerate(gdf.index)
    ]


def _measurement(
    geom_type: str | None,
    *,
    empty: bool,
    crs_known: bool,
    value: float,
    epsg: float,
    note: str | None,
) -> Measurement:
    if geom_type is None or empty:
        return Measurement(MeasureStatus.SKIPPED, note="empty geometry")
    if geom_type in PUNTAL:
        return Measurement(MeasureStatus.NOT_APPLICABLE, note=note)
    if geom_type not in POLYGONAL | LINEAR:
        return Measurement(
            MeasureStatus.UNSUPPORTED, note=f"no measurement defined for {geom_type}"
        )
    if not crs_known:
        return Measurement(MeasureStatus.SKIPPED, note="unknown CRS, cannot project to metres")

    measure_type, unit = ("area", "m2") if geom_type in POLYGONAL else ("length", "m")
    return Measurement(
        MeasureStatus.OK, measure_type, float(value), unit, f"EPSG:{int(epsg)}", note
    )


def _utm_epsg(geoms: gpd.GeoSeries) -> pd.Series:
    # bbox centre rather than centroid: good enough to pick a 6° zone and avoids
    # computing centroids on lon/lat coordinates
    bounds = geoms.bounds
    lon = (bounds.minx + bounds.maxx) / 2
    lat = (bounds.miny + bounds.maxy) / 2
    zone = np.clip(np.floor((lon + 180) / 6), 0, 59) + 1
    return pd.Series(np.where(lat >= 0, 32600, 32700) + zone, index=geoms.index)


def _measure(wgs84: gpd.GeoSeries, types: pd.Series, zones: pd.Series) -> pd.Series:
    values = pd.Series(np.nan, index=wgs84.index)
    measurable = types[types.isin(POLYGONAL | LINEAR)].index
    # one reprojection per UTM zone instead of one per feature
    for epsg, idx in zones[measurable].groupby(zones[measurable]).groups.items():
        projected = wgs84[idx].to_crs(epsg=int(epsg))
        values[idx] = np.where(types[idx].isin(POLYGONAL), projected.area, projected.length)
    return values


def _properties(gdf: gpd.GeoDataFrame) -> list[dict]:
    attributes = gdf.drop(columns=gdf.geometry.name)
    if attributes.columns.empty:
        return [{} for _ in range(len(gdf))]
    # let pandas handle NaN, numpy scalars and datetimes so the result is plain JSON
    return json.loads(attributes.to_json(orient="records", date_format="iso"))


def _within_lonlat(gdf: gpd.GeoDataFrame) -> bool:
    minx, miny, maxx, maxy = gdf.total_bounds
    return -180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90


def _crs_label(crs: CRS | None) -> str | None:
    if crs is None:
        return None
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg else crs.name


def _flat_parts(geom: shapely.Geometry) -> np.ndarray:
    return shapely.get_parts(shapely.get_parts(geom))


def _homogenize(geom: shapely.Geometry) -> shapely.Geometry:
    # a collection whose parts are all one type (e.g. nested KML MultiGeometry) is really a
    # Multi* geometry, so treat it as one instead of rejecting it as unsupported
    parts = _flat_parts(geom)
    kinds = {part.geom_type for part in parts}
    for single, multi in (
        ("Polygon", shapely.MultiPolygon),
        ("LineString", shapely.MultiLineString),
        ("Point", shapely.MultiPoint),
    ):
        if kinds == {single}:
            return multi(list(parts))
    return geom


def _polygonal(geom: shapely.Geometry) -> shapely.Geometry:
    if geom.geom_type in POLYGONAL:
        return geom
    polygons = [part for part in _flat_parts(geom) if part.geom_type == "Polygon"]
    return shapely.MultiPolygon(polygons) if polygons else shapely.Polygon()
