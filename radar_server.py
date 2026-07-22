"""
Radar data fetcher and tile renderer for image generation.
"""

import os, io, re, time, math, shutil
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import numpy as np
import h5py
import requests
from PIL import Image
from pyproj import Geod

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

STATIONS = {
    "czbrd": {"name": "Brdicky", "short": "BRD", "country": "cz", "lat": 49.6583, "lon": 13.8178},
    "czska": {"name": "Skalky", "short": "SKA", "country": "cz", "lat": 49.5011, "lon": 16.7885},
}

COUNTRY_CONFIG = {
    "cz": {"dir": "PVOL", "separate_elevations": False},
}

VARIABLES = {
    "DBZH": {"label": "Reflectivity", "unit": "dBZ", "vmin": 0, "vmax": 70},
    "VRADH": {"label": "Radial Velocity", "unit": "m/s", "vmin": -50, "vmax": 50},
    "TH": {"label": "Raw Reflectivity", "unit": "dBZ", "vmin": 0, "vmax": 70},
    "VRAD": {"label": "Radial Velocity", "unit": "m/s", "vmin": -50, "vmax": 50, "alias": "VRADH"},
    "ZDR": {"label": "Differential Reflectivity", "unit": "dB", "vmin": -2, "vmax": 5},
    "CC": {"label": "Correlation Coefficient", "unit": "", "vmin": 0.2, "vmax": 1.0},
    "W": {"label": "Spectral Width", "unit": "m/s", "vmin": 0, "vmax": 10},
    "PHIDP": {"label": "Differential Phase", "unit": "°", "vmin": 0, "vmax": 360},
}

_inmem_cache = {}
INMEM_TTL = 120
_elevation_cache = {}
ELEVATION_CACHE_TTL = 600

_geod = Geod(ellps="WGS84")


def _latlon_to_tile(lat, lon, zoom):
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def _cache_path(key):
    return os.path.join(CACHE_DIR, re.sub(r'[^a-zA-Z0-9._-]', '_', key))


def _free_space_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return 512 * 1024 * 1024


def _cleanup_cache_if_low_space(path=CACHE_DIR, min_free=512*1024*1024):
    if _free_space_bytes(path) >= min_free:
        return
    for root, _, files in os.walk(path):
        for name in files:
            if name.endswith(".tmp"):
                try:
                    os.remove(os.path.join(root, name))
                except Exception:
                    pass
    candidates = []
    for root, _, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                candidates.append((os.path.getmtime(fp), fp))
            except Exception:
                pass
    candidates.sort()
    for _, fp in candidates:
        if _free_space_bytes(path) >= min_free:
            break
        try:
            os.remove(fp)
        except Exception:
            pass


def _station_country(station):
    info = STATIONS.get(station)
    return info["country"] if info else "cz"


def _country_code(station):
    info = STATIONS.get(station)
    return info["country"].upper() if info else "CZ"


def _get_station_separate(station_id):
    key = f"sep:{station_id}"
    now = time.time()
    entry = _elevation_cache.get(key)
    if entry and now - entry["time"] < ELEVATION_CACHE_TTL:
        return entry["separate"]
    return None


def _is_valid_elevation(elevation):
    try:
        v = float(elevation.replace(",", "."))
        return not math.isnan(v) and not math.isinf(v)
    except (ValueError, TypeError):
        return False


