"""Japan prefecture SVG paths.

Reads `data/seed/japan_prefectures.geojson` (raw GeoJSON FeatureCollection of
the 47 都道府県), decimates each polygon ring (every Nth point) to keep
file size reasonable, projects to an SVG viewBox, and caches the result.

Output: dict mapping prefecture name (Japanese) → SVG <path> `d` string.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .config import SEED_DIR

GEOJSON_PATH = SEED_DIR / "japan_prefectures.geojson"

# SVG viewBox dimensions for the rendered map.
VIEW_W = 600
VIEW_H = 600

# Decimation factor: keep every Nth point in each ring. Higher = smaller.
DECIMATE = 12
# Don't decimate rings smaller than this many points (keeps small islands intact).
MIN_POINTS = 8


def _iter_rings(geometry):
    """Yield (ring_idx, [(lng, lat), ...]) for every linear ring in a geometry."""
    t = geometry.get("type")
    coords = geometry.get("coordinates")
    if t == "Polygon":
        for ring in coords:
            yield ring
    elif t == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                yield ring
    # other types not used


def _decimate(ring):
    """Keep every Nth point + the first/last to close the ring."""
    if len(ring) <= MIN_POINTS:
        return ring
    kept = ring[::DECIMATE]
    # ensure ring closes back to start
    if kept[-1] != ring[0]:
        kept.append(ring[0])
    return kept


@lru_cache(maxsize=1)
def _raw_geo() -> dict:
    return json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _bounds() -> tuple[float, float, float, float]:
    """(lng_min, lng_max, lat_min, lat_max) over all coordinates."""
    geo = _raw_geo()
    lng_min, lng_max = 180.0, -180.0
    lat_min, lat_max = 90.0, -90.0
    for feature in geo["features"]:
        for ring in _iter_rings(feature["geometry"]):
            for lng, lat in ring:
                if lng < lng_min: lng_min = lng
                if lng > lng_max: lng_max = lng
                if lat < lat_min: lat_min = lat
                if lat > lat_max: lat_max = lat
    return lng_min, lng_max, lat_min, lat_max


@lru_cache(maxsize=1)
def get_prefecture_paths() -> dict[str, str]:
    """Return {prefecture_name_jp: SVG path d-string}. Cached."""
    geo = _raw_geo()
    lng_min, lng_max, lat_min, lat_max = _bounds()
    lng_span = lng_max - lng_min or 1
    lat_span = lat_max - lat_min or 1

    # Preserve aspect ratio: Japan is taller than wide at these latitudes.
    # Use the min of both scales so the whole map fits in viewBox.
    sx = VIEW_W / lng_span
    sy = VIEW_H / lat_span
    scale = min(sx, sy)
    # Centre in the viewBox
    map_w = lng_span * scale
    map_h = lat_span * scale
    ox = (VIEW_W - map_w) / 2
    oy = (VIEW_H - map_h) / 2

    def project(lng: float, lat: float) -> tuple[float, float]:
        x = ox + (lng - lng_min) * scale
        # invert y: GeoJSON lat grows north, SVG y grows down
        y = oy + (lat_max - lat) * scale
        return x, y

    paths: dict[str, str] = {}
    for feature in geo["features"]:
        name = feature["properties"].get("nam_ja")
        if not name:
            continue
        parts: list[str] = []
        for ring in _iter_rings(feature["geometry"]):
            small = _decimate(ring)
            if len(small) < 3:
                continue
            x, y = project(*small[0])
            seg = [f"M{x:.1f},{y:.1f}"]
            for lng, lat in small[1:]:
                xx, yy = project(lng, lat)
                seg.append(f"L{xx:.1f},{yy:.1f}")
            seg.append("Z")
            parts.append("".join(seg))
        if parts:
            paths[name] = " ".join(parts)
    return paths


def reset_cache() -> None:
    """Test-only: drop cached map."""
    _raw_geo.cache_clear()
    _bounds.cache_clear()
    get_prefecture_paths.cache_clear()
