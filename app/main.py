from collections.abc import Callable

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

from app.api import router
from app.config import settings
from app.db import DbSession

# room for multipart boundaries and headers on top of the file itself
MULTIPART_OVERHEAD = 64 * 1024

app = FastAPI(title="Geo Measure API", version="0.1.0")
app.include_router(router)
redis_client = Redis.from_url(settings.redis_url, socket_connect_timeout=1)


@app.middleware("http")
async def reject_oversized_uploads(request: Request, call_next):
    # fail fast from the header, before the body is spooled; the chunked check in the
    # upload handler still catches clients that don't send Content-Length
    length = request.headers.get("content-length", "")
    limit = settings.max_upload_mb * 1024 * 1024 + MULTIPART_OVERHEAD
    if request.method == "POST" and length.isdigit() and int(length) > limit:
        return JSONResponse(
            {"detail": f"File is larger than {settings.max_upload_mb} MB"},
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    return await call_next(request)


def _status(check: Callable[[], object]) -> str:
    try:
        check()
    except Exception:
        return "down"
    return "ok"


@app.get("/health")
def health(response: Response, db: DbSession) -> dict[str, str]:
    checks = {
        "db": _status(lambda: db.execute(text("select 1"))),
        "redis": _status(redis_client.ping),
    }
    if "down" in checks.values():
        response.status_code = 503
    return checks