def parse_file(filepath):
    with h5py.File(filepath, "r") as f:
        site_lat = float(f["where"].attrs["lat"])
        site_lon = float(f["where"].attrs["lon"])
        datasets = []
        for i in range(1, 25):
            g = f"dataset{i}"
            if g not in f:
                break
            where = f[f"{g}/where"].attrs
            data_what = f[f"{g}/data1/what"].attrs
            elangle = float(where["elangle"])
            if math.isnan(elangle) or math.isinf(elangle):
                continue
            nbins = int(where["nbins"])
            nrays = int(where["nrays"])
            rscale = float(where["rscale"])
            rstart = float(where.get("rstart", 0))
            gain = float(data_what.get("gain", 1.0))
            offset = float(data_what.get("offset", 0.0))
            nodata_val = float(data_what.get("nodata", 255))
            undetect_val = float(data_what.get("undetect", 0))
            data_raw = f[f"{g}/data1/data"][:]
            data_f = data_raw.astype(np.float32)
            mask = (data_raw == nodata_val) | (data_raw == undetect_val)
            data_f = data_f * gain + offset
            data_f[mask] = np.nan
            if f"{g}/how" in f and "startazA" in f[f"{g}/how"].attrs:
                start_az = f[f"{g}/how"].attrs["startazA"][:].astype(np.float32)
                if len(start_az) != nrays:
                    step = 360.0 / nrays
                    a1gate_val = int(where.get("a1gate", 0))
                    az = (np.arange(nrays, dtype=np.float32) * step + a1gate_val) % 360
                else:
                    if "stopazA" in f[f"{g}/how"].attrs:
                        az = f[f"{g}/how"].attrs["stopazA"][:].astype(np.float32) % 360
                    else:
                        az = start_az % 360
            else:
                step = 360.0 / nrays
                a1gate_val = int(where.get("a1gate", 0))
                az = (np.arange(nrays, dtype=np.float32) * step + a1gate_val) % 360
            rng = np.arange(nbins, dtype=np.float32) * rscale + rstart
            datasets.append({
                "elevation": elangle,
                "azimuths": az,
                "ranges": rng,
                "data": data_f,
                "nrays": nrays,
                "nbins": nbins,
                "rscale": rscale,
            })
    return {"lat": site_lat, "lon": site_lon, "datasets": datasets}


def _index_scan(az, rng, data, fwd_az, dist, method="nearest"):
    nrays = len(az)
    step = 360.0 / nrays
    a0 = az[0]
    if method == "linear":
        ray_frac = ((fwd_az - a0) % 360) / step
        ray0 = np.floor(ray_frac).astype(np.int32) % nrays
        ray1 = (ray0 + 1) % nrays
        ry = (ray_frac - np.floor(ray_frac)).astype(np.float32)
        r0 = rng[0]
        rstep = rng[1] - r0
        bin_frac = (dist - r0) / rstep
        bin0 = np.floor(bin_frac).astype(np.int32)
        bin1 = bin0 + 1
        bx = (bin_frac - np.floor(bin_frac)).astype(np.float32)
        last = len(rng) - 1
        bin0c = np.clip(bin0, 0, last)
        bin1c = np.clip(bin1, 0, last)
        c00 = data[ray0, bin0c]
        c10 = data[ray1, bin0c]
        c01 = data[ray0, bin1c]
        c11 = data[ray1, bin1c]
        w0 = (1 - bx) * c00 + bx * c01
        w1 = (1 - bx) * c10 + bx * c11
        values = (1 - ry) * w0 + ry * w1
    else:
        ray_idx = np.round(((fwd_az - a0) % 360) / step).astype(np.int32) % nrays
        r0 = rng[0]
        rstep = rng[1] - r0
        bin_idx = np.round((dist - r0) / rstep).astype(np.int32)
        np.clip(bin_idx, 0, len(rng) - 1, out=bin_idx)
        values = data[ray_idx, bin_idx].copy()
    values[(dist > rng[-1]) | (dist < 0)] = np.nan
    return values


REFLECTIVITY_CMAP = [
    (0.000, (0.6, 0.85, 1.0, 0.70)),
    (0.071, (0.0, 0.863, 0.863, 0.471)),
    (0.143, (0.0, 0.706, 1.0, 0.627)),
    (0.214, (0.0, 0.549, 0.275, 0.706)),
    (0.286, (0.314, 0.784, 0.235, 0.784)),
    (0.357, (0.667, 0.863, 0.235, 0.863)),
    (0.429, (1.0, 1.0, 0.0, 0.902)),
    (0.500, (1.0, 0.706, 0.0, 0.941)),
    (0.571, (1.0, 0.392, 0.0, 0.961)),
    (0.643, (1.0, 0.0, 0.0, 0.980)),
    (0.714, (0.784, 0.0, 0.0, 0.980)),
    (0.786, (0.588, 0.0, 0.314, 0.980)),
    (0.857, (0.784, 0.392, 0.784, 0.980)),
    (0.929, (1.0, 1.0, 1.0, 1.0)),
    (1.000, (1.0, 1.0, 1.0, 1.0)),
]

