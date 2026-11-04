"""Map rendering: GeoJSON -> PNG (matplotlib + optional OSM basemap) and -> standalone Leaflet HTML."""

from __future__ import annotations

import io
import json
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402
from PIL import Image  # noqa: E402
from shapely.geometry import shape  # noqa: E402

from .errors import MapsqlError

PALETTE = ["#2563eb", "#e11d48", "#16a34a", "#f59e0b", "#7c3aed", "#0891b2", "#db2777", "#4d7c0f"]
R = 6378137.0
MAX_LAT = 85.0511287798
TILE_SIZE = 256
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "mapsql-mcp/0.1 (+https://github.com/mveyseldelibas/mapsql-mcp; MCP server rendering static maps)"
MAX_FEATURES = 20000
_tile_lock = threading.Lock()


@dataclass
class MapLayer:
    name: str
    features: list[dict]
    color: str | None = None
    label_property: str | None = None
    #: numeric property used for a graduated (choropleth-style) fill
    value_property: str | None = None


@dataclass
class RenderInfo:
    png_path: str | None = None
    html_path: str | None = None
    basemap: str = "none"
    features: int = 0
    layers: int = 0
    bbox: list[float] | None = None
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ projection


def lonlat_to_merc(lon: np.ndarray | float, lat: np.ndarray | float):
    lon = np.asarray(lon, dtype=float)
    lat = np.clip(np.asarray(lat, dtype=float), -MAX_LAT, MAX_LAT)
    x = R * np.radians(lon)
    y = R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
    return x, y


def merc_to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon = math.degrees(x / R)
    lat = math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2)
    return lon, lat


def _proj_coords(coords):
    arr = np.asarray(coords, dtype=float)
    x, y = lonlat_to_merc(arr[:, 0], arr[:, 1])
    return np.column_stack([x, y])


# ------------------------------------------------------------------ geometry iteration


def _iter_shapes(geom: dict):
    try:
        g = shape(geom)
    except Exception:
        return
    stack = [g]
    while stack:
        cur = stack.pop()
        if cur.is_empty:
            continue
        if cur.geom_type in ("Point", "LineString", "Polygon"):
            yield cur
        elif hasattr(cur, "geoms"):
            stack.extend(cur.geoms)


def _polygon_path(poly) -> MplPath:
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        pts = _proj_coords(list(ring.coords))
        verts.extend(pts)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(pts) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(np.array(verts), codes)


def collect_bounds(layers: list[MapLayer]) -> tuple[float, float, float, float] | None:
    from .geo import geom_bounds

    b = None
    for layer in layers:
        for f in layer.features:
            gb = geom_bounds(f.get("geometry"))
            if gb is None:
                continue
            b = list(gb) if b is None else [min(b[0], gb[0]), min(b[1], gb[1]), max(b[2], gb[2]), max(b[3], gb[3])]
    return tuple(b) if b else None  # type: ignore[return-value]


# ------------------------------------------------------------------ tiles


def _tile_xy(lon: float, lat: float, z: int) -> tuple[float, float]:
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    n = 2**z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def _tile_bounds_merc(tx: int, ty: int, z: int) -> tuple[float, float, float, float]:
    n = 2**z
    world = 2 * math.pi * R
    x0 = -world / 2 + tx / n * world
    x1 = -world / 2 + (tx + 1) / n * world
    y1 = world / 2 - ty / n * world
    y0 = world / 2 - (ty + 1) / n * world
    return x0, y0, x1, y1


def _fetch_tile(url: str, cache_file: Path | None, timeout: float = 6.0) -> Image.Image | None:
    if cache_file is not None and cache_file.exists():
        try:
            return Image.open(cache_file).convert("RGB")
        except Exception:
            pass
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return None
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        if cache_file is not None:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(r.content)
            except OSError:
                pass
        return img
    except Exception:
        return None


