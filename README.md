# Geo Measure API

Upload a KML or a zipped Shapefile, get back the area of every polygon and the length of every line, in metres.

- Built with FastAPI, PostgreSQL + PostGIS, Celery + Redis
- Files are processed in the background, so uploads return immediately
- Measurements are done in a projected CRS (UTM), never on raw lat/long degrees
- Results can be fetched as JSON or as GeoJSON

## Contents

- [Setup](#setup)
- [API](#api)
- [Architecture](#architecture)
- [CRS handling](#crs-handling)
- [Design decisions](#design-decisions)
- [Tests](#tests)
- [Learnings](#learnings)
- [Future scope](#future-scope)

## Setup

You only need Docker (with Compose v2).

```bash
git clone https://github.com/chinmai-sd-123/aereo-geo-measure.git
cd aereo-geo-measure
docker compose up --build
```

- API runs on http://localhost:8000
- Swagger docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health
- First build takes a few minutes (GDAL/PROJ wheels are big), after that it's cached

What gets started:

- `db`: Postgres 16 with PostGIS
- `redis`: task queue
- `migrate`: runs `alembic upgrade head` once and exits
- `api`: FastAPI app
- `worker`: Celery worker that processes uploaded files

Settings come from environment variables (see `.env.example`):

- `DATABASE_URL`, default `postgresql+psycopg://geo:geo@localhost:5432/geo`
- `REDIS_URL`, default `redis://localhost:6379/0`
- `UPLOAD_DIR`, default `uploads`
- `MAX_UPLOAD_MB`, default `50`

## API

There are two sample files in `samples/` you can try straight away:

- `survey.kml`: two folders with a polygon, a multipolygon, a line, a point and a mixed collection
- `parcels_utm43n.zip`: shapefile in UTM 43N with a `parcels` (polygon) and a `roads` (line) layer

### Endpoints

| Method | Path | What it does |
|---|---|---|
| POST | `/api/files/` | Upload a file, returns `202` with the file id |
| GET | `/api/files/` | List uploaded files, newest first |
| GET | `/api/files/{id}/` | File info and processing status |
| GET | `/api/files/{id}/measurements/` | Measurement for every feature, plus totals |
| GET | `/api/files/{id}/features/` | Features as a GeoJSON FeatureCollection |
| GET | `/health` | Checks the database and Redis |

### Upload

```bash
curl -F "file=@samples/survey.kml" http://localhost:8000/api/files/
```

```json
{
  "id": "fb2c9c67-0c42-4b01-9361-385024c3c977",
  "filename": "survey.kml",
  "file_type": "kml",
  "status": "PENDING",
  "feature_count": 0,
  "crs": null,
  "error": null,
  "created_at": "2026-10-07T10:00:23.819747Z",
  "processed_at": null
}
```

- Only `.kml` and `.zip` are accepted, anything else is a `400`
- Empty file is a `400`, file over `MAX_UPLOAD_MB` is a `413`
- If Redis is down the file is marked `FAILED` and you get a `503`
- Status starts as `PENDING` because processing happens in the worker

### File info

```bash
curl http://localhost:8000/api/files/{id}/
```

```json
{
  "id": "fb2c9c67-0c42-4b01-9361-385024c3c977",
  "filename": "survey.kml",
  "file_type": "kml",
  "status": "COMPLETED",
  "feature_count": 5,
  "crs": "EPSG:4326",
  "error": null,
  "created_at": "2026-10-07T10:00:23.819747Z",
  "processed_at": "2026-10-07T10:00:24.341854Z",
  "geometry_types": {
    "GeometryCollection": 1,
    "LineString": 1,
    "MultiPolygon": 1,
    "Point": 1,
    "Polygon": 1
  }
}
```

- `status` goes `PENDING` → `PROCESSING` → `COMPLETED` or `FAILED`
- Poll this until it's `COMPLETED`
- If it fails, `error` says why, e.g. `"parcels.shp is missing .dbf"`

### Measurements

```bash
curl "http://localhost:8000/api/files/{id}/measurements/?geometry_type=polygon&limit=1"
```

```json
{
  "file_id": "fb2c9c67-0c42-4b01-9361-385024c3c977",
  "summary": {
    "total_area_m2": 1000000.004,
    "total_area_ha": 100.0,
    "total_length_m": 0.0,
    "total_length_km": 0.0,
    "by_status": { "OK": 1 }
  },
  "items": [
    {
      "feature_id": 0,
      "layer": "Boundaries",
      "geometry_type": "Polygon",
      "status": "OK",
      "measure_type": "area",
      "value": 1000000.004,
      "unit": "m2",
      "projected_crs": "EPSG:32643",
      "note": null
    }
  ],
  "total": 1,
  "limit": 1,
  "offset": 0
}
```

- Query params: `geometry_type` (case-insensitive), `limit` (max 1000), `offset`
- `summary` is for all matching features, not just the current page
- Returns `409` if the file isn't processed yet or failed

Each feature gets a status:

- `OK`: polygon has `area` in `m2`, line has `length` in `m`
- `NOT_APPLICABLE`: points, nothing to measure
- `UNSUPPORTED`: e.g. a collection that mixes points and lines
- `SKIPPED`: empty geometry, or the CRS is unknown so metres can't be worked out

`note` explains anything unusual, like a polygon that had to be repaired or a CRS that was assumed.

### Features (GeoJSON)

```bash
curl "http://localhost:8000/api/files/{id}/features/?limit=1000"
```

```json
{
  "type": "Feature",
  "id": 0,
  "geometry": { "type": "Polygon", "coordinates": [[[77.5896628, 12.9226249], "..."]] },
  "properties": {
    "layer": "Boundaries",
    "geometry_type": "Polygon",
    "source_crs": "EPSG:4326",
    "measurement": { "status": "OK", "type": "area", "value": 1000000.004, "unit": "m2" },
    "attributes": { "Name": "plot-a", "Description": "" }
  }
}
```

- Geometry is always EPSG:4326, so it can go straight into Leaflet, Mapbox or QGIS
- `attributes` are the original properties from the file

## Architecture

![System architecture](docs/architecture.png)

Editable diagrams are on [Eraser](https://app.eraser.io/workspace/QD60b8b9xnKhX3VfpMay).

### Project structure

```
app/
  main.py      app setup, upload size middleware, /health
  api.py       /api/files endpoints
  schemas.py   response models
  models.py    GeoFile and Feature tables
  geo.py       reading files, CRS handling, measuring
  tasks.py     Celery app and the process_file task
  db.py        engine and session
  config.py    settings
migrations/    Alembic migrations
tests/         pytest
samples/       sample KML and shapefile
docs/          diagrams
```

- `geo.py` has no web or database code, it just takes a file (or GeoDataFrame) and returns results
- That keeps the GIS logic easy to test on its own
- The worker only saves what `geo.py` returns

### File processing flow

![Upload and processing flow](docs/upload-flow.png)

1. API checks the extension and creates a `geo_files` row with status `PENDING`
2. File is streamed to disk in 1 MB chunks as `<uuid>.kml` or `<uuid>.zip`
3. Row is committed, then `process_file(id)` is put on the queue, then API returns `202`
4. Worker sets status to `PROCESSING` and reads the file:
   - KML: each `<Folder>` is read as a layer
   - ZIP: read directly with GDAL's `/vsizip/`, nothing is extracted
   - ZIP is checked first: valid archive, under 500 MB uncompressed, every `.shp` has its `.shx` and `.dbf`
   - shapefiles inside subfolders work, `__MACOSX` junk is ignored
5. Geometries are cleaned up:
   - Z values are dropped
   - a `GeometryCollection` with only one kind of geometry becomes a `Multi*`
   - invalid (self-intersecting) polygons are fixed with `make_valid`
6. Features are measured (see below)
7. All features are saved in one bulk insert, then status becomes `COMPLETED`
8. If the file can't be read at all, status becomes `FAILED` with the reason

One bad feature doesn't fail the file. It just gets its own status.

### Measurement flow

For each layer:

1. Reproject everything to WGS84 (EPSG:4326)
2. Find each feature's UTM zone from the centre of its bounding box
   - zone = `floor((lon + 180) / 6) + 1`
   - EPSG `326xx` in the northern hemisphere, `327xx` in the southern
3. Group features by zone and reproject each group once
4. Polygons → `.area` in m², lines → `.length` in m
5. Points and mixed collections get a status instead of a value

Speed: 20,000 polygons spread across 6 UTM zones take about 0.6 s.

## CRS handling

Why not measure in lat/long directly:

- Degrees are angles, not distances
- 1° of longitude is about 111 km at the equator, 108 km at Bengaluru and 0 at the poles
- So `polygon.area` on EPSG:4326 gives "square degrees", a 1 km² plot comes out as about 0.00008

What the service does with each kind of input:

| Input | What happens |
|---|---|
| Geographic CRS (e.g. EPSG:4326) | Projected to the feature's own UTM zone |
| Already projected (UTM, Web Mercator, etc.) | Also goes WGS84 → UTM, so every file is measured the same way |
| KML | Always EPSG:4326 (that's in the KML spec) |
| Shapefile with no `.prj`, coordinates look like lat/long | Assumed EPSG:4326, a note is added to each feature |
| Shapefile with no `.prj`, other coordinates | Measurement `SKIPPED`, because guessing would give wrong numbers |

- Even projected files go through UTM because some projections are bad for measuring
- Example: Web Mercator (EPSG:3857) inflates area by about 5% at Bengaluru, and much more further north
- UTM is accurate to roughly 0.1% inside its zone
- Tests compare every result with `pyproj.Geod` (geodesic, measured on the ellipsoid) and require them to agree within 0.2%

## Design decisions

**UTM zone per feature, not one CRS for the whole file**

- A road or pipeline file can cross zones, one zone for everything distorts features far from it
- Considered a single equal-area CRS (e.g. EPSG:6933): fine for area, but distorts length
- Considered geodesic measurement with `pyproj.Geod`: most accurate and needs no projection
- Went with UTM because it's the usual GIS approach, easy to explain and check, and it's vectorised in geopandas
- Geodesic is used in the tests as the reference, and is the first thing I'd add as an option

**Background processing with Celery + Redis**

- Reading and measuring a big shapefile can take seconds, that shouldn't block the request
- API returns `202` straight away and the client polls for the status
- Considered FastAPI `BackgroundTasks`: simpler, but tasks are lost on restart and run inside the API process
- Things I made sure of:
  - row is committed before the task is queued, so the worker never looks for a row that isn't there yet
  - `acks_late` is on, so if a worker dies mid-file the task is delivered again
  - the task is safe to run twice: old features are deleted first, finished files are skipped
  - soft time limit of 300 s, so a bad file ends up `FAILED` instead of blocking a worker
  - if Redis is down on upload, the file is marked `FAILED` instead of sitting in `PENDING` forever

**PostGIS for storage**

- Geometry stored as `geometry(Geometry, 4326)` with a GiST index, ready for spatial queries later
- `ST_AsGeoJSON` builds the GeoJSON response in the database
- Original CRS is kept on each feature
- Attributes are different for every file, so they go in a `JSONB` column

**Reading zips in place**

- GDAL reads shapefiles straight from the zip through `/vsizip/`
- Nothing is extracted, so there's no zip-slip risk and no temp folder to clean up

**Smaller choices**

- Statuses are `varchar` + `CHECK` instead of Postgres enums, so adding one later is an easy migration
- Measurement totals are calculated in SQL (`SUM ... FILTER`, `GROUP BY`), not in Python
- Measurement endpoint only selects the columns it needs, no geometry or attributes
- Upload size is checked twice: middleware rejects big requests from `Content-Length` before reading the body, and the chunked writer catches clients that don't send it
- Uploaded files are saved under a generated uuid, the user's filename is never used as a path
- Errors use FastAPI's normal `{"detail": "..."}` format

## Tests

```bash
docker compose exec api pytest
```

64 tests in two files.

`tests/test_geo.py` (no database):

- area in m² vs raw degrees
- results match geodesic area and length
- Web Mercator input measured correctly
- correct UTM zone, including southern hemisphere and files spanning several zones
- points, mixed collections, single-type collections, empty and self-intersecting geometries
- shapefile without `.prj`
- KML folders and MultiGeometry
- shapefile in a subfolder, several shapefiles in one zip
- missing `.dbf` / `.shx`, corrupt zip, zip bomb, malformed KML

`tests/test_api.py`:

- runs against a separate `geo_test` database built with the real Alembic migration
- Celery runs in eager mode
- covers upload, status changes, failures (including a failed database insert), Redis being down, task redelivery, filtering, pagination, totals, GeoJSON and upload limits

Test data is built in code from shapes with known sizes (a 1 km × 1 km square in UTM 43N), so the expected answers are exact and there are no binary fixtures.

Lint and format check:

```bash
docker compose exec api ruff check .
docker compose exec api ruff format --check .
```

## Learnings

- Latitude and longitude are angles, not distances, so you can't measure area or length with them directly
- You have to convert to a projected CRS in metres first, and UTM is a good, simple choice for that
- Not every projected CRS is good for measuring. Web Mercator looks fine on a map but makes areas about 5% too big in Bengaluru
- A shapefile is really 4+ files, and without the `.prj` file you don't know what the numbers mean
- GDAL can read a shapefile straight out of a zip, so I didn't need to extract anything
- Real files are messy: KML has height values, Mac zips have `__MACOSX` folders, and hand-drawn polygons sometimes cross themselves
- With background jobs, the order of steps matters: save to the database first, then send the job
- A job can run twice (for example after a crash), so it has to be safe to repeat
- Testing with a shape I already knew the answer for (a 1 km × 1 km square) made mistakes easy to spot
- Checking the result with a second method (geodesic) gave me confidence the numbers are right

## Future scope

- `?method=geodesic` option using `pyproj.Geod` for very large features
- Handle polar areas (UTM stops at 84°N / 80°S) and features crossing the 180° line
- Support KMZ, GeoJSON and GeoPackage, GDAL already reads them so it's a small change in `geo.py`
- Perimeter for polygons, totals per layer
- Store uploads in S3/GCS instead of a local volume
- Auth and per-user files, plus `DELETE /api/files/{id}/`
- Keyset pagination on `(file_id, index)` for files with millions of features
- Vector tiles (`ST_AsMVT`) to show very large files on a map
- Progress updates while a big file is processing