VELOCITY_CMAP = [
    (0.000, (0.518, 0.910, 0.922, 1.0)),
    (0.205, (0.518, 0.910, 0.922, 1.0)),
    (0.227, (0.616, 0.918, 0.929, 1.0)),
    (0.268, (0.392, 0.922, 0.522, 1.0)),
    (0.294, (0.031, 0.902, 0.035, 1.0)),
    (0.335, (0.067, 0.835, 0.067, 1.0)),
    (0.370, (0.145, 0.718, 0.145, 1.0)),
    (0.402, (0.145, 0.718, 0.145, 1.0)),
    (0.420, (0.251, 0.557, 0.251, 1.0)),
    (0.437, (0.294, 0.494, 0.290, 1.0)),
    (0.469, (0.392, 0.467, 0.369, 1.0)),
    (0.487, (0.490, 0.463, 0.447, 1.0)),
    (0.496, (0.529, 0.435, 0.455, 1.0)),
    (0.504, (0.518, 0.376, 0.396, 1.0)),
    (0.513, (0.506, 0.322, 0.337, 1.0)),
    (0.531, (0.459, 0.133, 0.141, 1.0)),
    (0.563, (0.435, 0.008, 0.012, 1.0)),
    (0.580, (0.553, 0.043, 0.071, 1.0)),
    (0.598, (0.608, 0.067, 0.102, 1.0)),
    (0.630, (0.678, 0.090, 0.141, 1.0)),
    (0.665, (0.824, 0.149, 0.220, 1.0)),
    (0.706, (0.980, 0.282, 0.396, 1.0)),
    (0.732, (0.980, 0.400, 0.529, 1.0)),
    (0.773, (0.988, 0.576, 0.737, 1.0)),
    (0.795, (0.992, 0.675, 0.757, 1.0)),
    (1.000, (0.992, 0.675, 0.757, 1.0)),
]

ZDR_CMAP = [
    (0.000, (0.078, 0.078, 0.392, 1.0)),
    (0.286, (0.118, 0.235, 0.627, 1.0)),
    (0.429, (0.235, 0.471, 0.784, 1.0)),
    (0.571, (0.706, 0.706, 0.863, 1.0)),
    (0.714, (1.0, 0.863, 0.706, 1.0)),
    (0.857, (1.0, 0.588, 0.235, 1.0)),
    (1.000, (0.784, 0.196, 0.078, 1.0)),
]

CC_CMAP = [
    (0.000, (0.196, 0.196, 0.196, 1.0)),
    (0.250, (0.118, 0.275, 0.353, 1.0)),
    (0.500, (0.157, 0.392, 0.510, 1.0)),
    (0.750, (0.235, 0.549, 0.706, 1.0)),
    (0.812, (0.314, 0.667, 0.784, 1.0)),
    (0.875, (0.471, 0.784, 0.863, 1.0)),
    (0.938, (0.706, 0.902, 0.941, 1.0)),
    (1.000, (0.941, 0.980, 1.0, 1.0)),
]

W_CMAP = [
    (0.000, (0.039, 0.039, 0.039, 1.0)),
    (0.100, (0.078, 0.078, 0.392, 1.0)),
    (0.250, (0.118, 0.235, 0.627, 1.0)),
    (0.400, (0.235, 0.471, 0.784, 1.0)),
    (0.500, (0.471, 0.784, 0.863, 1.0)),
    (0.600, (0.706, 0.902, 0.941, 1.0)),
    (0.700, (1.0, 0.863, 0.706, 1.0)),
    (0.800, (1.0, 0.588, 0.235, 1.0)),
    (0.900, (0.784, 0.196, 0.078, 1.0)),
    (1.000, (0.502, 0.039, 0.039, 1.0)),
]

PHIDP_CMAP = [
    (0.000, (0.196, 0.196, 0.196, 1.0)),
    (0.125, (0.039, 0.118, 0.627, 1.0)),
    (0.250, (0.039, 0.627, 0.118, 1.0)),
    (0.375, (0.627, 0.627, 0.039, 1.0)),
    (0.500, (0.627, 0.039, 0.118, 1.0)),
    (0.625, (0.627, 0.039, 0.627, 1.0)),
    (0.750, (0.039, 0.627, 0.627, 1.0)),
    (0.875, (0.627, 0.118, 0.039, 1.0)),
    (1.000, (0.627, 0.039, 0.039, 1.0)),
]


