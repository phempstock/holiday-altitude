#!/usr/bin/env python3
"""
Build Holiday Altitude's climate tiles.

Reads CHELSA V2.1 monthly climatologies (1981-2010, about 1 km, licence CC0) for
typical daily maximum and minimum temperature, clips them to the regions below, and
writes web-map tiles that store the temperature in each pixel's colour using the
Terrarium encoding (the same scheme as the elevation tiles):

    value = R * 256 + G + B / 256 - 32768      (here the value is in °C)

so the app can shade a temperature range with MapLibre's colour-relief layer, and
read a spot's temperature the same way it reads its height.

Output:  climate/{tmax,tmin}/{01..12}/{z}/{x}/{y}.png  and  climate/meta.json

Usage:
    python scripts/build_climate_tiles.py              # real data (needs rasterio + internet)
    python scripts/build_climate_tiles.py --synthetic  # made-up data, to test the tiling offline
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np
from PIL import Image

# Areas covered (west, south, east, north), in degrees. Keep in step with CLIMATE_REGIONS
# in index.html. Tiles from all regions go into one shared tile tree.
REGIONS = {
    "costa-rica": (-86.3, 7.7, -82.3, 11.5),
    "spain": (-9.6, 35.8, 4.6, 44.0),            # mainland Spain, Portugal, Balearic Islands
    "canary-islands": (-18.4, 27.5, -13.2, 29.6),
}
# Zoom levels written. 9 is about 300 m per pixel here, finer than the 1 km source;
# the app lets MapLibre enlarge zoom-9 tiles smoothly beyond that.
ZOOMS = list(range(5, 10))
TILE = 256

VARIABLES = {"tmax": "tasmax", "tmin": "tasmin"}  # our name -> CHELSA name
CHELSA_URL = ("https://os.zhdk.cloud.switch.ch/chelsav2/GLOBAL/climatologies/1981-2010/"
              "{v}/CHELSA_{v}_{m:02d}_1981-2010_V.2.1.tif")
CHELSA_LIST = ("https://os.zhdk.cloud.switch.ch/chelsav2?prefix="
               "GLOBAL/climatologies/1981-2010/{v}/")

NODATA_C = -99.0  # written where there is no land data (sea); far outside any comfort range

# Places used to sanity-check the result: (name, lat, lon, approx height m)
CHECKS = [
    ("Puntarenas (coast)", 9.976, -84.836, 5),
    ("San José", 9.933, -84.080, 1160),
    ("Monteverde", 10.300, -84.818, 1400),
    ("San Gerardo de Dota", 9.550, -83.805, 2200),
    ("Cerro de la Muerte", 9.560, -83.750, 3400),
    ("Madrid", 40.417, -3.704, 650),
    ("Seville", 37.389, -5.984, 10),
    ("Bilbao", 43.263, -2.935, 20),
    ("Pradollano (Sierra Nevada)", 37.095, -3.397, 2100),
    ("Palma de Mallorca", 39.570, 2.650, 10),
    ("Las Palmas (Gran Canaria)", 28.110, -15.420, 10),
]
# (warmer place, cooler place, month, at least this many °C warmer in typical highs)
COMPARISONS = [
    ("Puntarenas (coast)", "San Gerardo de Dota", 1, 5),
    ("Seville", "Pradollano (Sierra Nevada)", 7, 8),
    ("Seville", "Bilbao", 7, 5),
    ("Las Palmas (Gran Canaria)", "Madrid", 1, 5),
]


class Grid:
    """A regular latitude/longitude grid of temperatures in °C (NaN = no data)."""

    def __init__(self, data, west, north, dx, dy):
        self.data = data.astype(np.float32)
        self.west, self.north, self.dx, self.dy = west, north, dx, dy
        self.h, self.w = self.data.shape

    def sample(self, lon, lat):
        """Bilinear sample at arrays of lon/lat; ignores missing neighbours; NaN if none."""
        fx = (np.asarray(lon) - self.west) / self.dx - 0.5
        fy = (self.north - np.asarray(lat)) / self.dy - 0.5
        x0 = np.floor(fx).astype(int)
        y0 = np.floor(fy).astype(int)
        tx = fx - x0
        ty = fy - y0
        total = np.zeros(np.shape(fx), dtype=np.float64)
        weight = np.zeros(np.shape(fx), dtype=np.float64)
        for oy, wy in ((0, 1 - ty), (1, ty)):
            for ox, wx in ((0, 1 - tx), (1, tx)):
                xi = np.clip(x0 + ox, 0, self.w - 1)
                yi = np.clip(y0 + oy, 0, self.h - 1)
                inside = (x0 + ox >= 0) & (x0 + ox < self.w) & (y0 + oy >= 0) & (y0 + oy < self.h)
                v = self.data[yi, xi]
                ok = inside & ~np.isnan(v)
                wgt = np.where(ok, wx * wy, 0.0)
                total += np.where(ok, v, 0.0) * wgt
                weight += wgt
        out = np.full(np.shape(fx), np.nan)
        has = weight > 1e-9
        out[has] = total[has] / weight[has]
        return out


# ---------- Web Mercator tile maths ----------

def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    r = math.radians(lat)
    y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return x, y


def tiles_for_bbox(z, bbox):
    west, south, east, north = bbox
    x0, y0 = lonlat_to_tile(west, north, z)
    x1, y1 = lonlat_to_tile(east, south, z)
    for x in range(int(math.floor(x0)), int(math.floor(x1)) + 1):
        for y in range(int(math.floor(y0)), int(math.floor(y1)) + 1):
            yield x, y


def tile_pixel_lonlat(z, x, y):
    """Longitude/latitude of the centre of every pixel in a tile (256 x 256 arrays)."""
    n = 2 ** z * TILE
    px = (x * TILE + np.arange(TILE) + 0.5) / n
    py = (y * TILE + np.arange(TILE) + 0.5) / n
    lon = px * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * py))))
    return np.meshgrid(lon, lat)


# ---------- Terrarium encoding ----------

def encode_terrarium(values_c):
    """°C (NaN = no data) -> RGB uint8 image array, rounded to 0.1 °C to keep files small."""
    t = np.where(np.isnan(values_c), NODATA_C, np.round(values_c * 10.0) / 10.0)
    v = t + 32768.0
    whole = np.floor(v)
    r = np.floor(whole / 256.0)
    g = whole - r * 256.0
    b = np.floor((v - whole) * 256.0 + 1e-6)
    return np.stack([r, g, b], axis=-1).clip(0, 255).astype(np.uint8)


def decode_terrarium(rgb):
    rgb = rgb.astype(np.float64)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


# ---------- Reading the source data ----------

def to_celsius(data, scale, offset):
    """Apply the file's own scale/offset if it has one; otherwise detect the unit."""
    if (scale not in (None, 1.0)) or (offset not in (None, 0.0)):
        return data * (scale or 1.0) + (offset or 0.0), "file scale %s, offset %s" % (scale, offset)
    med = float(np.nanmedian(data))
    if med > 1000:   # tenths of a kelvin
        return data / 10.0 - 273.15, "detected tenths of kelvin (median %.0f)" % med
    if med > 150:    # kelvin
        return data - 273.15, "detected kelvin (median %.1f)" % med
    return data, "already °C (median %.1f)" % med


