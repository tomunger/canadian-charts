# Instructions

Build a caching server that reads Canadian nautical charts and provides a 
map tile server that GaiaGPS can use.  Below is the plan and sample code 
written by Claude Chat.  Use this as a basis for writing a python based
server to be run in a docker container.  

# Claude Chat Output

Now I have a complete picture. The CHS service is a dynamic rendering service with no tile cache (singleFusedMapCache: false). There's no WMTS endpoint — so there's no pre-built {z}/{x}/{y} URL. You have to bridge the gap yourself.

## The Core Problem

Gaia GPS needs XYZ tile URLs. CHS only offers a bbox-based export endpoint. The solution is a small proxy that does the math to convert between the two.

## The Proxy Approach

Here's a complete FastAPI proxy that translates XYZ tile requests into CHS export calls:

``` python
import math
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

app = FastAPI()

CHS_EXPORT = (
    "https://egisp.dfo-mpo.gc.ca/arcgis/rest/services/chs"
    "/ENC_MaritimeChartService/MapServer/exts/MaritimeChartService"
    "/MapServer/export"
)

# Layers: 0-7 are the useful ones; 8=Data Quality (hatching), skip it
LAYERS = "show:0,1,2,3,4,5,6,7"

def tile_to_bbox(z: int, x: int, y: int):
    """Convert XYZ tile coords to EPSG:3857 bounding box."""
    n = 2 ** z
    half = 20037508.342789244
    tile_size = half * 2 / n
    xmin = -half + x * tile_size
    xmax = xmin + tile_size
    ymax =  half - y * tile_size
    ymin = ymax - tile_size
    return xmin, ymin, xmax, ymax

@app.get("/tiles/{z}/{x}/{y}.png")
async def serve_tile(z: int, x: int, y: int):
    xmin, ymin, xmax, ymax = tile_to_bbox(z, x, y)
    params = {
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": "3857",
        "size": "256,256",
        "imageSR": "3857",
        "format": "png",
        "f": "image",
        "layers": LAYERS,
        "transparent": "true",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(CHS_EXPORT, params=params)
    if r.status_code != 200:
        raise HTTPException(status_code=502)
    return Response(content=r.content, media_type="image/png")

```

```bash
pip install fastapi uvicorn httpx
uvicorn chs_proxy:app --host 0.0.0.0 --port 8001
```

Then in Gaia GPS custom map sources:

```
http://YOUR_SERVER_IP:8001/tiles/{z}/{x}/{y}.png
```

## Layer Reference
From the service definition, the 13 layers are:

IDNameDefault On0Information about the chart display✓1Natural and man-made features, port features✓2Depths, currents, etc✓3Seabed, obstructions, pipelines✓4Traffic routes✓5Special areas✓6Buoys, beacons, lights, fog signals, radar✓7Services and small craft facilities✓8Data quality✗ (diagonal hatching)9Low accuracy✗10Additional chart information✗11Shallow water pattern✗12Overscale warning✗

## Caching Layer

Since each tile request hits the CHS server live, adding a disk cache is worth it for frequently-viewed areas:

```python
import hashlib, os
from pathlib import Path

CACHE_DIR = Path("/tmp/chs_tiles")
CACHE_DIR.mkdir(exist_ok=True)

@app.get("/tiles/{z}/{x}/{y}.png")
async def serve_tile(z: int, x: int, y: int):
    cache_path = CACHE_DIR / f"{z}_{x}_{y}.png"
    if cache_path.exists():
        return Response(content=cache_path.read_bytes(), media_type="image/png")
    
    # ... fetch from CHS ...
    
    cache_path.write_bytes(r.content)
    return Response(content=r.content, media_type="image/png")

```