def build_basemap(
    bbox_merc: tuple[float, float, float, float], width_px: int, template: str, cache_dir: Path | None
) -> tuple[Image.Image, tuple[float, float, float, float]] | None:
    x0, y0, x1, y1 = bbox_merc
    world = 2 * math.pi * R
    span_x = x1 - x0
    z = int(math.floor(math.log2(width_px * world / (TILE_SIZE * span_x)))) if span_x > 0 else 17
    z = max(0, min(17, z))
    lon0, lat1 = merc_to_lonlat(x0, y1)
    lon1, lat0 = merc_to_lonlat(x1, y0)
    while True:
        tx0, ty0 = _tile_xy(lon0, lat1, z)
        tx1, ty1 = _tile_xy(lon1, lat0, z)
        ix0, ix1 = int(math.floor(tx0)), int(math.floor(tx1))
        iy0, iy1 = int(math.floor(ty0)), int(math.floor(ty1))
        n_tiles = (ix1 - ix0 + 1) * (iy1 - iy0 + 1)
        if n_tiles <= 16 or z <= 0:
            break
        z -= 1
    n = 2**z
    jobs = []
    for ty in range(max(iy0, 0), min(iy1, n - 1) + 1):
        for tx in range(ix0, ix1 + 1):
            url = template.format(z=z, x=tx % n, y=ty)
            cf = cache_dir / f"{z}_{tx % n}_{ty}.png" if cache_dir else None
            jobs.append((tx, ty, url, cf))
    if not jobs:
        return None
    mosaic = Image.new("RGB", ((ix1 - ix0 + 1) * TILE_SIZE, (min(iy1, n - 1) - max(iy0, 0) + 1) * TILE_SIZE), (233, 233, 233))
    ok = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda j: _fetch_tile(j[2], j[3]), jobs))
    for (tx, ty, _u, _c), img in zip(jobs, results):
        if img is None:
            continue
        ok += 1
        mosaic.paste(img.resize((TILE_SIZE, TILE_SIZE)), ((tx - ix0) * TILE_SIZE, (ty - max(iy0, 0)) * TILE_SIZE))
    if ok == 0:
        return None
    bx0, _, _, by1 = _tile_bounds_merc(ix0, max(iy0, 0), z)
    _, by0, bx1, _ = _tile_bounds_merc(ix1, min(iy1, n - 1), z)
    return mosaic, (bx0, bx1, by0, by1)


# ------------------------------------------------------------------ PNG


def _auto_label(layer: MapLayer) -> str | None:
    if layer.label_property:
        return layer.label_property
    if len(layer.features) <= 40:
        for key in ("name", "label", "title", "NAME", "Name"):
            if any(f.get("properties", {}).get(key) for f in layer.features):
                return key
    return None


