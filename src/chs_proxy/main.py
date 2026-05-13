import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response

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
MAX_ZOOM = int(os.environ.get("MAX_ZOOM", "19"))

# Web Mercator world half-extent in metres.
HALF = 20037508.342789244

# Coalesce concurrent requests for the same tile so we only hit upstream once.
_inflight: dict[tuple[int, int, int], asyncio.Task[bytes]] = {}
_inflight_lock = asyncio.Lock()


def tile_to_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    tile = HALF * 2 / n
    xmin = -HALF + x * tile
    xmax = xmin + tile
    ymax = HALF - y * tile
    ymin = ymax - tile
    return xmin, ymin, xmax, ymax


def cache_path(z: int, x: int, y: int) -> Path:
    # Shard by z/x to keep any one directory from getting huge.
    return CACHE_DIR / str(z) / str(x) / f"{y}.png"


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
    r = await client.get(CHS_EXPORT, params=params)
    if r.status_code != 200:
        log.warning("upstream %s for z=%s x=%s y=%s", r.status_code, z, x, y)
        raise HTTPException(status_code=502, detail="upstream error")
    return r.content


def write_cache_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    app.state.client = httpx.AsyncClient(
        timeout=UPSTREAM_TIMEOUT,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        headers={"User-Agent": "chs-tile-proxy/0.1"},
    )
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(lifespan=lifespan, title="CHS Tile Proxy")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/tiles/{z}/{x}/{y}.png")
async def serve_tile(z: int, x: int, y: int) -> Response:
    if z < 0 or z > MAX_ZOOM:
        raise HTTPException(status_code=400, detail="zoom out of range")
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        raise HTTPException(status_code=400, detail="tile out of range")

    path = cache_path(z, x, y)
    if path.exists():
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

    write_cache_atomic(path, data)
    return Response(
        content=data,
        media_type="image/png",
        headers={"X-Cache": "MISS"},
    )
