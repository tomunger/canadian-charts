# canadian-charts

A caching XYZ tile proxy that turns the **Canadian Hydrographic Service (CHS) ENC Maritime Chart Service** into a standard slippy-map tile endpoint suitable for [Gaia GPS](https://www.gaiagps.com/), Leaflet, MapLibre, OpenLayers, QGIS, or any other client that consumes `{z}/{x}/{y}.png` tiles.

CHS publishes its charts as a dynamic ArcGIS MapServer with no tile cache and no WMTS endpoint, so each map view requires a bounding-box `export` call. This proxy:

1. Converts XYZ tile coordinates into the EPSG:3857 bbox the CHS service expects.
2. Forwards the request to CHS and returns the rendered PNG.
3. Caches every rendered tile to disk so repeated views of the same area never hit the upstream again.
4. Coalesces concurrent requests for the same tile so a burst of clients fetching the same area only triggers one upstream call.

The result is a fast, offline-friendly nautical chart layer for Canadian waters.

## Endpoint

```
GET /tiles/{z}/{x}/{y}.png
GET /healthz
```

Point your client at `http://<host>:8001/tiles/{z}/{x}/{y}.png`. Coverage is everywhere CHS publishes ENC data — Canadian coastal and inland waters, from the Pacific to the Atlantic to the Arctic.

## Configuration

All settings are environment variables. Defaults are tuned for typical use; you usually only need to change `CACHE_DIR`.

| Variable | Default | Description |
| --- | --- | --- |
| `CACHE_DIR` | `/var/cache/chs_tiles` | Directory where rendered tiles are persisted. Mount a volume here so the cache survives container restarts. |
| `CHS_EXPORT_URL` | CHS production endpoint | The upstream ArcGIS `export` URL. Override only if CHS changes the path. |
| `CHS_LAYERS` | `show:0,1,2,3,4,5,6,7` | Which ENC layers to render. The default matches the layers enabled by default in the CHS service definition (chart display, features, depths, seabed, traffic routes, special areas, navaids, services). Add `8` for data-quality hatching, `9`–`12` for low-accuracy / shallow-water / overscale warnings. |
| `TILE_SIZE` | `256` | Tile edge in pixels. Stick with 256 for Gaia GPS. |
| `MAX_ZOOM` | `17` | Maximum zoom the server will accept; requests above this return HTTP 400. |
| `CACHE_TTL_MINUTES` | `20160` (14 days) | How long a cached tile is considered fresh. Tiles older than this are re-fetched on the next request. Set to `0` to disable expiry and keep tiles forever. |
| `CACHE_MAX_BYTES` | `21474836480` (20 GiB) | On-disk cache size cap. A background task evicts oldest-mtime tiles when usage exceeds this. Set to `0` to disable eviction. |
| `CACHE_EVICTION_INTERVAL_SECONDS` | `3600` | How often the eviction task runs. |
| `ALLOW_LON_MIN` / `ALLOW_LAT_MIN` / `ALLOW_LON_MAX` / `ALLOW_LAT_MAX` | `-145` / `40` / `-50` / `85` | Lat/lon allowlist envelope. Tiles whose envelope does not intersect this bbox return HTTP 400. Default covers Canadian waters with offshore margin. |
| `RATE_LIMIT` | `60/minute` | Per-IP rate limit applied to `/tiles/...`. Format is [slowapi-style](https://slowapi.readthedocs.io/) (e.g. `30/minute`, `10/second`, `1000/hour`). |
| `UPSTREAM_CONCURRENCY` | `8` | Process-wide cap on simultaneous outbound requests to CHS. |
| `MAX_TILE_BYTES` | `2097152` (2 MiB) | Hard cap on upstream response size; larger responses are rejected with HTTP 502. |
| `CONTACT` | *(empty)* | Optional contact string (email or URL) appended to the outbound `User-Agent`. Strongly recommended when running publicly so CHS can reach you instead of silently blocking. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Trusted-proxy list for uvicorn's `--proxy-headers`. Set this to your reverse-proxy IP (or `*` if you trust the surrounding network) so the rate limiter sees real client IPs from `X-Forwarded-For`. |
| `UPSTREAM_TIMEOUT` | `30` | Seconds to wait for the CHS service before returning HTTP 502. |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

### Tile coordinate scheme

`z`, `x`, `y` follow the Google / OSM XYZ convention:

- **z** — zoom level (`0` = whole world as one tile; world is a `2^z × 2^z` grid).
- **x** — column, `0` at 180°W, increasing eastward.
- **y** — row, `0` at the top (~85°N), increasing southward.

Conversion from latitude/longitude:

```python
import math
n = 2 ** z
x = int((lon + 180.0) / 360.0 * n)
y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
```

### Cache layout

Tiles are written as `${CACHE_DIR}/{z}/{x}/{y}.png` with atomic replace (no partial files on crash). The cache is content-stable — you can safely rsync, snapshot, or back it up while the server is running. Clearing it is as simple as `rm -rf` on the directory.

Responses include an `X-Cache: HIT` or `X-Cache: MISS` header so you can verify caching is working.

## Run locally (docker compose)

```sh
docker compose up --build
```

Then open `http://localhost:8001/tiles/14/2418/5564.png` (a chart tile off Vancouver Island).

## Run locally (uv, no Docker)

```sh
uv sync
uv run uvicorn chs_proxy.main:app --host 0.0.0.0 --port 8001
```

## Install on Unraid

The image is published to GitHub Container Registry as `ghcr.io/<owner>/canadian-charts:latest` and built for both `linux/amd64` and `linux/arm64`. The container runs as UID 99 / GID 100 (`nobody:users`) so a default Unraid appdata bind mount works without any permission tweaks.

### Option A — template install (recommended)

1. On your workstation, edit [unraid/chs-tile-proxy.xml](unraid/chs-tile-proxy.xml) and replace each `OWNER` with the GitHub user or organization that hosts the image (three occurrences: `<Repository>`, `<Project>`, `<Icon>`).
2. Copy the template onto the NAS:
   ```sh
   scp unraid/chs-tile-proxy.xml root@<nas>:/boot/config/plugins/dockerMan/templates-user/my-chs-tile-proxy.xml
   ```
3. In the Unraid web UI go to **Docker → Add Container**, pick `chs-tile-proxy` from the **Template** dropdown, review the settings, and click **Apply**. Unraid will pull the image from ghcr.io and start it.

### Option B — manual

In **Docker → Add Container**, fill in:

- **Repository:** `ghcr.io/<owner>/canadian-charts:latest`
- **Network Type:** Bridge
- **Port:** `8001` (host) → `8001` (container), TCP
- **Path:** `/mnt/user/appdata/chs-tile-proxy/cache` (host) → `/var/cache/chs_tiles` (container), Read/Write

Optionally add the env vars from the configuration table above under **Add another Path, Port, Variable, Label or Device**.

### After install

- Verify it's running: open `http://<nas-ip>:8001/healthz` — should return `{"status":"ok"}`.
- Fetch a test tile: `http://<nas-ip>:8001/tiles/14/2418/5564.png`.
- Add the layer to Gaia GPS: **Settings → Map Sources → Add… → Custom Source**, URL `http://<nas-ip>:8001/tiles/{z}/{x}/{y}.png`. Set the max zoom to whatever you configured (default 19); the source name is up to you.

### Updates

The GitHub Actions workflow rebuilds and pushes `latest` (plus a `sha-<commit>` tag) on every push to `main`. On the NAS, Unraid's **Check for Updates** in the Docker tab will pick up the new image. The cache directory is preserved across image updates.

## Layer reference

The CHS ENC service publishes 13 layers. The proxy renders the eight that are on by default; the others can be added by overriding `CHS_LAYERS`.

| ID | Layer | Default |
| -- | --- | --- |
| 0 | Information about the chart display | on |
| 1 | Natural and man-made features, port features | on |
| 2 | Depths, currents | on |
| 3 | Seabed, obstructions, pipelines | on |
| 4 | Traffic routes | on |
| 5 | Special areas | on |
| 6 | Buoys, beacons, lights, fog signals, radar | on |
| 7 | Services and small craft facilities | on |
| 8 | Data quality (diagonal hatching) | off |
| 9 | Low accuracy | off |
| 10 | Additional chart information | off |
| 11 | Shallow water pattern | off |
| 12 | Overscale warning | off |

## Building Garmin Custom Maps (`chs-mkmap`)

This repo also ships a CLI that fetches tiles from your running proxy and packages them as a **Garmin Custom Maps KMZ** suitable for handheld GPS devices.

**Supported devices:** eTrex 20x / 22x / 30x / 32x / Touch 25 / Touch 35, GPSMAP 62 / 64 / 65 / 66, Oregon, Montana. Older eTrex models (Vista HCx, Legend HCx) don't support Custom Maps — they only take vector `.img` files and aren't compatible with raster ENC data.

### Install

```sh
uv sync
```

The `chs-mkmap` console script lands in the project venv. Run it via `uv run chs-mkmap …` or activate the venv with `source .venv/bin/activate`.

### Usage

`chs-mkmap` fetches tiles **directly from the CHS server** (not through the local proxy) and rate-limits its outbound requests to be a good citizen of a free government service.

```sh
uv run chs-mkmap \
  --bbox 49.5,-127.5,50.2,-126.0 \
  --zoom 13 \
  --contact you@example.com \
  --output nootka.kmz
```

`--bbox` is `south,west,north,east` in decimal degrees. The example above covers Nootka Sound on the west coast of Vancouver Island.

| Flag | Default | Description |
| --- | --- | --- |
| `--bbox` | *(required)* | Coverage area as `south,west,north,east`. |
| `--zoom` | `13` | Tile zoom (≈ 5 km/tile at 50°N). 12 covers more area at lower detail; 14 is finer but quadruples tile count. |
| `--chs-url` | CHS production endpoint | Override the upstream export URL (mostly for testing). |
| `--layers` | `show:0,1,2,3,4,5,6,7` | ArcGIS layer spec — defaults to the visible chart layers. |
| `--rate` | `5` | Max outbound requests per second to CHS. |
| `--contact` | *(empty)* | Email or URL appended to the User-Agent so CHS can reach you. Strongly recommended. |
| `--output` / `-o` | `chs_charts.kmz` | Output KMZ path. |
| `--name` | `CHS Charts` | Display name embedded in the KMZ. |
| `--tiles-per-side` | `4` | Tiles per side of each overlay JPEG. 4 → 1024×1024 = 1 MP, Garmin's per-overlay max. |
| `--quality` | `80` | JPEG quality (1–95). |
| `--concurrency` | `8` | Max simultaneous in-flight requests (still subject to `--rate`). |
| `--max-per-kmz` | `100` | Cap on overlays per KMZ. When the total exceeds this, output is auto-split into numbered KMZ files. |
| `--no-split` | off | Disable auto-splitting and write a single KMZ even if it exceeds the per-file limit (Garmin will silently drop excess overlays). |

### Large regions (auto-split)

A bbox covering all of Vancouver Island at zoom 13 produces ~900 overlays — far over Garmin's 100-per-KMZ limit. The tool detects this and splits the output automatically:

```sh
uv run chs-mkmap \
  --bbox 48.3,-128.8,51.1,-123.0 \
  --zoom 13 \
  --contact you@example.com \
  -o vancouver-island.kmz
```

You'll get `vancouver-island_01.kmz`, `vancouver-island_02.kmz`, … each at or under `--max-per-kmz`. Copy them all into `/Garmin/CustomMaps/` on the device.

**Caveat — per-device limit:** Garmin devices also cap total loaded overlays (typically 500 on Oregon 600+/GPSMAP 64+/eTrex 30x+, only 100 on older units). Auto-split lets you stay under the *per-file* limit, but the *per-device* limit still binds — `chs-mkmap` warns when your build would exceed it. For full Vancouver Island coverage at zoom 13 you'll need to either drop to zoom 12 or load only the parts you're cruising that season.

### Garmin's constraints (the tool enforces these)

- ≤ 1 MP per overlay image — default `--tiles-per-side 4` keeps each overlay at exactly 1 MP.
- ≤ 100 overlays per KMZ — the tool fails fast if your bbox + zoom would produce more (pass `--force` to override, or lower the zoom / shrink the bbox).
- ≤ ~500 overlays total across all KMZs loaded on the device.

### Loading onto the device

1. Connect the GPS via USB; it appears as a mass-storage device.
2. Copy the `.kmz` into `/Garmin/CustomMaps/` on the device (create the folder if it doesn't exist).
3. Eject and reboot the device.
4. Enable under **Setup → Map → Map Information → Select Map** — toggle your KMZ on.

The chart overlay will then render at appropriate zooms on the device's map page.

## Data attribution

Chart data is © Canadian Hydrographic Service / Fisheries and Oceans Canada, served from `egisp.dfo-mpo.gc.ca`. This project is an unaffiliated client of that public service; **do not use these tiles for navigation**. Always navigate from official, up-to-date paper or ECDIS charts.

# GaiaGPS

try: http://cnc.tumtum.com/tiles/14/2418/5564.png

Add map:  http://cnc.tumtum.com/tiles/{z}/{x}/{y}.png


```json
{
  "tilejson": "2.1.0",
  "name": "Canadian Nautical Charts",
  "description": "A caching server for Canadian Nautical charts",
  "version": "1.0.0",
  "attribution": "Canadian Hydrographics Serivice va a server hosted by Tom Unger. This project is an unaffiliated client of that public service; **do not use these tiles for navigation**. Always navigate from official, up-to-date paper or ECDIS charts.",
  "scheme": "xyz",
  "tiles": [
    "https://cnc.tumtum.com/tiles/{z}/{x}/{y}.png"
  ],
  "minzoom": 4,
  "maxzoom": 16,
  "bounds": [
    -133,
    7,
    0,
    80
  ]
}
```