def build_colormap_lut(cmap_def, steps=256):
    stops = np.array([s[0] for s in cmap_def])
    colors = np.array([s[1] for s in cmap_def])
    x = np.linspace(0, 1, steps)
    lut = np.zeros((steps, 4))
    for c in range(4):
        lut[:, c] = np.interp(x, stops, colors[:, c])
    return (lut * 255).astype(np.uint8)


REFLECTIVITY_CMAP_2 = [
    (0.000, (0.702, 0.698, 0.698, 1.0)),
    (0.071, (0.702, 0.698, 0.698, 1.0)),
    (0.114, (0.392, 0.478, 0.659, 1.0)),
    (0.143, (0.251, 0.373, 0.631, 1.0)),
    (0.214, (0.353, 0.671, 0.831, 1.0)),
    (0.286, (0.118, 0.714, 0.290, 1.0)),
    (0.357, (0.008, 0.565, 0.039, 1.0)),
    (0.429, (0.0, 0.467, 0.020, 1.0)),
    (0.500, (0.051, 0.329, 0.0, 1.0)),
    (0.529, (0.545, 0.596, 0.0, 1.0)),
    (0.571, (0.969, 0.824, 0.0, 1.0)),
    (0.643, (1.0, 0.588, 0.0, 1.0)),
    (0.714, (0.937, 0.082, 0.004, 1.0)),
    (0.786, (0.804, 0.067, 0.008, 1.0)),
    (0.857, (0.596, 0.051, 0.0, 1.0)),
    (0.929, (0.271, 0.020, 0.004, 1.0)),
    (1.000, (0.886, 0.596, 0.882, 1.0)),
]
DBZH_LUT = build_colormap_lut(REFLECTIVITY_CMAP)
DBZH_LUT_2 = build_colormap_lut(REFLECTIVITY_CMAP_2)
VRADH_LUT = build_colormap_lut(VELOCITY_CMAP)
ZDR_LUT = build_colormap_lut(ZDR_CMAP)
CC_LUT = build_colormap_lut(CC_CMAP)
W_LUT = build_colormap_lut(W_CMAP)
PHIDP_LUT = build_colormap_lut(PHIDP_CMAP)


def render_tile(parsed, elevation_idx, z, x, y, var="DBZH", size=256, colorscheme="1",
                vmin=None, vmax=None, vmin_cut=None):
    n = 2 ** z
    max_global = n * size
    px = np.arange(x * size, (x + 1) * size) + 0.5
    py = np.arange(y * size, (y + 1) * size) + 0.5
    px_mesh, py_mesh = np.meshgrid(px, py)
    world_x = px_mesh.astype(np.float64) / max_global
    world_y = py_mesh.astype(np.float64) / max_global
    lon = world_x * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi - 2.0 * np.pi * world_y)))
    ds = parsed["datasets"][elevation_idx]
    site_lat, site_lon = parsed["lat"], parsed["lon"]
    fwd_az, _, dist = _geod.inv(
        np.full(lat.size, site_lon),
        np.full(lat.size, site_lat),
        lon.ravel(), lat.ravel()
    )
    values = _index_scan(ds["azimuths"], ds["ranges"], ds["data"],
                         np.asarray(fwd_az) % 360, dist)
    values = values.reshape(lat.shape)
    if vmin_cut is not None:
        values[values < vmin_cut] = np.nan
    if vmin is None:
        vmin = VARIABLES[var]["vmin"]
    if vmax is None:
        vmax = VARIABLES[var]["vmax"]
    norm = (values - vmin) / (vmax - vmin)
    mask = np.isnan(norm)
    norm = np.clip(norm, 0, 1)
    norm[mask] = 0
    idx = (norm * 255).astype(np.uint32)
    is_refl = var in ("DBZH", "TH")
    if var == "ZDR":
        lut = ZDR_LUT
    elif var == "CC":
        lut = CC_LUT
    elif var == "W":
        lut = W_LUT
    elif var == "PHIDP":
        lut = PHIDP_LUT
    elif is_refl:
        lut = DBZH_LUT_2 if colorscheme == "2" else DBZH_LUT
    else:
        lut = VRADH_LUT
    rgb = lut[idx]
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    for c in range(4):
        rgba[:, :, c] = rgb[:, :, c]
    rgba[mask] = [0, 0, 0, 0]
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


