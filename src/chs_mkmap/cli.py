"""Build a Garmin Custom Maps KMZ from CHS nautical chart tiles.

Compatible with Garmin handhelds that support Custom Maps: eTrex 20x/22x/30x/
32x/Touch 25/35, GPSMAP 62/64/65/66, Oregon, Montana, etc. Older eTrex models
(Vista/Legend HCx) do NOT support Custom Maps — they only accept vector .img
files, which can't be produced from raster ENC chart data.

Tiles are fetched directly from the CHS ENC MapServer (no proxy). Outbound
requests are rate-limited to be a good citizen of a free government service.

Drop the resulting .kmz into the device's /Garmin/CustomMaps/ folder.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import math
import random
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image

TILE_PX = 256
HALF = 20037508.342789244

# Garmin Custom Maps limits.
MAX_OVERLAY_PIXELS = 1_048_576  # 1 MP per overlay
MAX_OVERLAYS_PER_KMZ = 100
# Newer devices (Oregon 600+, GPSMAP 64/65/66, eTrex 30x/32x, Montana) allow
# up to 500 overlays loaded at once across all KMZs. Older devices are 100.
TYPICAL_DEVICE_LIMIT = 500

DEFAULT_CHS_EXPORT = (
    "https://egisp.dfo-mpo.gc.ca/arcgis/rest/services/chs"
    "/ENC_MaritimeChartService/MapServer/exts/MaritimeChartService"
    "/MapServer/export"
)
# Layers 0-7 are the default-on chart content; matches the proxy.
DEFAULT_LAYERS = "show:0,1,2,3,4,5,6,7"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Retry policy for transient network / upstream errors. Permanent failures
# (4xx other than 429, non-image responses, malformed PNGs) are not retried.
RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY = 1.0  # seconds; doubled each attempt, plus jitter


class TransientFetchError(Exception):
    """Raised for upstream failures that warrant a retry."""


class RateLimiter:
    """Async rate limiter: at most `rate` acquires per second, fair-ordered."""

    def __init__(self, rate_per_sec: float):
        if rate_per_sec <= 0:
            raise ValueError("rate must be > 0")
        self._interval = 1.0 / rate_per_sec
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next = max(now, self._next) + self._interval


def tile_to_mercator_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z
    tile = HALF * 2 / n
    xmin = -HALF + x * tile
    xmax = xmin + tile
    ymax = HALF - y * tile
    ymin = ymax - tile
    return xmin, ymin, xmax, ymax


@dataclass
class TileRange:
    z: int
    x_min: int
    y_min: int
    x_max: int  # inclusive
    y_max: int  # inclusive

    @property
    def width(self) -> int:
        return self.x_max - self.x_min + 1

    @property
    def height(self) -> int:
        return self.y_max - self.y_min + 1


def lonlat_to_tile(z: int, lon: float, lat: float) -> tuple[int, int]:
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(min(lat, 85.0511), -85.0511))
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def tile_lonlat_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) for one XYZ tile in degrees."""
    n = 1 << z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def tile_range_for_bbox(z: int, south: float, west: float, north: float, east: float) -> TileRange:
    x_min, y_max_idx = lonlat_to_tile(z, west, south)
    x_max, y_min_idx = lonlat_to_tile(z, east, north)
    return TileRange(z=z, x_min=x_min, y_min=y_min_idx, x_max=x_max, y_max=y_max_idx)