def render_png(
    layers: list[MapLayer],
    *,
    title: str | None = None,
    width: int = 1000,
    height: int = 700,
    basemap: str = "osm",
    cache_dir: Path | None = None,
) -> tuple[bytes, RenderInfo]:
    width = max(300, min(int(width), 2400))
    height = max(300, min(int(height), 2400))
    info = RenderInfo(layers=len(layers))
    layers = [MapLayer(l.name, l.features[:MAX_FEATURES], l.color, l.label_property, l.value_property) for l in layers]
    info.features = sum(len(l.features) for l in layers)
    b = collect_bounds(layers)
    if b is None:
        raise MapsqlError("Nothing to draw: the inputs contain no geometries.")
    minx, miny, maxx, maxy = b
    info.bbox = [round(v, 6) for v in b]

    # pad & enforce a minimum span (single points)
    padx = max((maxx - minx) * 0.08, 0.0015)
    pady = max((maxy - miny) * 0.08, 0.0015)
    minx, maxx, miny, maxy = minx - padx, maxx + padx, miny - pady, maxy + pady
    (mx0, mx1), (my0, my1) = (
        tuple(float(v) for v in lonlat_to_merc(np.array([minx, maxx]), np.array([miny, miny]))[0]),
        tuple(float(v) for v in lonlat_to_merc(np.array([minx, minx]), np.array([miny, maxy]))[1]),
    )
    # match the figure aspect ratio
    aspect = width / height
    cx, cy = (mx0 + mx1) / 2, (my0 + my1) / 2
    w, h = mx1 - mx0, my1 - my0
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    mx0, mx1, my0, my1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2

    dpi = 100
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(mx0, mx1)
    ax.set_ylim(my0, my1)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    fig.patch.set_facecolor("#f3f4f6")

    # basemap
    attribution = None
    if basemap and basemap != "none":
        template = basemap if "{z}" in basemap else OSM_TILES
        bm = None
        try:
            bm = build_basemap((mx0, my0, mx1, my1), width, template, cache_dir)
        except Exception as e:  # network trouble must never break map rendering
            info.notes.append(f"basemap failed: {type(e).__name__}")
        if bm is not None:
            img, (bx0, bx1, by0, by1) = bm
            ax.imshow(np.asarray(img), extent=(bx0, bx1, by0, by1), origin="upper", zorder=0, interpolation="bilinear")
            ax.set_xlim(mx0, mx1)
            ax.set_ylim(my0, my1)
            info.basemap = "osm" if template == OSM_TILES else "custom"
            attribution = "© OpenStreetMap contributors" if template == OSM_TILES else None
        else:
            info.notes.append("Basemap unavailable (offline or tile server refused); drew layers on a plain background.")
    if info.basemap == "none":
        ax.set_facecolor("#f9fafb")
        ax.patch.set_visible(True)

    path_fx = [pe.withStroke(linewidth=3, foreground="white")]
    choropleth = None
    label_budget = 60
    legend_items = []
    for li, layer in enumerate(layers):
        color = layer.color or PALETTE[li % len(PALETTE)]
        legend_items.append((layer.name, color, len(layer.features)))
        n_feat = len(layer.features)
        pts_x: list[float] = []
        pts_y: list[float] = []
        label_prop = _auto_label(layer)
        labels: list[tuple[float, float, str, float]] = []
        lw_line = 2.2 if n_feat <= 200 else 1.2
        cmap = norm = None
        if layer.value_property:
            vals = [
                float(v)
                for f in layer.features
                if isinstance((v := f.get("properties", {}).get(layer.value_property)), (int, float)) and not isinstance(v, bool)
            ]
            if vals:
                from matplotlib import colors

                cmap = matplotlib.colormaps["YlOrRd"]
                lo, hi = min(vals), max(vals)
                norm = colors.Normalize(vmin=lo, vmax=hi if hi > lo else lo + 1)
                choropleth = (layer.value_property, cmap, norm)
            else:
                info.notes.append(f"value_property {layer.value_property!r} has no numeric values; using a flat colour.")
        for f in layer.features:
            g = f.get("geometry")
            if not g:
                continue
            fill = color
            fill_alpha = 0.38 if n_feat < 400 else 0.3
            if cmap is not None:
                v = f.get("properties", {}).get(layer.value_property)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    fill = cmap(norm(float(v)))
                    fill_alpha = 0.78
                else:
                    fill, fill_alpha = "#d1d5db", 0.5
            text = None
            if label_prop:
                v = f.get("properties", {}).get(label_prop)
                text = None if v is None else str(v)
            best_poly = None
            for part in _iter_shapes(g):
                if part.geom_type == "Point":
                    x, y = lonlat_to_merc(part.x, part.y)
                    pts_x.append(float(x))
                    pts_y.append(float(y))
                    if text:
                        labels.append((float(x), float(y), text, 0.0))
                elif part.geom_type == "LineString":
                    xy = _proj_coords(list(part.coords))
                    ax.plot(xy[:, 0], xy[:, 1], color=color, linewidth=lw_line, solid_capstyle="round", zorder=3 + li * 0.1)
                else:
                    patch = PathPatch(
                        _polygon_path(part), facecolor=fill, edgecolor="none", alpha=fill_alpha,
                        linewidth=0, zorder=2 + li * 0.1,
                    )
                    ax.add_patch(patch)
                    outline = PathPatch(
                        _polygon_path(part), facecolor="none", edgecolor="#6b7280" if cmap is not None else color,
                        linewidth=0.8 if cmap is not None else 1.2, zorder=2.5 + li * 0.1,
                    )
                    ax.add_patch(outline)
                    if text and (best_poly is None or part.area > best_poly.area):
                        best_poly = part
            if best_poly is not None and text:
                rp = best_poly.representative_point()
                x, y = lonlat_to_merc(rp.x, rp.y)
                pv = f.get("properties", {}).get(layer.value_property) if layer.value_property else None
                labels.append((float(x), float(y), text, float(pv) if isinstance(pv, (int, float)) else 0.0))
        if pts_x:
            size = 46 if len(pts_x) <= 150 else (22 if len(pts_x) <= 1000 else 9)
            ax.scatter(pts_x, pts_y, s=size, c=color, edgecolors="white", linewidths=0.9, zorder=5 + li * 0.1)
        if cmap is not None:  # choropleth: label only the highest values to avoid clutter
            labels = sorted(labels, key=lambda t: -t[3])[:15]
        for x, y, text, _v in labels[: min(40, label_budget)]:
            if not (mx0 <= x <= mx1 and my0 <= y <= my1):
                continue
            t = text if len(text) <= 28 else text[:27] + "…"
            ax.annotate(
                t, (x, y), xytext=(6, 6), textcoords="offset points", fontsize=8.5, color="#111827",
                path_effects=path_fx, zorder=8, annotation_clip=True,
            )
        label_budget -= min(40, len(labels))

    # title + legend + attribution
    if title:
        ax.text(
            0.012, 0.985, title, transform=ax.transAxes, va="top", ha="left", fontsize=12.5, fontweight="bold",
            color="#111827", zorder=10,
            bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor="#d1d5db", alpha=0.92),
        )
    if len(legend_items) > 1 or (legend_items and not title):
        from matplotlib.lines import Line2D

        handles = [
            Line2D([0], [0], marker="s", linestyle="", markersize=9, markerfacecolor=c, markeredgecolor=c, alpha=0.9,
                   label=n if "(" in n else f"{n} ({k})")
            for n, c, k in legend_items
        ]
        leg = ax.legend(handles=handles, loc="lower left", fontsize=9, frameon=True, framealpha=0.92, edgecolor="#d1d5db")
        leg.set_zorder(10)
    if choropleth is not None:
        prop, cmap_, norm_ = choropleth
        cax = ax.inset_axes([0.78, 0.085, 0.2, 0.022])
        cb = fig.colorbar(cm_scalar(cmap_, norm_), cax=cax, orientation="horizontal")
        cb.ax.tick_params(labelsize=8)
        cb.set_label(prop, fontsize=8.5)
        cax.set_zorder(12)
    if attribution:
        ax.text(
            0.995, 0.006, attribution, transform=ax.transAxes, va="bottom", ha="right", fontsize=8, color="#374151",
            zorder=10, bbox=dict(boxstyle="square,pad=0.2", facecolor="white", edgecolor="none", alpha=0.8),
        )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue(), info


