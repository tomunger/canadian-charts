import asyncio
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

log = logging.getLogger("chs_proxy")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

CHS_EXPORT = os.environ.get(
    "CHS_EXPORT_URL",
    "https://egisp.dfo-mpo.gc.ca/arcgis/rest/services/chs"
    "/ENC_MaritimeChartService/MapServer/exts/MaritimeChartService"
    "/MapServer/export",
)

# Layers 0-7 are the visible chart content. 8 (data quality hatching)
# and 9-12 (overscale, low accuracy, etc.) are off by default in the
# service definition, so we skip them.
LAYERS = os.environ.get("CHS_LAYERS", "show:0,1,2,3,4,5,6,7")

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/var/cache/chs_tiles"))
TILE_SIZE = int(os.environ.get("TILE_SIZE", "256"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "30"))
MAX_ZOOM = int(os.environ.get("MAX_ZOOM", "17"))

# Cache TTL in minutes. Tiles older than this are treated as a miss and
# re-fetched from upstream. Default is 14 days; set to 0 (or negative) to
# disable expiry and keep cached tiles forever.
CACHE_TTL_MINUTES = int(os.environ.get("CACHE_TTL_MINUTES", str(14 * 24 * 60)))
CACHE_TTL_SECONDS = CACHE_TTL_MINUTES * 60

# Cache size cap. When the on-disk cache exceeds this many bytes the
# eviction task removes oldest-mtime files until usage is back under the
# target (CACHE_MAX_BYTES * CACHE_EVICTION_TARGET_RATIO). Set the cap to
# 0 to disable eviction.
CACHE_MAX_BYTES = int(os.environ.get("CACHE_MAX_BYTES", str(20 * 1024**3)))  # 20 GiB
CACHE_EVICTION_INTERVAL_SECONDS = int(
    os.environ.get("CACHE_EVICTION_INTERVAL_SECONDS", "3600")
)
CACHE_EVICTION_TARGET_RATIO = 0.9

# Canadian-waters bounding box (lon_min, lat_min, lon_max, lat_max).
# Generous default covering all CHS coverage including offshore EEZ.
ALLOW_LON_MIN = float(os.environ.get("ALLOW_LON_MIN", "-145.0"))
ALLOW_LAT_MIN = float(os.environ.get("ALLOW_LAT_MIN", "40.0"))
ALLOW_LON_MAX = float(os.environ.get("ALLOW_LON_MAX", "-50.0"))
ALLOW_LAT_MAX = float(os.environ.get("ALLOW_LAT_MAX", "85.0"))

# Rate limit applied per client IP. With --proxy-headers, the client IP
# is sourced from X-Forwarded-For.
RATE_LIMIT = os.environ.get("RATE_LIMIT", "60/minute")

# Cap on concurrent outbound CHS requests from this process, so we never
# hammer upstream regardless of how many clients are connected.
UPSTREAM_CONCURRENCY = int(os.environ.get("UPSTREAM_CONCURRENCY", "8"))

# Maximum bytes accepted in an upstream response body. Real chart tiles
# are well under 100 KB; 2 MiB is a generous safety cap.
MAX_TILE_BYTES = int(os.environ.get("MAX_TILE_BYTES", str(2 * 1024 * 1024)))