async def _fetch_tile_once(
    client: httpx.AsyncClient,
    chs_url: str,
    layers: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    xmin, ymin, xmax, ymax = tile_to_mercator_bbox(z, x, y)
    params = {
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": "3857",
        "size": f"{TILE_PX},{TILE_PX}",
        "imageSR": "3857",
        "format": "png",
        "f": "image",
        "layers": layers,
        "transparent": "true",
    }
    try:
        r = await client.get(chs_url, params=params)
    except (httpx.TransportError, httpx.TimeoutException) as e:
        # DNS failure, connection reset, read timeout, etc. — all worth retrying.
        raise TransientFetchError(f"network error: {e!r}") from e

    if r.status_code == 429 or 500 <= r.status_code < 600:
        raise TransientFetchError(f"upstream HTTP {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"tile {z}/{x}/{y} returned HTTP {r.status_code}")
    ctype = r.headers.get("content-type", "")
    if not ctype.lower().startswith("image/"):
        raise RuntimeError(f"tile {z}/{x}/{y} non-image content-type {ctype!r}")
    data = r.content
    if not data.startswith(PNG_MAGIC):
        raise RuntimeError(f"tile {z}/{x}/{y} did not return a PNG")
    return data


async def fetch_tile(
    client: httpx.AsyncClient,
    chs_url: str,
    layers: str,
    z: int,
    x: int,
    y: int,
    rate: "RateLimiter | None" = None,
) -> bytes:
    last: BaseException | None = None
    for attempt in range(RETRY_ATTEMPTS):
        if rate is not None:
            await rate.acquire()
        try:
            return await _fetch_tile_once(client, chs_url, layers, z, x, y)
        except TransientFetchError as e:
            last = e
            if attempt == RETRY_ATTEMPTS - 1:
                break
            delay = RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 0.5)
            print(
                f"  tile {z}/{x}/{y}: {e}; retry {attempt + 1}/{RETRY_ATTEMPTS - 1}"
                f" in {delay:.1f}s",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(
        f"tile {z}/{x}/{y}: gave up after {RETRY_ATTEMPTS} attempts ({last})"
    )


async def fetch_all_tiles(
    chs_url: str,
    layers: str,
    user_agent: str,
    tr: TileRange,
    concurrency: int,
    rate_per_sec: float,
) -> dict[tuple[int, int], bytes]:
    sem = asyncio.Semaphore(concurrency)
    rate = RateLimiter(rate_per_sec)
    tiles: dict[tuple[int, int], bytes] = {}
    total = tr.width * tr.height
    done = 0

    async with httpx.AsyncClient(
        timeout=60,
        headers={"User-Agent": user_agent},
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=max(concurrency, 4),
            max_keepalive_connections=max(concurrency, 4),
        ),
    ) as client:
        async def get(x: int, y: int) -> None:
            nonlocal done
            async with sem:
                data = await fetch_tile(
                    client, chs_url, layers, tr.z, x, y, rate=rate
                )
            tiles[(x, y)] = data
            done += 1
            if done % 10 == 0 or done == total:
                print(f"  fetched {done}/{total} tiles", file=sys.stderr)

        tasks = [
            get(x, y)
            for y in range(tr.y_min, tr.y_max + 1)
            for x in range(tr.x_min, tr.x_max + 1)
        ]
        await asyncio.gather(*tasks)
    return tiles


def group_tiles(tr: TileRange, tiles_per_side: int) -> list[TileRange]:
    """Subdivide the tile range into sub-ranges of at most NxN tiles."""
    groups: list[TileRange] = []
    for y0 in range(tr.y_min, tr.y_max + 1, tiles_per_side):
        for x0 in range(tr.x_min, tr.x_max + 1, tiles_per_side):
            groups.append(
                TileRange(
                    z=tr.z,
                    x_min=x0,
                    y_min=y0,
                    x_max=min(x0 + tiles_per_side - 1, tr.x_max),
                    y_max=min(y0 + tiles_per_side - 1, tr.y_max),
                )
            )
    return groups


def compose_overlay(
    group: TileRange, tiles: dict[tuple[int, int], bytes], quality: int
) -> tuple[bytes, tuple[float, float, float, float]]:
    """Stitch a group of tiles into one JPEG; return (jpeg_bytes, (W,S,E,N))."""
    canvas = Image.new("RGB", (group.width * TILE_PX, group.height * TILE_PX), "white")
    for y in range(group.y_min, group.y_max + 1):
        for x in range(group.x_min, group.x_max + 1):
            tile_img = Image.open(io.BytesIO(tiles[(x, y)])).convert("RGB")
            canvas.paste(
                tile_img,
                ((x - group.x_min) * TILE_PX, (y - group.y_min) * TILE_PX),
            )

    w_nw, _, _, n_nw = tile_lonlat_bounds(group.z, group.x_min, group.y_min)
    _, s_se, e_se, _ = tile_lonlat_bounds(group.z, group.x_max, group.y_max)

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), (w_nw, s_se, e_se, n_nw)