CHMI_BASE = "https://opendata.chmi.cz/meteorology/weather/radar/sites"

CHMI_STATIONS = {
    "czbrd": "brd",
    "czska": "ska",
}

CHMI_PRODUCT_MAP = {
    "DBZH": "z",
    "VRADH": "v",
    "VRAD": "v",
    "ZDR": "zdr",
    "CC": "rhohv",
    "W": "w",
    "PHIDP": "phidp",
}


class _ChmiLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, val in attrs:
                if name == "href" and val.endswith(".hdf"):
                    self.links.append(val)


def chmi_list_files(station_id, product):
    chmi_code = CHMI_STATIONS.get(station_id)
    if not chmi_code:
        return []
    chmi_prod = CHMI_PRODUCT_MAP.get(product)
    if not chmi_prod:
        return []
    url = f"{CHMI_BASE}/{chmi_code}/vol_{chmi_prod}/hdf5/"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        parser = _ChmiLinkParser()
        parser.feed(resp.text)
        return parser.links
    except Exception:
        return []


def chmi_latest_key(station_id, product, target_time=None):
    files = chmi_list_files(station_id, product)
    if not files:
        return None
    if target_time is None:
        files.sort(reverse=True)
        return files[0]
    def _chmi_ts_diff(fname):
        m = re.search(r'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', fname or "")
        if not m:
            return float('inf')
        dt = datetime(int(m[1]), int(m[2]), int(m[3]),
                      int(m[4]), int(m[5]), int(m[6]), tzinfo=timezone.utc)
        return abs((dt - target_time).total_seconds())
    files.sort(key=_chmi_ts_diff)
    return files[0]


def _parse_timestamp(fname):
    m = re.search(r'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', fname or "")
    if not m:
        return None
    return datetime(int(m[1]), int(m[2]), int(m[3]),
                    int(m[4]), int(m[5]), int(m[6]), tzinfo=timezone.utc)


def chmi_fetch_filename(station_id, product, filename):
    """Download + parse a specific CHMI volume file (cached on disk)."""
    chmi_code = CHMI_STATIONS.get(station_id)
    chmi_prod = CHMI_PRODUCT_MAP.get(product)
    if not chmi_code or not chmi_prod or not filename:
        return None
    url = f"{CHMI_BASE}/{chmi_code}/vol_{chmi_prod}/hdf5/{filename}"
    cache_key = f"chmi_{station_id}_{product}_{filename}"
    cache_fp = _cache_path(cache_key)
    if os.path.exists(cache_fp):
        try:
            return parse_file(cache_fp)
        except Exception:
            os.remove(cache_fp)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    tmp = cache_fp + ".tmp"
    with open(tmp, "wb") as f:
        f.write(resp.content)
    os.replace(tmp, cache_fp)
    try:
        return parse_file(cache_fp)
    except Exception:
        return None


def chmi_fetch_and_parse(station_id, product, target_time=None):
    filename = chmi_latest_key(station_id, product, target_time)
    return chmi_fetch_filename(station_id, product, filename)


def chmi_best_volume_key(station_id, product, target_time=None):
    """Pick the volume file nearest in time that carries the MOST elevation
    sweeps. CHMI publishes DBZH both as a single 1.5 deg sweep and as a full
    multi-angle volume (0.1-21.6); the latest file is usually the 1.5 deg
    sweep, so this selects the full-volume file instead when available."""
    files = chmi_list_files(station_id, product)
    if not files:
        return None
    base = target_time or datetime.now(timezone.utc)
    files = sorted(files, key=lambda fn: (
        abs((_parse_timestamp(fn) - base).total_seconds())
        if _parse_timestamp(fn) else float("inf")))
    best_fn = None
    best_n = 0
    for fn in files[:12]:
        p = chmi_fetch_filename(station_id, product, fn)
        n = len(p["datasets"]) if p else 0
        if n >= 2 and n > best_n:
            best_n = n
            best_fn = fn
        if best_n >= 12:
            break
    return best_fn or files[0]