# Contact string embedded in the outbound User-Agent so CHS operators can
# reach the operator instead of silently blocking. Recommended: an email
# or URL.
CONTACT = os.environ.get("CONTACT", "")
USER_AGENT = (
    f"chs-tile-proxy/0.1 (+{CONTACT})" if CONTACT else "chs-tile-proxy/0.1"
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Web Mercator world half-extent in metres.
HALF = 20037508.342789244

# Coalesce concurrent requests for the same tile so we only hit upstream once.
_inflight: dict[tuple[int, int, int], asyncio.Task[bytes]] = {}
_inflight_lock = asyncio.Lock()
_upstream_semaphore = asyncio.Semaphore(UPSTREAM_CONCURRENCY)


def tile_to_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    tile = HALF * 2 / n
    xmin = -HALF + x * tile
    xmax = xmin + tile
    ymax = HALF - y * tile
    ymin = ymax - tile
    return xmin, ymin, xmax, ymax


def _mercator_y_to_lat(y_m: float) -> float:
    return math.degrees(math.atan(math.sinh(math.pi * y_m / HALF)))


def tile_in_allowed_bbox(z: int, x: int, y: int) -> bool:
    """True if the tile envelope intersects the Canadian-waters allowlist."""
    xmin_m, ymin_m, xmax_m, ymax_m = tile_to_bbox(z, x, y)
    lon_min = xmin_m / HALF * 180.0
    lon_max = xmax_m / HALF * 180.0
    lat_min = _mercator_y_to_lat(ymin_m)
    lat_max = _mercator_y_to_lat(ymax_m)
    if lon_max < ALLOW_LON_MIN or lon_min > ALLOW_LON_MAX:
        return False
    if lat_max < ALLOW_LAT_MIN or lat_min > ALLOW_LAT_MAX:
        return False
    return True


def cache_path(z: int, x: int, y: int) -> Path:
    # Shard by z/x to keep any one directory from getting huge.
    return CACHE_DIR / str(z) / str(x) / f"{y}.png"


def cache_is_fresh(path: Path) -> bool:
    """True if the cached tile exists and is within the TTL window."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return False
    if CACHE_TTL_SECONDS <= 0:
        return True
    return (time.time() - mtime) < CACHE_TTL_SECONDS


async def fetch_upstream(client: httpx.AsyncClient, z: int, x: int, y: int) -> bytes:
    xmin, ymin, xmax, ymax = tile_to_bbox(z, x, y)
    params = {
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": "3857",
        "size": f"{TILE_SIZE},{TILE_SIZE}",
        "imageSR": "3857",
        "format": "png",
        "f": "image",
        "layers": LAYERS,
        "transparent": "true",
    }
    async with _upstream_semaphore:
        async with client.stream("GET", CHS_EXPORT, params=params) as r:
            if r.status_code != 200:
                log.warning("upstream %s for z=%s x=%s y=%s", r.status_code, z, x, y)
                raise HTTPException(status_code=502, detail="upstream error")

            ctype = r.headers.get("content-type", "")
            if not ctype.lower().startswith("image/"):
                log.warning(
                    "upstream non-image content-type %r for z=%s x=%s y=%s",
                    ctype, z, x, y,
                )
                raise HTTPException(status_code=502, detail="upstream not an image")

            declared = r.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > MAX_TILE_BYTES:
                        raise HTTPException(
                            status_code=502, detail="upstream payload too large"
                        )
                except ValueError:
                    pass

            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > MAX_TILE_BYTES:
                    raise HTTPException(
                        status_code=502, detail="upstream payload too large"
                    )
                chunks.append(chunk)
            data = b"".join(chunks)

    if not data.startswith(PNG_MAGIC):
        log.warning("upstream returned non-PNG bytes for z=%s x=%s y=%s", z, x, y)
        raise HTTPException(status_code=502, detail="upstream not a PNG")
    return data


def write_cache_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def evict_cache_once() -> None:
    """Walk the cache directory and prune oldest files if over the cap."""
    if CACHE_MAX_BYTES <= 0 or not CACHE_DIR.exists():
        return

    entries: list[tuple[float, int, Path]] = []
    total = 0
    for root, _dirs, files in os.walk(CACHE_DIR):
        for fname in files:
            p = Path(root) / fname
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
            total += st.st_size

    if total <= CACHE_MAX_BYTES:
        return

    target = int(CACHE_MAX_BYTES * CACHE_EVICTION_TARGET_RATIO)
    entries.sort(key=lambda e: e[0])  # oldest first
    freed = 0
    removed = 0
    for _mtime, size, p in entries:
        if total - freed <= target:
            break
        try:
            p.unlink()
            freed += size
            removed += 1
        except FileNotFoundError:
            pass
    log.info(
        "cache eviction: removed %d files, freed %d bytes (was %d, target %d)",
        removed, freed, total, target,
    )


async def cache_eviction_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(evict_cache_once)
        except Exception:
            log.exception("cache eviction failed")
        await asyncio.sleep(CACHE_EVICTION_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    app.state.client = httpx.AsyncClient(
        timeout=UPSTREAM_TIMEOUT,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=False,
    )
    evictor = asyncio.create_task(cache_eviction_loop())
    try:
        yield
    finally:
        evictor.cancel()
        try:
            await evictor
        except (asyncio.CancelledError, Exception):
            pass
        await app.state.client.aclose()


limiter = Limiter(key_func=get_remote_address, default_limits=[RATE_LIMIT])
app = FastAPI(lifespan=lifespan, title="CHS Tile Proxy")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/tiles/{z}/{x}/{y}.png")
@limiter.limit(RATE_LIMIT)
async def serve_tile(request: Request, z: int, x: int, y: int) -> Response:
    if z < 0 or z > MAX_ZOOM:
        raise HTTPException(status_code=400, detail="zoom out of range")
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        raise HTTPException(status_code=400, detail="tile out of range")
    if not tile_in_allowed_bbox(z, x, y):
        raise HTTPException(status_code=400, detail="tile outside allowed bbox")

    path = cache_path(z, x, y)
    if cache_is_fresh(path):
        return Response(
            content=path.read_bytes(),
            media_type="image/png",
            headers={"X-Cache": "HIT"},
        )

    key = (z, x, y)
    async with _inflight_lock:
        task = _inflight.get(key)
        if task is None:
            task = asyncio.create_task(fetch_upstream(app.state.client, z, x, y))
            _inflight[key] = task
            created = True
        else:
            created = False

    try:
        data = await task
    finally:
        if created:
            async with _inflight_lock:
                _inflight.pop(key, None)

    if created:
        write_cache_atomic(path, data)
    return Response(
        content=data,
        media_type="image/png",
        headers={"X-Cache": "MISS"},
    )