def chelsa_url(var, month):
    """The expected file URL; if it isn't there, look the name up in the bucket listing."""
    import urllib.request
    url = CHELSA_URL.format(v=var, m=month)
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status == 200:
                return url
    except Exception as e:  # noqa: BLE001
        print("  expected name not found (%s); searching the listing" % e)
    with urllib.request.urlopen(CHELSA_LIST.format(v=var), timeout=120) as r:
        listing = r.read().decode("utf-8", "replace")
    import re
    keys = re.findall(r"<Key>([^<]+)</Key>", listing)
    want = "_%02d_" % month
    for k in keys:
        name = k.rsplit("/", 1)[-1]
        if name.startswith("CHELSA_%s" % var) and want in name and name.endswith(".tif"):
            return "https://os.zhdk.cloud.switch.ch/chelsav2/" + k
    raise RuntimeError("Could not find CHELSA %s for month %d" % (var, month))


def read_chelsa(var, month, bboxes):
    """Reads one month of one variable for every region; returns a list of Grids."""
    import rasterio
    from rasterio.windows import from_bounds

    url = chelsa_url(var, month)
    env = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
               CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
               GDAL_HTTP_MAX_RETRY="5", GDAL_HTTP_RETRY_DELAY="3")
    with rasterio.Env(**env):
        with rasterio.open("/vsicurl/" + url) as src:
            scale = src.scales[0] if src.scales else None
            offset = src.offsets[0] if src.offsets else None
            grids, how = [], ""
            for bbox in bboxes:
                win = from_bounds(*bbox, transform=src.transform).round_offsets().round_lengths()
                arr = src.read(1, window=win, masked=True).astype("float64").filled(np.nan)
                if src.nodata is not None:
                    arr[arr == src.nodata] = np.nan
                tr = src.window_transform(win)
                data, how = to_celsius(arr, scale, offset)
                grids.append(Grid(data, west=tr.c, north=tr.f, dx=tr.a, dy=-tr.e))
    return grids, url, how


def synthetic_grids(var, month, bboxes):
    grids = [synthetic_grid(var, month, b) for b in bboxes]
    return grids, "synthetic", "synthetic °C"


def sample_all(grids, lon, lat):
    """Sample every region's grid and keep the first value found at each point."""
    out = None
    for g in grids:
        v = g.sample(lon, lat)
        out = v if out is None else np.where(np.isnan(out), v, out)
    return out