def build_kml(name: str, overlays: list[tuple[str, tuple[float, float, float, float]]]) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document>",
        f"<name>{_xml_escape(name)}</name>",
    ]
    for fname, (west, south, east, north) in overlays:
        parts.append(
            "<GroundOverlay>"
            f"<name>{_xml_escape(fname)}</name>"
            "<drawOrder>50</drawOrder>"
            f"<Icon><href>files/{fname}</href></Icon>"
            "<LatLonBox>"
            f"<north>{north:.10f}</north>"
            f"<south>{south:.10f}</south>"
            f"<east>{east:.10f}</east>"
            f"<west>{west:.10f}</west>"
            "</LatLonBox>"
            "</GroundOverlay>"
        )
    parts.extend(["</Document>", "</kml>"])
    return "\n".join(parts)


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_kmz(
    output: Path,
    name: str,
    overlays: list[tuple[str, bytes, tuple[float, float, float, float]]],
) -> None:
    kml = build_kml(name, [(fname, box) for fname, _data, box in overlays])
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
        for fname, data, _box in overlays:
            z.writestr(f"files/{fname}", data)


def parse_bbox(s: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "bbox must be 'south,west,north,east' (four decimal degrees)"
        )
    try:
        south, west, north, east = (float(p) for p in parts)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"bbox values must be numbers: {e}") from None
    if not (-90 <= south < north <= 90):
        raise argparse.ArgumentTypeError("south must be < north and within [-90, 90]")
    if not (-180 <= west < east <= 180):
        raise argparse.ArgumentTypeError("west must be < east and within [-180, 180]")
    return south, west, north, east


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="chs-mkmap",
        description=(
            "Build a Garmin Custom Maps KMZ of Canadian nautical charts. "
            "Fetches tiles directly from the CHS ENC MapServer (rate-limited) "
            "and packages them with the right georeferencing for eTrex 20x/22x/"
            "30x/32x/Touch, GPSMAP 62/64/65/66, Oregon and Montana."
        ),
    )
    p.add_argument(
        "--bbox",
        required=True,
        type=parse_bbox,
        help="bounding box as 'south,west,north,east' in decimal degrees",
    )
    p.add_argument(
        "--zoom",
        type=int,
        default=13,
        help="tile zoom level to fetch (default: 13, ~5km/tile at 50°N)",
    )
    p.add_argument(
        "--chs-url",
        default=DEFAULT_CHS_EXPORT,
        help="override the CHS export endpoint (default: production CHS URL)",
    )
    p.add_argument(
        "--layers",
        default=DEFAULT_LAYERS,
        help=f"ArcGIS layers spec (default: {DEFAULT_LAYERS!r})",
    )
    p.add_argument(
        "--rate",
        type=float,
        default=5.0,
        help=(
            "max outbound requests per second to CHS (default: 5). "
            "Be polite — CHS is a free public service."
        ),
    )
    p.add_argument(
        "--contact",
        default="",
        help=(
            "optional contact email or URL added to the outbound User-Agent. "
            "Strongly recommended so CHS can reach you instead of silently blocking."
        ),
    )
    p.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("chs_charts.kmz"),
        help="output KMZ path (default: chs_charts.kmz)",
    )
    p.add_argument(
        "--name",
        default="CHS Charts",
        help="display name embedded in the KMZ (default: 'CHS Charts')",
    )
    p.add_argument(
        "--tiles-per-side",
        type=int,
        default=4,
        help=(
            "tiles per side of each overlay image. 4 gives 1024x1024 = 1 MP, "
            "the Garmin per-overlay maximum (default: 4)"
        ),
    )
    p.add_argument(
        "--quality",
        type=int,
        default=80,
        help="JPEG quality 1-95 (default: 80)",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="concurrent tile fetches (default: 8)",
    )
    p.add_argument(
        "--max-per-kmz",
        type=int,
        default=MAX_OVERLAYS_PER_KMZ,
        help=(
            f"max overlays per KMZ file (default: {MAX_OVERLAYS_PER_KMZ}, "
            "Garmin's documented per-file limit). When the total exceeds this, "
            "the output is auto-split into numbered KMZ files."
        ),
    )
    p.add_argument(
        "--no-split",
        action="store_true",
        help=(
            "disable auto-splitting and write a single KMZ even if it exceeds "
            "--max-per-kmz. Garmin will silently drop overlays past the limit."
        ),
    )
    args = p.parse_args(argv)

    south, west, north, east = args.bbox

    if args.tiles_per_side < 1:
        print("--tiles-per-side must be >= 1", file=sys.stderr)
        return 2
    if args.tiles_per_side * TILE_PX * args.tiles_per_side * TILE_PX > MAX_OVERLAY_PIXELS:
        print(
            f"warning: each overlay will be {args.tiles_per_side * TILE_PX}x{args.tiles_per_side * TILE_PX}"
            f" = {(args.tiles_per_side * TILE_PX) ** 2} pixels, exceeds Garmin's"
            f" 1 MP per-overlay limit. eTrex may refuse to display it.",
            file=sys.stderr,
        )

    tr = tile_range_for_bbox(args.zoom, south, west, north, east)
    total_tiles = tr.width * tr.height
    groups = group_tiles(tr, args.tiles_per_side)

    print(
        f"zoom={args.zoom}  tiles={total_tiles} ({tr.width}x{tr.height})  "
        f"overlays={len(groups)}",
        file=sys.stderr,
    )

    if args.max_per_kmz < 1:
        print("--max-per-kmz must be >= 1", file=sys.stderr)
        return 2

    if len(groups) > MAX_OVERLAYS_PER_KMZ and args.max_per_kmz > MAX_OVERLAYS_PER_KMZ:
        print(
            f"warning: --max-per-kmz={args.max_per_kmz} exceeds Garmin's documented "
            f"{MAX_OVERLAYS_PER_KMZ}/file limit; the device may silently drop overlays.",
            file=sys.stderr,
        )

    if len(groups) > TYPICAL_DEVICE_LIMIT:
        print(
            f"warning: {len(groups)} overlays exceeds the typical per-device limit "
            f"of {TYPICAL_DEVICE_LIMIT} (newer Garmins) / {MAX_OVERLAYS_PER_KMZ} "
            "(older models). Some overlays will not render even if split across "
            "multiple KMZs. Consider a lower --zoom or smaller --bbox.",
            file=sys.stderr,
        )

    user_agent = (
        f"chs-mkmap/0.1 (+{args.contact})" if args.contact else "chs-mkmap/0.1"
    )
    if not args.contact:
        print(
            "warning: --contact not set. Consider passing an email or URL so "
            "CHS can reach you instead of silently blocking.",
            file=sys.stderr,
        )

    print(
        f"fetching {total_tiles} tiles from CHS at {args.rate} req/s "
        f"(concurrency {args.concurrency}) ...",
        file=sys.stderr,
    )
    tiles = asyncio.run(
        fetch_all_tiles(
            args.chs_url, args.layers, user_agent, tr, args.concurrency, args.rate
        )
    )

    print(f"composing {len(groups)} overlays ...", file=sys.stderr)
    overlays: list[tuple[str, bytes, tuple[float, float, float, float]]] = []
    for i, g in enumerate(groups, start=1):
        jpg, box = compose_overlay(g, tiles, args.quality)
        overlays.append((f"overlay_{i:04d}.jpg", jpg, box))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    split = not args.no_split and len(overlays) > args.max_per_kmz
    written = (
        _write_split(args.output, args.name, overlays, args.max_per_kmz)
        if split
        else _write_single(args.output, args.name, overlays)
    )

    print(
        f"copy to your device under /Garmin/CustomMaps/ ({len(written)} file"
        f"{'s' if len(written) != 1 else ''}):",
        file=sys.stderr,
    )
    for p in written:
        print(f"  {p.name}  ({p.stat().st_size:,} bytes)", file=sys.stderr)
    return 0


def _write_single(
    output: Path,
    name: str,
    overlays: list[tuple[str, bytes, tuple[float, float, float, float]]],
) -> list[Path]:
    write_kmz(output, name, overlays)
    return [output]


def _write_split(
    output: Path,
    name: str,
    overlays: list[tuple[str, bytes, tuple[float, float, float, float]]],
    per_file: int,
) -> list[Path]:
    """Chunk overlays into per_file groups and write one KMZ per chunk."""
    total = len(overlays)
    parts = math.ceil(total / per_file)
    width = max(2, len(str(parts)))
    stem = output.stem
    suffix = output.suffix or ".kmz"
    parent = output.parent

    written: list[Path] = []
    for i in range(parts):
        chunk = overlays[i * per_file : (i + 1) * per_file]
        out_path = parent / f"{stem}_{i + 1:0{width}d}{suffix}"
        part_name = f"{name} (part {i + 1} of {parts})"
        write_kmz(out_path, part_name, chunk)
        written.append(out_path)
    return written


if __name__ == "__main__":
    sys.exit(main())