def cm_scalar(cmap, norm):
    from matplotlib.cm import ScalarMappable

    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    return sm


# ------------------------------------------------------------------ HTML (Leaflet)

_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
html,body,#map{height:100%;margin:0}
.title{position:absolute;z-index:1000;top:10px;left:56px;background:rgba(255,255,255,.93);padding:6px 12px;border-radius:6px;
font:600 15px system-ui,sans-serif;box-shadow:0 1px 4px rgba(0,0,0,.3)}
.leaflet-popup-content table{border-collapse:collapse;font:12px system-ui,sans-serif}
.leaflet-popup-content td{padding:2px 8px 2px 0;vertical-align:top}
.leaflet-popup-content td:first-child{color:#555}
</style></head>
<body><div id="map"></div><div class="title">__TITLE__</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const LAYERS = __DATA__;
const map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
function popup(props){
  const t = document.createElement('table');
  Object.entries(props || {}).slice(0, 30).forEach(([k, v]) => {
    const r = t.insertRow(); r.insertCell().textContent = k; r.insertCell().textContent = (v === null ? '' : String(v));
  });
  return t;
}
const overlays = {}; let bounds = null;
LAYERS.forEach(l => {
  let lo = Infinity, hi = -Infinity;
  if (l.value) l.features.forEach(f => { const v = f.properties && f.properties[l.value]; if (typeof v === 'number') { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
  const ramp = ['#ffffcc', '#ffeda0', '#fed976', '#feb24c', '#fd8d3c', '#fc4e2a', '#e31a1c', '#b10026'];
  const fillFor = f => {
    const v = l.value && f.properties ? f.properties[l.value] : null;
    if (typeof v !== 'number' || !isFinite(lo)) return l.color;
    return ramp[Math.min(ramp.length - 1, Math.floor((v - lo) / ((hi - lo) || 1) * ramp.length))];
  };
  const style = f => ({color: l.value ? '#555' : l.color, weight: l.value ? 1 : 2, fillColor: fillFor(f), fillOpacity: l.value ? 0.75 : 0.3});
  const gj = L.geoJSON({type: 'FeatureCollection', features: l.features}, {
    style: style,
    pointToLayer: (f, ll) => L.circleMarker(ll, {radius: 6, color: '#fff', weight: 1.5, fillColor: fillFor(f), fillOpacity: 0.95}),
    onEachFeature: (f, lyr) => { if (f.properties && Object.keys(f.properties).length) lyr.bindPopup(() => popup(f.properties), {maxHeight: 260}); }
  }).addTo(map);
  overlays[l.name + ' (' + l.features.length + ')'] = gj;
  const b = gj.getBounds(); if (b.isValid()) bounds = bounds ? bounds.extend(b) : b;
});
if (LAYERS.length > 1) L.control.layers(null, overlays, {collapsed: false}).addTo(map);
if (bounds) map.fitBounds(bounds.pad(0.08)); else map.setView([0, 0], 2);
</script></body></html>
"""


def render_html(layers: list[MapLayer], title: str | None) -> str:
    data = [
        {
            "name": l.name,
            "color": l.color or PALETTE[i % len(PALETTE)],
            "value": l.value_property,
            "features": [
                {"type": "Feature", "properties": f.get("properties", {}), "geometry": f.get("geometry")}
                for f in l.features[:MAX_FEATURES]
                if f.get("geometry")
            ],
        }
        for i, l in enumerate(layers)
    ]
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # keep the JSON from ever terminating the <script> element
    payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    safe_title = (title or "mapsql-mcp map").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _HTML.replace("__TITLE__", safe_title).replace("__DATA__", payload)


def safe_filename(stem: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:60]
    return s or "map"