def synthetic_grid(var, month, bbox):
    """Made-up but plausible temperatures for offline testing: cooler with height
    (a fake mountain ridge) and slightly seasonal; NaN over a fake sea."""
    dx = dy = 1.0 / 120.0  # 30 arc-seconds, like CHELSA
    west, south, east, north = bbox
    lons = west + (np.arange(int((east - west) / dx)) + 0.5) * dx
    lats = north - (np.arange(int((north - south) / dy)) + 0.5) * dy
    LON, LAT = np.meshgrid(lons, lats)
    ridge = np.exp(-((LAT - 9.5 - 0.4 * (LON + 84)) ** 2) / 0.15) * 3200.0  # metres
    base = 32.0 if var == "tasmax" else 22.0
    season = 1.5 * math.cos((month - 4) / 12.0 * 2 * math.pi)
    t = base + season - ridge * 0.0062
    t = t - np.clip(LAT - 10, 0, None) * 0.6   # cooler further from the equator
    sea = (LAT < south + 0.6) | (LON < west + 0.4)
    t[sea] = np.nan
    return Grid(t, west, north, dx, dy)


# ---------- Main ----------

def build(out_dir, synthetic):
    started = time.time()
    meta = {
        "source": "CHELSA V2.1 climatologies 1981-2010 (Karger et al.), licence CC0" if not synthetic
                  else "SYNTHETIC TEST DATA - not real climate",
        "encoding": "terrarium, value in °C, rounded to 0.1 °C; %g = no data" % NODATA_C,
        "regions": REGIONS, "minzoom": ZOOMS[0], "maxzoom": ZOOMS[-1],
        "variables": {"tmax": "typical daily maximum temperature",
                      "tmin": "typical daily minimum temperature"},
        "files": {}, "checks": {name: {} for name, *_ in CHECKS},
    }
    tile_count = 0
    byte_count = 0
    worst_roundtrip = 0.0
    bboxes = list(REGIONS.values())
    # Every tile any region needs, at each zoom (regions that share a tile are merged into it).
    tiles = {z: sorted({t for b in bboxes for t in tiles_for_bbox(z, b)}) for z in ZOOMS}

    for ours, chelsa in VARIABLES.items():
        for month in range(1, 13):
            grids, url, how = (synthetic_grids(chelsa, month, bboxes) if synthetic
                               else read_chelsa(chelsa, month, bboxes))
            meta["files"]["%s_%02d" % (ours, month)] = {"url": url, "units": how}
            print("%s %02d: %s; %s" % (ours, month, url, how), flush=True)

            for name, lat, lon, _h in CHECKS:
                v = float(sample_all(grids, np.array([lon]), np.array([lat]))[0])
                meta["checks"][name]["%s_%02d" % (ours, month)] = None if math.isnan(v) else round(v, 1)

            for z in ZOOMS:
                for x, y in tiles[z]:
                    LON, LAT = tile_pixel_lonlat(z, x, y)
                    vals = sample_all(grids, LON, LAT)
                    rgb = encode_terrarium(vals)
                    # round-trip check on the land pixels
                    land = ~np.isnan(vals)
                    if land.any():
                        diff = np.abs(decode_terrarium(rgb)[land] - vals[land]).max()
                        worst_roundtrip = max(worst_roundtrip, float(diff))
                    path = os.path.join(out_dir, ours, "%02d" % month, str(z), str(x), "%d.png" % y)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    Image.fromarray(rgb, "RGB").save(path, optimize=True)
                    tile_count += 1
                    byte_count += os.path.getsize(path)

    meta["tiles"] = tile_count
    meta["megabytes"] = round(byte_count / 1e6, 1)
    meta["worst_roundtrip_error_c"] = round(worst_roundtrip, 3)
    meta["built_seconds"] = round(time.time() - started)
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)

    # ----- sanity checks: stop the job (and the commit) if anything looks wrong -----
    problems = []
    if worst_roundtrip > 0.06:
        problems.append("encoding round-trip error %.3f °C" % worst_roundtrip)
    print("\nSpot checks (°C):  month: high / low")
    for name, *_ in CHECKS:
        c = meta["checks"][name]
        row = []
        for m in range(1, 13):
            hi, lo = c.get("tmax_%02d" % m), c.get("tmin_%02d" % m)
            row.append("%s/%s" % (hi, lo))
            if hi is None or lo is None:
                problems.append("%s month %d has no data" % (name, m))
            elif not (-25 <= lo <= hi <= 45):
                problems.append("%s month %d implausible: high %s, low %s" % (name, m, hi, lo))
        print("  %-27s %s" % (name, "  ".join(row)))
    for warm, cool, month, gap in ([] if synthetic else COMPARISONS):
        a = meta["checks"][warm].get("tmax_%02d" % month)
        b = meta["checks"][cool].get("tmax_%02d" % month)
        if a is not None and b is not None and not a >= b + gap:
            problems.append("%s (%s) should be at least %d °C warmer than %s (%s) in month %d"
                            % (warm, a, gap, cool, b, month))

    print("\n%d tiles, %.1f MB, worst round-trip error %.3f °C" % (tile_count, byte_count / 1e6, worst_roundtrip))
    if problems:
        print("\nPROBLEMS:\n  " + "\n  ".join(problems))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthetic", action="store_true", help="use made-up data to test the tiling")
    ap.add_argument("--out", default="climate", help="output folder (default: climate)")
    args = ap.parse_args()
    sys.exit(build(args.out, args.synthetic))
