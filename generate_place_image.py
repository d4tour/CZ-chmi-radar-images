"""
Generate a combined radar image (DBZH + VRADH, side-by-side) centered on a place name.
Legend bars at top, radar panels in middle, info bar at bottom.

Usage:
    python generate_place_image.py "Lubawa"
    python generate_place_image.py "London" --index 1
    python generate_place_image.py "Berlin" --scheme 2 --zoom 11
    python generate_place_image.py "Warsaw" --time 202606301010
"""

import os, sys, io, time, re, argparse, math
from datetime import datetime, timezone, timedelta
import concurrent.futures

import requests
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import types
_cell_detect = types.ModuleType("cell_detect")
def _noop(*args, **kwargs):
    return [], [], []
_cell_detect.process_frames = _noop
sys.modules["cell_detect"] = _cell_detect

from radar_server import (
    STATIONS, _geod, render_tile,
    _latlon_to_tile, _station_country, COUNTRY_CONFIG,
    _get_station_separate, VARIABLES,
    DBZH_LUT, DBZH_LUT_2, VRADH_LUT, ZDR_LUT, CC_LUT, W_LUT, PHIDP_LUT,
    chmi_fetch_and_parse, chmi_latest_key, chmi_best_volume_key,
    chmi_fetch_filename, CHMI_STATIONS
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "opencode-radar-image-generator/1.0"

CARTO_SUBDOMAINS = ["a", "b", "c"]
DARK_NOLABELS = "https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png"
DARK_LABELS   = "https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}.png"
LIGHT_NOLABELS = "https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}.png"
LIGHT_LABELS   = "https://{s}.basemaps.cartocdn.com/light_only_labels/{z}/{x}/{y}.png"
VOYAGER_NOLABELS = "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}.png"
VOYAGER_LABELS   = "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}.png"
ESRI_IMAGERY  = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"


# Each basemap: base tile + transparent overlay(s) composited ON TOP of the radar.
# overlay tuple = (url, subdomains_or_None, yx_order, boost_labels, whiteout, alpha)
BASEMAPS = {
    "dark": {
        "base": DARK_NOLABELS, "base_sub": CARTO_SUBDOMAINS, "base_yx": False,
        "overlays": [(DARK_LABELS, CARTO_SUBDOMAINS, False, True, False, 1.0)],
        "fallback": (30, 30, 35, 255),
    },
    "light": {
        "base": LIGHT_NOLABELS, "base_sub": CARTO_SUBDOMAINS, "base_yx": False,
        "overlays": [(LIGHT_LABELS, CARTO_SUBDOMAINS, False, False, False, 1.0)],
        "fallback": (245, 245, 240, 255),
    },
    "voyager": {
        "base": VOYAGER_NOLABELS, "base_sub": CARTO_SUBDOMAINS, "base_yx": False,
        "overlays": [(VOYAGER_LABELS, CARTO_SUBDOMAINS, False, False, False, 1.0)],
        "fallback": (245, 245, 240, 255),
    },
    "satellite": {
        "base": ESRI_IMAGERY, "base_sub": None, "base_yx": True,
        "overlays": [],
        "fallback": (0, 0, 0, 255),
    },
}


# Major CZ / neighbouring cities (name, lat, lon) for on-map labels.
# Only those falling inside the requested view are drawn.
CITIES = [
    ("Praha", 50.0755, 14.4378), ("Brno", 49.1951, 16.6068),
    ("Ostrava", 49.8209, 18.2625), ("Plzeň", 49.7465, 13.3779),
    ("Liberec", 50.7709, 15.0558), ("Olomouc", 49.5938, 17.2509),
    ("České Budějovice", 48.9747, 14.4743), ("Hradec Králové", 50.2066, 15.8332),
    ("Pardubice", 50.0343, 15.7813), ("Zlín", 49.2268, 17.6566),
    ("Ústí nad Labem", 50.6607, 14.0406), ("Karlovy Vary", 50.2322, 12.8712),
    ("Jihlava", 49.3961, 15.5903), ("Příbram", 49.6917, 14.0123),
    ("Tábor", 49.4145, 14.6572), ("Znojmo", 48.8559, 16.0486),
    ("Cheb", 50.0803, 12.3707), ("Děčín", 50.7721, 14.1869),
    ("Mladá Boleslav", 50.4123, 14.9033), ("Teplice", 50.6406, 13.8277),
    ("Chomutov", 50.4596, 13.4179), ("Jablonec nad Nisou", 50.7253, 15.1660),
    ("Prostějov", 49.4719, 17.1067), ("Kladno", 50.1467, 14.1017),
    ("Most", 50.5033, 13.6375), ("Opava", 49.9385, 17.9034),
    ("Frýdek-Místek", 49.6839, 18.3478), ("Havířov", 49.7845, 18.3901),
    ("Kroměříž", 49.3015, 17.3928), ("Kolín", 50.0275, 15.1990),
    ("Trutnov", 50.5855, 15.9107), ("Česká Lípa", 50.6880, 14.5373),
    ("Třinec", 49.6747, 18.6686), ("Hodonín", 48.8549, 17.1300),
    ("Břeclav", 48.7605, 16.8839), ("Vyškov", 49.2823, 17.0013),
    ("Blansko", 49.3417, 16.6481), ("Náchod", 50.4160, 16.1628),
    ("Písek", 49.3080, 14.1480), ("Strakonice", 49.2607, 13.9070),
    ("Český Krumlov", 48.8127, 14.3164), ("Klatovy", 49.4050, 13.2944),
    ("Rokycany", 49.7330, 13.6010), ("Beroun", 49.9620, 14.0720),
    ("Benešov", 49.7710, 14.6880), ("Litoměřice", 50.5320, 14.1300),
    ("Rakovník", 50.1060, 13.7360), ("Žďár nad Sázavou", 49.5620, 15.9420),
    ("Kutná Hora", 49.9480, 15.2670), ("Třebíč", 49.2150, 15.8770),
    ("Chrudim", 49.9520, 15.7920), ("Jičín", 50.4370, 15.3500),
    ("Šumperk", 49.9690, 16.9720), ("Přerov", 49.4550, 17.4490),
    # --- additional Czech towns ---
    ("Pelhřimov", 49.4306, 15.2206), ("Nový Jičín", 49.5939, 18.0089),
    ("Valašské Meziříčí", 49.4714, 17.9736), ("Vsetín", 49.3397, 17.9894),
    ("Rožnov pod Radhoštěm", 49.4611, 18.1406), ("Kopřivnice", 49.4422, 18.1450),
    ("Frenštát pod Radhoštěm", 49.5544, 18.2097), ("Bohumín", 49.9042, 18.3589),
    ("Orlová", 49.8561, 18.3058), ("Karviná", 49.8483, 18.3903),
    ("Český Těšín", 49.7479, 18.6220), ("Nový Bor", 50.7556, 14.5472),
    ("Varnsdorf", 50.9131, 14.6203), ("Rumburk", 50.9411, 14.5664),
    ("Litvínov", 50.6022, 13.6114), ("Louny", 50.3589, 13.7964),
    ("Žatec", 50.3272, 13.5464), ("Kadaň", 50.3786, 13.2722),
    ("Podbořany", 50.3217, 13.4167), ("Bílina", 50.5458, 13.7778),
    ("Aš", 50.2244, 12.1917), ("Sokolov", 50.1764, 12.6419),
    ("Ostrov", 50.3053, 12.9433), ("Nejdek", 50.3300, 12.7253),
    ("Mariánské Lázně", 49.9556, 12.7014), ("Františkovy Lázně", 50.1228, 12.3514),
    ("Tachov", 49.7936, 12.6306), ("Stříbro", 49.7567, 12.9294),
    ("Domažlice", 49.4397, 12.9325), ("Sušice", 49.2328, 13.5164),
    ("Horažďovice", 49.3264, 13.6906), ("Přeštice", 49.6742, 13.3383),
    ("Dobřany", 49.7244, 13.2539), ("Blovice", 49.6717, 13.5403),
    ("Rožmitál pod Třemšínem", 49.5117, 13.8603), ("Vlašim", 49.7036, 15.0775),
    ("Votice", 49.6400, 14.6400), ("Sedlčany", 49.5336, 14.3903),
    ("Dobříš", 49.7736, 14.1706), ("Mnichovo Hradiště", 50.5286, 14.8864),
    ("Turnov", 50.5889, 15.1097), ("Semily", 50.6011, 15.3894),
    ("Železný Brod", 50.6436, 15.2519), ("Jilemnice", 50.6097, 15.4728),
    ("Nová Paka", 50.4936, 15.5211), ("Hořice", 50.3714, 15.6267),
    ("Nové Město nad Metují", 50.3436, 16.0578), ("Jaroměř", 50.3547, 15.9322),
    ("Vrchlabí", 50.6286, 15.6086), ("Hostinné", 50.5714, 15.6900),
    ("Svoboda nad Úpou", 50.5994, 15.8064), ("Broumov", 50.5844, 16.3322),
    ("Polička", 49.7158, 16.2592), ("Svitavy", 49.7633, 16.4708),
    ("Litomyšl", 49.8717, 16.2747), ("Moravská Třebová", 49.7581, 16.6617),
    ("Lanškroun", 49.9136, 16.6114), ("Ústí nad Orlicí", 49.9717, 16.3961),
    ("Česká Třebová", 49.9061, 16.4472), ("Vysoké Mýto", 49.9514, 16.4322),
    ("Choceň", 50.0011, 16.2203), ("Králíky", 49.8956, 16.7683),
    ("Žamberk", 50.0797, 16.4681), ("Rychnov nad Kněžnou", 50.1681, 16.2797),
    ("Kostelec nad Orlicí", 50.1275, 16.2086), ("Prachatice", 49.0119, 13.9572),
    ("Vimperk", 49.0575, 13.8883), ("Volary", 48.9289, 13.8961),
    ("Vodňany", 49.1458, 14.1636), ("Protivín", 49.1972, 14.2217),
    ("Třeboň", 49.0036, 14.7694), ("Suchdol nad Lužnicí", 48.9411, 14.8658),
    ("Nové Hrady", 48.7931, 14.7836), ("Slavonice", 48.9994, 15.3553),
    ("Dačice", 49.0878, 15.4194), ("Telč", 49.1839, 15.4547),
    ("Moravské Budějovice", 49.0836, 15.9125), ("Jemnice", 49.0175, 15.5753),
    ("Nové Město na Moravě", 49.5614, 16.0889), ("Bystřice nad Pernštejnem", 49.5297, 16.2583),
    ("Velké Meziříčí", 49.4392, 16.0453), ("Velká Bíteš", 49.2956, 16.2289),
    ("Náměšť nad Oslavou", 49.2075, 16.1597), ("Ivančice", 49.3014, 16.3889),
    ("Rosice", 49.2800, 16.3922), ("Pohořelice", 48.9514, 16.5297),
    ("Hustopeče", 48.9383, 16.7383), ("Mikulov", 48.8053, 16.6372),
    ("Bzenec", 48.9572, 17.2736), ("Uherské Hradiště", 49.0681, 17.4569),
    ("Uherský Brod", 49.0239, 17.6425), ("Kunovice", 49.0547, 17.4556),
    ("Napajedla", 49.1086, 17.5103), ("Otrokovice", 49.2064, 17.5453),
    ("Vizovice", 49.2764, 17.8475), ("Luhačovice", 49.0994, 17.7506),
    ("Bojkovice", 49.0347, 17.8317), ("Veselí nad Moravou", 48.9581, 17.3703),
    ("Strážnice", 48.9033, 17.3217), ("Kyjov", 49.0089, 17.1244),
    ("Bučovice", 49.1553, 16.9986), ("Slavkov u Brna", 49.1633, 16.9475),
    ("Rousínov", 49.1892, 16.9194), ("Boskovice", 49.4889, 16.6319),
    ("Letovice", 49.5411, 16.5411), ("Kuřim", 49.3011, 16.5328),
    ("Tišnov", 49.3472, 16.4250), ("Rajhrad", 49.0864, 16.6067),
    ("Modřice", 49.0892, 16.6089), ("Šlapanice", 49.1567, 16.7289),
    ("Holešov", 49.3136, 17.5794), ("Bystřice pod Hostýnem", 49.3847, 17.7172),
    ("Chropyně", 49.3525, 17.4281), ("Kojetín", 49.3969, 17.3189),
    ("Kralovice", 50.0044, 13.4808), ("Toužim", 50.0889, 13.0028),
    ("Bečov nad Teplou", 50.2150, 12.8786), ("Teplá", 50.0164, 12.8803),
    ("Kraslice", 50.3358, 12.5158), ("Kynšperk nad Ohří", 50.1303, 12.5583),
    ("Lázně Kynžvart", 50.0300, 12.6125), ("Frýdlant", 50.9108, 15.0825),
    ("Harrachov", 50.7811, 15.4292), ("Železná Ruda", 49.1394, 13.2158),
    ("Kvilda", 49.0325, 13.5739), ("Blatná", 49.4269, 13.9289),
    ("Zdice", 49.8700, 13.9700), ("Hořovice", 49.8317, 13.9167),
    ("Žebrák", 49.8833, 13.9033), ("Křivoklát", 50.0406, 13.8808),
    ("Nezvěstice", 49.6811, 13.5328), ("Chotěšov", 49.7169, 13.3789),
    ("Holýšov", 49.7189, 13.2389), ("Staňkov", 49.6597, 13.2853),
    ("Poběžovice", 49.6547, 12.8447), ("Bakov nad Jizerou", 50.4875, 14.9294),
    ("Bělá pod Bezdězem", 50.5036, 14.8153), ("Benátky nad Jizerou", 50.2903, 14.8522),
    ("Milovice", 50.2292, 14.8886), ("Lysá nad Labem", 50.2053, 14.8297),
    ("Čelákovice", 50.1600, 14.7717), ("Brandýs nad Labem", 50.1875, 14.7794),
    ("Úvaly", 50.1611, 14.7300), ("Říčany", 49.9936, 14.6644),
    ("Jeseník", 50.2278, 17.2106), ("Vrbno pod Pradědem", 50.2953, 17.3575),
    ("Zlaté Hory", 50.2650, 17.3797), ("Javorník", 50.3022, 17.3136),
    ("Krásná Lípa", 50.8944, 14.5475), ("Šluknov", 50.9983, 14.4581),
    ("Jiříkov", 50.9814, 14.5697), ("Česká Kamenice", 50.8000, 14.4194),
    ("Kamenický Šenov", 50.7569, 14.4678), ("Cvikov", 50.7811, 14.6158),
    ("Zákupy", 50.6922, 14.6572), ("Doksy", 50.5442, 14.6417),
    ("Mimoň", 50.6508, 14.7208), ("Stráž pod Ralskem", 50.7064, 14.6983),
    ("Roudnice nad Labem", 50.4239, 14.2575), ("Štětí", 50.4933, 14.4178),
    ("Terezín", 50.5033, 14.1514), ("Lovosice", 50.5172, 14.0517),
    ("Úštěk", 50.5883, 14.3206), ("Libochovice", 50.4772, 14.0936),
    ("Hřensko", 50.8722, 14.2222), ("Jílové u Prahy", 49.9047, 14.4939),
    ("Průhonice", 49.9992, 14.5617), ("Černošice", 49.9861, 14.3925),
    ("Kralupy nad Vltavou", 50.2906, 14.3186), ("Slaný", 50.2308, 14.0886),
    ("Veltrusy", 50.2778, 14.3383), ("Peruc", 50.3386, 13.9786),
    ("Týnec nad Sázavou", 49.8533, 14.5947), ("Neveklov", 49.7839, 14.6014),
    ("Světlá nad Sázavou", 49.6611, 15.4025), ("Ledeč nad Sázavou", 49.6983, 15.3753),
    ("Humpolec", 49.5439, 15.3544), ("Pacov", 49.4703, 15.2247),
    ("Zruč nad Sázavou", 49.7008, 15.1406), ("Uhlířské Janovice", 49.8519, 15.0750),
    ("Zásmuky", 49.9044, 15.0197), ("Kouřim", 49.9875, 14.9814),
    ("Poděbrady", 50.1139, 15.1186), ("Nymburk", 50.1856, 15.0428),
    ("Sadská", 50.1564, 14.9944), ("Rožďalovice", 50.2239, 15.0056),
    ("Heřmanův Městec", 49.9467, 15.6928), ("Chrast", 49.9128, 15.6336),
    ("Hrochův Týnec", 49.9622, 15.7244), ("Skuteč", 49.8464, 15.7972),
    ("Proseč", 49.8556, 16.0097), ("Jevíčko", 49.7036, 16.6853),
    ("Velké Opatovice", 49.6639, 16.6422), ("Rokytnice v Orlických horách", 50.5075, 16.2897),
    ("Dobruška", 50.3922, 16.2228), ("Opočno", 50.4033, 16.2131),
    ("Solnice", 50.2111, 16.1464), ("Třebechovice pod Orebem", 50.2167, 16.0867),
    ("Borohrádek", 50.2333, 16.1486), ("Běstvina", 49.8436, 15.6933),
]


def _latlon_to_tile_float(lat, lon, zoom):
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def _is_europe(lat, lon):
    return 34.0 <= lat <= 72.0 and -25.0 <= lon <= 45.0

def geocode_all(place_name):
    params = {"q": place_name, "format": "json", "limit": 5}
    europe_codes = ",".join([
        "al","ad","at","ba","be","bg","by","ch","cy","cz","de","dk","ee",
        "es","fi","fr","gb","gr","hr","hu","ie","is","it","lt","lu","lv",
        "md","me","mk","mt","nl","no","pl","pt","ro","rs","se","si","sk",
        "ua","xk"
    ])
    params["countrycodes"] = europe_codes
    for attempt in range(5):
        if attempt > 0:
            wait = 2 ** attempt
            print(f"  Waiting {wait}s before retry...")
            time.sleep(wait)
        time.sleep(1.0)
        resp = requests.get(NOMINATIM_URL, params=params,
                            headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code == 429:
            print(f"  Nominatim rate limited (attempt {attempt+1}/5)")
            continue
        resp.raise_for_status()
        data = resp.json()
        break
    else:
        raise RuntimeError(f"Nominatim rate limit exceeded for '{place_name}', try again later")
    data = resp.json()
    if not data:
        raise ValueError(f"Place not found: {place_name}")
    results = []
    for item in data:
        display = item.get("display_name", "")
        lat = float(item["lat"])
        lon = float(item["lon"])
        if _is_europe(lat, lon):
            results.append((display, lat, lon))
    if not results:
        raise ValueError(f"No European results for: {place_name}")
    return results


def find_closest_station(lat, lon):
    best_id = None
    best_dist = float("inf")
    for sid, info in STATIONS.items():
        _, _, dist = _geod.inv(lon, lat, info["lon"], info["lat"])
        if dist < best_dist:
            best_dist = dist
            best_id = sid
    return best_id, best_dist


def fetch_tile(url_template, z, x, y, fallback_color=(30, 30, 35, 255),
               yx=False, subdomains=CARTO_SUBDOMAINS):
    if subdomains is not None:
        sub = subdomains[(x + y) % len(subdomains)]
        url = url_template.replace("{s}", sub)
    else:
        url = url_template
    if yx:
        url = (url.replace("{z}", str(z))
                 .replace("{y}", str(y))
                 .replace("{x}", str(x)))
    else:
        url = (url.replace("{z}", str(z))
                 .replace("{x}", str(x))
                 .replace("{y}", str(y)))
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception:
        return Image.new("RGBA", (256, 256), fallback_color)


def render_radar_tile(parsed, elev_idx, z, x, y, var, colorscheme="1", vmin=None, vmax=None,
                    vmin_cut=None):
    try:
        png = render_tile(parsed, elev_idx, z, x, y, var, colorscheme=colorscheme,
                          vmin=vmin, vmax=vmax, vmin_cut=vmin_cut)
        return Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception:
        return Image.new("RGBA", (256, 256), (0, 0, 0, 0))


def _boost_labels(img, factor=1.8):
    arr = np.array(img, dtype=np.float32)
    arr[:, :, :3] = np.clip(arr[:, :, :3] * factor, 0, 255)
    arr[:, :, 3] = np.clip(arr[:, :, 3] * factor, 0, 255)
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def _draw_city_labels(canvas, zoom, cx, cy, half, grid_size, label_font, cities,
                     fill=(255, 255, 255), stroke=(0, 0, 0),
                     shift_x=0, shift_y=0):
    """Draw city name labels at their true geographic positions. Only cities
    inside the panel view are drawn. Colors adapt to the basemap brightness:
    dark basemaps use white fill + black outline; light basemaps use dark
    fill + white outline."""
    T = 256
    panel_w = grid_size * T
    panel_h = grid_size * T
    draw = ImageDraw.Draw(canvas)
    margin = 60
    for name, lat, lon in cities:
        xf, yf = _latlon_to_tile_float(lat, lon, zoom)
        px = int(round((xf - (cx - half)) * T + shift_x))
        py = int(round((yf - (cy - half)) * T + shift_y))
        if -margin <= px <= panel_w + margin and -margin <= py <= panel_h + margin:
            draw.text((px, py), name, font=label_font, fill=fill,
                      stroke_width=2, stroke_fill=stroke, anchor="mm")


_small_font = None
_font = None
_large_font = None
_label_font = None


# Candidate truetype fonts tried in order, covering Windows, Linux and macOS.
# A font bundled in the repo's `fonts/` directory is preferred first, so the
# same glyphs are used on every platform. Falls back to PIL's built-in bitmap
# font only if none are present.
_FONT_CANDIDATES = [
    # Windows
    "arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    # Linux (common packages)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]


def _bundled_fonts():
    """Return any .ttf files shipped in the repo's `fonts/` dir, in sorted
    order, so a bundled font wins over system fonts on every platform."""
    fonts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    if not os.path.isdir(fonts_dir):
        return []
    return [os.path.join(fonts_dir, fn)
            for fn in sorted(os.listdir(fonts_dir))
            if fn.lower().endswith(".ttf")]


def _load_font(size):
    """Load a scalable truetype font of `size`. Prefers a font bundled in the
    repo's `fonts/` directory, then common Windows/Linux/macOS fonts. Returns
    PIL's default bitmap font only as a last resort."""
    for path in _bundled_fonts() + _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _init_fonts():
    global _small_font, _font, _large_font, _label_font
    _small_font = _load_font(11)
    _font = _load_font(14)
    _large_font = _load_font(18)
    _label_font = _load_font(16)


def _draw_legend_bar(canvas, draw, x, y, width, height, var, colorscheme="1", vmin=None, vmax=None):
    if vmin is None:
        vmin = VARIABLES[var]["vmin"]
    if vmax is None:
        vmax = VARIABLES[var]["vmax"]
    if var == "ZDR":
        lut = ZDR_LUT
    elif var == "CC":
        lut = CC_LUT
    elif var == "W":
        lut = W_LUT
    elif var == "PHIDP":
        lut = PHIDP_LUT
    elif var in ("DBZH", "TH"):
        lut = DBZH_LUT_2 if colorscheme == "2" else DBZH_LUT
    else:
        lut = VRADH_LUT

    bar = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bar_d = bar.load()
    for i in range(width):
        idx = int(i / width * 255)
        idx = min(idx, 255)
        c = tuple(int(v) for v in lut[idx])
        for j in range(height):
            bar_d[i, j] = c

    canvas.paste(bar, (x, y), bar)

    start_label = f"{vmin}"
    end_label = f"{vmax}"
    dy = height + 2
    draw.text((x, y + dy), start_label, fill=(180, 180, 180), font=_small_font)
    end_w = draw.textbbox((0, 0), end_label, font=_small_font)[2]
    draw.text((x + width - end_w, y + dy), end_label, fill=(180, 180, 180), font=_small_font)


def _parse_ts_to_utc2(ts_str):
    if ts_str == "N/A" or not ts_str:
        return "N/A"
    dt_utc = _parse_ts_dt(ts_str)
    tz_utc2 = timezone(timedelta(hours=2))
    dt_local = dt_utc.astimezone(tz_utc2)
    return dt_local.strftime("%Y-%m-%d %H:%M")

def _parse_ts_dt(ts_str):
    return datetime(
        int(ts_str[0:4]), int(ts_str[4:6]), int(ts_str[6:8]),
        int(ts_str[9:11]), int(ts_str[11:13]), tzinfo=timezone.utc
    )

def _is_stale(ts_str, max_minutes=30, target_time=None):
    if ts_str == "N/A" or not ts_str:
        return True
    dt = _parse_ts_dt(ts_str)
    if target_time is not None:
        return abs((dt - target_time).total_seconds()) > max_minutes * 60
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() > max_minutes * 60


def generate_place_image(place_name, zoom=12, grid_size=5, elevation=None,
                          label_intensity=1.8, colorscheme="1", output_dir=None,
                          place_lat=None, place_lon=None,
                          include_zdr=False, include_cc=False,
                          include_w=False, include_phidp=False,
                          include_dbzh=True, include_vel=True,
                          station_code=None, target_time=None,
                          single_var=None, dbzh_min=None, layout="auto",
                          basemap="dark"):
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(__file__))
    _init_fonts()
    basemap_cfg = BASEMAPS.get(basemap, BASEMAPS["dark"])
    if basemap not in BASEMAPS:
        basemap = "dark"
    t_start = time.time()

    print(f"[1/7] Resolving place '{place_name}'...")
    print(f"       Basemap: {basemap}")
    if place_lat is None or place_lon is None:
        all_results = geocode_all(place_name)
        if len(all_results) > 1:
            print(f"       Multiple matches ({len(all_results)} found):")
            for i, (display, lat, lon) in enumerate(all_results):
                short = display[:80] + "..." if len(display) > 80 else display
                print(f"         [{i}] {short}  ({lat:.4f}, {lon:.4f})")
            print(f"       Using [0] by default. Use --index N to pick another.")
        _, place_lat, place_lon = all_results[0]
    print(f"       => {place_lat:.4f}, {place_lon:.4f}")

    print(f"[2/7] Finding closest radar station...")
    if station_code:
        sc = station_code.lower()
        if sc not in STATIONS:
            codes = sorted(STATIONS.keys())
            raise RuntimeError(
                f"Unknown station '{sc}'. Available: {', '.join(codes)}")
        station_id = sc
        info = STATIONS[station_id]
        dist = 0
        print(f"       Manual: {info['name']} ({info['country'].upper()})")
    else:
        station_id, dist = find_closest_station(place_lat, place_lon)
        info = STATIONS[station_id]
        print(f"       => {info['name']} ({info['country'].upper()}), {dist/1000:.1f} km away")

    if target_time:
        print(f"[3/7] Fetching radar data for {target_time.strftime('%Y-%m-%d %H:%M')} UTC...")
    else:
        print(f"[3/7] Fetching latest radar data...")
    country = _station_country(station_id)
    cfg = COUNTRY_CONFIG[country]
    sep = _get_station_separate(station_id)
    if sep is None:
        sep = cfg["separate_elevations"]

    def _chmi_filename_to_dt(fname):
        m = re.search(r'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', fname or "")
        if not m:
            return None
        return datetime(int(m[1]), int(m[2]), int(m[3]),
                        int(m[4]), int(m[5]), int(m[6]), tzinfo=timezone.utc)

    def _chmi_ts_12(fname):
        dt = _chmi_filename_to_dt(fname)
        return dt.strftime("%Y%m%dT%H%M") if dt else "N/A"

    # --- DBZH + radial velocity from CHMI open data (vol_z / vol_v) ---
    # DBZH is published both as a single 1.5 deg sweep and as a full
    # multi-angle volume (0.1-21.6); pick the full-volume file so --elev
    # works across all angles, not just 1.5.
    dbzh_filename = chmi_best_volume_key(station_id, "DBZH", target_time)
    dbzh_parsed = chmi_fetch_filename(station_id, "DBZH", dbzh_filename)
    dbzh_ts = _chmi_ts_12(dbzh_filename)
    dbzh_dt = _chmi_filename_to_dt(dbzh_filename)

    vel_var = "VRADH"
    vel_filename = chmi_latest_key(station_id, vel_var, target_time)
    vel_parsed = chmi_fetch_and_parse(station_id, vel_var, target_time)
    if not vel_parsed:
        vel_var = "VRAD"
        vel_filename = chmi_latest_key(station_id, vel_var, target_time)
        vel_parsed = chmi_fetch_and_parse(station_id, vel_var, target_time)
    vel_ts = _chmi_ts_12(vel_filename)

    print(f"       DBZH file: {dbzh_filename or 'N/A'}")
    print(f"       {vel_var} file: {vel_filename or 'N/A'}")

    if not dbzh_parsed and not vel_parsed:
        raise RuntimeError("No radar data available for station")

    # Elevations from parsed CHMI sweeps. DBZH is fetched as the full-volume
    # file (0.1-21.6), so it offers the same elevation set as the other
    # multi-angle products.
    if vel_parsed:
        elevs = [ds["elevation"] for ds in vel_parsed["datasets"]]
    elif dbzh_parsed:
        elevs = [ds["elevation"] for ds in dbzh_parsed["datasets"]]
    else:
        elevs = []
    default_elev = elevation if elevation else (str(elevs[0]) if elevs else "0.5")
    print(f"       Available elevations: {elevs}")
    print(f"       Using elevation: {default_elev}")

    no_data_dbzh = False
    no_data_vel = False
    if dbzh_parsed and _is_stale(dbzh_ts, 30, target_time):
        if target_time is not None:
            print(f"       WARNING: no DBZH data within 30 min of requested "
                  f"{target_time.strftime('%Y-%m-%d %H:%M')} UTC (nearest {dbzh_ts})")
        else:
            print(f"       WARNING: DBZH data is older than 30 min ({dbzh_ts})")
        dbzh_parsed = None
        no_data_dbzh = True
    if vel_parsed and _is_stale(vel_ts, 30, target_time):
        if target_time is not None:
            print(f"       WARNING: no {vel_var} data within 30 min of requested "
                  f"{target_time.strftime('%Y-%m-%d %H:%M')} UTC (nearest {vel_ts})")
        else:
            print(f"       WARNING: {vel_var} data is older than 30 min ({vel_ts})")
        vel_parsed = None
        no_data_vel = True

    def find_elev_idx(parsed, elev_str):
        if not parsed:
            return 0
        try:
            el = float(elev_str)
        except (ValueError, TypeError):
            return 0
        for i, ds in enumerate(parsed["datasets"]):
            if abs(float(ds["elevation"]) - el) < 0.01:
                return i
        return 0

    dbzh_elev_idx = find_elev_idx(dbzh_parsed, default_elev)
    vel_elev_idx = find_elev_idx(vel_parsed, default_elev)

    # --- CHMI extra products (ZDR, CC) ---
    def _parse_chmi_ts(filename):
        import re
        m = re.search(r'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', filename or "")
        if not m:
            return "N/A"
        dt_utc = datetime(int(m[1]), int(m[2]), int(m[3]),
                          int(m[4]), int(m[5]), int(m[6]), tzinfo=timezone.utc)
        tz_utc2 = timezone(timedelta(hours=2))
        dt_local = dt_utc.astimezone(tz_utc2)
        return dt_local.strftime("%Y-%m-%d %H:%M")

    extra_panels = []
    if station_id in CHMI_STATIONS:
        chmi_products = [("ZDR", include_zdr), ("CC", include_cc), ("W", include_w), ("PHIDP", include_phidp)]
        for pname, flag in chmi_products:
            if not flag:
                continue
            filename = chmi_latest_key(station_id, pname, target_time)
            chmi_ts = _parse_chmi_ts(filename)
            parsed = chmi_fetch_and_parse(station_id, pname, target_time)
            _fdt = _chmi_filename_to_dt(filename)
            far_from_target = target_time is not None and (
                _fdt is None or abs((_fdt - target_time).total_seconds()) > 30 * 60)
            if parsed and not far_from_target:
                print(f"       {pname}: OK ({len(parsed['datasets'])} sweeps, {chmi_ts})")
                extra_panels.append({
                    "var": pname, "parsed": parsed,
                    "elev_idx": find_elev_idx(parsed, default_elev),
                    "no_data": False, "ts": chmi_ts,
                })
            else:
                if far_from_target:
                    print(f"       {pname}: no data within 30 min of requested "
                          f"{target_time.strftime('%Y-%m-%d %H:%M')} UTC (nearest {chmi_ts})")
                else:
                    print(f"       {pname}: N/A")
                extra_panels.append({
                    "var": pname, "parsed": None, "elev_idx": 0,
                    "no_data": True, "ts": "N/A",
                })
    else:
        any_extra = include_zdr or include_cc or include_w or include_phidp
        if any_extra:
            print(f"       SKIP: {info['country'].upper()} has no CHMI open data")

    # --- Build panel list ---
    panels = []
    if include_dbzh:
        panels.append({"var": "DBZH", "parsed": dbzh_parsed, "elev_idx": dbzh_elev_idx, "no_data": no_data_dbzh,
                       "ts": _parse_ts_to_utc2(dbzh_ts),
                       "elev": (dbzh_parsed["datasets"][dbzh_elev_idx]["elevation"] if dbzh_parsed else 1.5)})
    if include_vel:
        panels.append({"var": vel_var, "parsed": vel_parsed, "elev_idx": vel_elev_idx, "no_data": no_data_vel,
                       "ts": _parse_ts_to_utc2(vel_ts),
                       "elev": (vel_parsed["datasets"][vel_elev_idx]["elevation"] if vel_parsed else float(default_elev))})
    for ep in extra_panels:
        if ep["parsed"] is not None:
            ep["elev"] = ep["parsed"]["datasets"][ep["elev_idx"]]["elevation"]
        else:
            try:
                ep["elev"] = float(default_elev)
            except (ValueError, TypeError):
                ep["elev"] = 0.0
    panels = panels + extra_panels

    if single_var is not None:
        panels = [p for p in panels if p["var"] == single_var]

    if all(p["no_data"] for p in panels):
        if target_time is not None:
            raise RuntimeError(
                f"No radar data within 30 min of requested "
                f"{target_time.strftime('%Y-%m-%d %H:%M')} UTC "
                f"(CHMI keeps ~4 days).")
        raise RuntimeError(f"No radar data available for station {station_id}")

    print(f"[4/7] Calculating tile grid at zoom {zoom}...")

    T = 256
    cx_float, cy_float = _latlon_to_tile_float(place_lat, place_lon, zoom)
    cx_int, cy_int = int(cx_float), int(cy_float)
    shift_x = (0.5 - (cx_float - cx_int)) * T
    shift_y = (0.5 - (cy_float - cy_int)) * T
    half = grid_size // 2
    tile_xs = list(range(cx_int - half - 1, cx_int - half + grid_size + 1))
    tile_ys = list(range(cy_int - half - 1, cy_int - half + grid_size + 1))
    tile_grids = {}
    tile_grids["C"] = {
        "cx_int": cx_int, "cy_int": cy_int,
        "shift_x": shift_x, "shift_y": shift_y,
        "tile_xs": tile_xs, "tile_ys": tile_ys,
        "tile_positions": [(x, y) for x in tile_xs for y in tile_ys],
    }
    print(f"       center=({cx_int},{cy_int}), grid={grid_size}x{grid_size}")

    print(f"[5/7] Rendering tiles...")
    cache = {}

    def worker(x, y):
        cfg = basemap_cfg
        base = fetch_tile(cfg["base"], zoom, x, y, fallback_color=cfg["fallback"],
                          yx=cfg["base_yx"], subdomains=cfg["base_sub"])
        overlays = []
        for ov in cfg["overlays"]:
            ourl, osub, oyx, oboost, ow, oalpha = ov[:6]
            oimg = fetch_tile(ourl, zoom, x, y, fallback_color=(0, 0, 0, 0),
                              yx=oyx, subdomains=osub)
            if oboost:
                oimg = _boost_labels(oimg, label_intensity)
            if oalpha != 1.0:
                arr = np.array(oimg)
                arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * oalpha).astype(np.uint8)
                oimg = Image.fromarray(arr, "RGBA")
            overlays.append(oimg)
        tiles = []
        for p in panels:
            _tvmin = _tvmax = _tcut = None
            if p["var"] == "DBZH":
                if dbzh_min is not None:
                    _tcut = dbzh_min
            if p["parsed"]:
                t = render_radar_tile(p["parsed"], p["elev_idx"], zoom, x, y, p["var"],
                                      colorscheme if p["var"] in ("DBZH", "TH") else "1",
                                      _tvmin, _tvmax, _tcut)
            else:
                t = None
            tiles.append(t)
        return x, y, base, overlays, tiles

    all_tile_positions = set()
    for q in tile_grids.values():
        all_tile_positions.update(q["tile_positions"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        fs = {pool.submit(worker, x, y): (x, y) for x, y in all_tile_positions}
        for f in concurrent.futures.as_completed(fs):
            x, y, base, overlays, tiles = f.result()
            cache[(x, y)] = (base, overlays, tiles)

    print(f"[6/7] Compositing panels...")
    T = 256
    panel_w = grid_size * T
    panel_h = grid_size * T

    # Build canvas per (qname, panel_index)
    all_canvases = {}
    for qname, qdata in tile_grids.items():
        for ci in range(len(panels)):
            all_canvases[(qname, ci)] = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))

    for qname, qdata in tile_grids.items():
        cx_int = qdata["cx_int"]
        cy_int = qdata["cy_int"]
        half = grid_size // 2
        shift_x = qdata["shift_x"]
        shift_y = qdata["shift_y"]
        txs = qdata["tile_xs"]
        tys = qdata["tile_ys"]
        for x in txs:
            for y in tys:
                px = int(round((x - (cx_int - half)) * T + shift_x))
                py = int(round((y - (cy_int - half)) * T + shift_y))
                entry = cache.get((x, y))
                if not entry:
                    continue
                base, overlays, tiles = entry
                base = base or Image.new("RGBA", (T, T), basemap_cfg["fallback"])
                for ci, p in enumerate(panels):
                    radar_tile = tiles[ci] or Image.new("RGBA", (T, T), (0, 0, 0, 0))
                    panel = Image.new("RGBA", (T, T), (0, 0, 0, 0))
                    panel.paste(base, (0, 0), base)
                    panel = Image.alpha_composite(panel, radar_tile)
                    for ov in overlays:
                        panel = Image.alpha_composite(panel, ov)
                    all_canvases[(qname, ci)].paste(panel, (px, py), panel)

    if basemap == "satellite":
        _city_list = CITIES + [(place_name, place_lat, place_lon)]
        for qname, qdata in tile_grids.items():
            for ci in range(len(panels)):
                _draw_city_labels(
                    all_canvases[(qname, ci)], zoom, qdata["cx_int"], qdata["cy_int"],
                    grid_size // 2, grid_size, _label_font, _city_list,
                    shift_x=qdata["shift_x"], shift_y=qdata["shift_y"])

    print(f"[7/7] Assembling final image(s)...")
    gap = 4
    bottom_h = 50
    lbar_h = 28
    desc_h = 46
    cell_h = panel_h + lbar_h + desc_h
    lbar_margin = 10
    one_line_font = _load_font(30)
    desc_font = _load_font(32)

    panel_descs = {
        "DBZH": "Reflectivity (DBZH)", "VRADH": "Velocity (VRADH)",
        "VRAD": "Velocity (VRAD)", "ZDR": "Differential Reflectivity (ZDR)",
        "CC": "Correlation Coefficient (CC)", "W": "Spectral Width (W)",
        "PHIDP": "Differential Phase (PHIDP)",
    }

    def _render_single_cell(canvas_rgba, var, ts_str, is_no_data, w, h, elev):
        rgb = Image.new("RGB", (w, h), (0, 0, 0))
        rgb.paste(canvas_rgba, (0, 0), canvas_rgba)

        cell = Image.new("RGB", (w, cell_h), (0, 0, 0))
        cd = ImageDraw.Draw(cell)
        cell.paste(rgb, (0, lbar_h))
        cd.rectangle([(0, 0), (w, lbar_h)], fill=(18, 18, 24))
        _lbar_vmin = _lbar_vmax = None
        _draw_legend_bar(cell, cd, lbar_margin, 2, w - lbar_margin * 2, lbar_h - 6,
                         var, colorscheme if var in ("DBZH", "TH") else "1",
                         _lbar_vmin, _lbar_vmax)
        desc_y = lbar_h + h
        cd.rectangle([(0, desc_y), (w, desc_y + desc_h)], fill=(18, 18, 24))
        desc = panel_descs.get(var, var)
        try:
            elev_txt = f"elev {float(elev):g}"
        except (ValueError, TypeError):
            elev_txt = "elev ?"
        bottom_line = f"{desc}  {elev_txt}  {ts_str}"
        cd.text((8, desc_y + (desc_h - 38) // 2), bottom_line,
                fill=(210, 210, 215), font=desc_font)
        return cell

    def _add_info_bar(img, w, station_name, city, single_panel=False):
        draw = ImageDraw.Draw(img)
        bar_y = img.height - bottom_h
        draw.rectangle([(0, bar_y), (w, img.height)], fill=(22, 22, 28))
        station_str = f"{station_name} ({info['country'].upper()})"
        text_y = bar_y + (bottom_h - 34) // 2
        sbbox = draw.textbbox((0, 0), station_str, font=one_line_font)
        sw = sbbox[2] - sbbox[0]
        cbbox = draw.textbbox((0, 0), city, font=one_line_font)
        cw = cbbox[2] - cbbox[0]
        gap_min = 20
        if single_panel:
            start_x = 12
        else:
            center_x = w // 2
            combined_w = sw + gap_min + cw
            if combined_w > w - 28:
                short_s = station_name[:12] + ".." if len(station_name) > 12 else station_name
                station_str = f"{short_s} ({info['country'].upper()})"
                sbbox = draw.textbbox((0, 0), station_str, font=one_line_font)
                sw = sbbox[2] - sbbox[0]
                combined_w = sw + gap_min + cw
            start_x = center_x - combined_w // 2
        draw.text((start_x, text_y), station_str, fill=(210, 210, 215), font=one_line_font)
        draw.text((start_x + sw + gap_min, text_y), city, fill=(255, 240, 160), font=one_line_font)
        disclaimer = "Radar data: CHMI open data (CC BY 4.0)"
        dbbox = draw.textbbox((0, 0), disclaimer, font=one_line_font)
        dw = dbbox[2] - dbbox[0]
        draw.text((w - dw - 12, text_y), disclaimer, fill=(150, 150, 158), font=one_line_font)

    def _save_one(path):
        n = 1
        p = path
        while os.path.exists(p):
            p = path.replace(".png", f"_{n}.png")
            n += 1
        final.save(p, "PNG")
        return p

    n_panels = len(panels)
    cols, rows = _compute_grid(n_panels, layout)
    total_w = panel_w * cols + gap * (cols - 1)
    total_h = cell_h * rows + gap * (rows - 1) + bottom_h

    final = Image.new("RGB", (total_w, total_h), (0, 0, 0))
    draw = ImageDraw.Draw(final)

    for ci, p in enumerate(panels):
        cvs = all_canvases[("C", ci)]
        cell = _render_single_cell(cvs, p["var"], p.get("ts", ""), p["no_data"], panel_w, panel_h,
                                  p.get("elev", default_elev))
        x_off = (ci % cols) * (panel_w + gap)
        y_off = (ci // cols) * (cell_h + gap)
        final.paste(cell, (x_off, y_off))
        if p["no_data"]:
            nd_font = _load_font(24 if rows > 1 else 32)
            nd_text = "No data"
            nd_bbox = draw.textbbox((0, 0), nd_text, font=nd_font)
            nd_w = nd_bbox[2] - nd_bbox[0]
            nd_h = nd_bbox[3] - nd_bbox[1]
            nd_x = x_off + (panel_w - nd_w) // 2
            nd_y = y_off + lbar_h + (panel_h - nd_h) // 2
            draw.text((nd_x, nd_y), nd_text, fill=(180, 180, 180), font=nd_font)

    _add_info_bar(final, total_w, info['name'], place_name, single_panel=(n_panels == 1))
    elapsed = time.time() - t_start
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', place_name.lower())
    suffix_parts = [f"z{zoom}", station_id]
    if single_var:
        suffix_parts.append(single_var.lower())
    else:
        if include_zdr: suffix_parts.append("zdr")
        if include_cc: suffix_parts.append("cc")
        if include_w: suffix_parts.append("w")
        if include_phidp: suffix_parts.append("phidp")
        if dbzh_min is not None:
            suffix_parts.append(f"min{int(dbzh_min)}")
        suffix_parts.append("combined")
    out = os.path.join(output_dir, f"{safe_name}_{'_'.join(suffix_parts)}.png")
    out = _save_one(out)
    print(f"\n{'='*60}")
    print(f"  Done in {elapsed:.1f}s")
    print(f"  Output: {out}")
    print(f"  Size: {final.width} x {final.height} px")
    print(f"  Color scheme: {colorscheme}")
    print(f"{'='*60}")
    return out


def _compute_grid(n, layout):
    if layout and layout != "auto":
        cols = None
        if "x" in layout:
            try:
                r, c = layout.lower().split("x")
                cols = int(c)
            except Exception:
                cols = None
        else:
            try:
                cols = int(layout)
            except Exception:
                cols = None
        if cols and cols > 0:
            rows = (n + cols - 1) // cols
            return cols, rows
    if n == 6:
        return 3, 2
    if n == 4:
        return 2, 2
    return n, 1


def main():
    parser = argparse.ArgumentParser(description="Generate combined radar image centered on a place")
    parser.add_argument("place", help="Place name (e.g., 'Lubawa', 'Berlin')")
    parser.add_argument("--index", type=int, default=None, help="If multiple place matches, pick this index")
    parser.add_argument("--scheme", type=str, default="1", choices=["1", "2"], help="Reflectivity color scheme: 1=colorful (default), 2=gray-to-red")
    parser.add_argument("--zoom", type=int, default=12, help="Zoom level (default: 12)")
    parser.add_argument("--grid", type=int, default=5, help="Tile grid size (default: 5)")
    parser.add_argument("--elev", type=str, default=None, help="Elevation angle in degrees, e.g. 0.5, 1.5 (default: lowest available)")
    parser.add_argument("--label-intensity", type=float, default=1.8, help="Label brightness boost factor (default: 1.8)")
    parser.add_argument("--output", "-o", default=None, help="Output directory (default: script folder)")
    parser.add_argument("--station", "-s", default=None, help="Manual station shortcode (brd, ska). Skips closest-station lookup.")
    parser.add_argument("--dbzh-min", type=float, default=None,
                        help="Mask out DBZH values below this dBZ (transparent). e.g. --dbzh-min 0")
    parser.add_argument("--time", type=str, default=None,
                        help="Target UTC time in YYYYMMDDHHMM format for past data (within CHMI retention, ~4 days)")
    parser.add_argument("--layout", type=str, default="auto",
                        help="Panel grid: 'auto' (default), column count (e.g. 2, 3), or 'RxC' (e.g. 2x3, 3x2, 2x2)")
    parser.add_argument("--basemap", "--map", dest="basemap", type=str, default="dark",
                        choices=list(BASEMAPS.keys()),
                        help="Basemap style (default: dark). Options: " + ", ".join(BASEMAPS.keys()))
    parser.add_argument("--var", type=str, default=None,
                        choices=["DBZH", "VRADH", "VRAD", "ZDR", "CC", "W", "PHIDP"],
                        help="Render only this variable (default: DBZH + VRADH)")
    parser.add_argument("--zdr", action="store_true", help="Include ZDR panel")
    parser.add_argument("--cc", action="store_true", help="Include CC panel")
    parser.add_argument("--w", action="store_true", help="Include W panel")
    parser.add_argument("--phidp", action="store_true", help="Include PHIDP panel")
    args = parser.parse_args()

    target_time = None
    if args.time:
        if len(args.time) != 12 or not args.time.isdigit():
            print("ERROR: --time must be in YYYYMMDDHHMM format (e.g. 202606301010)")
            sys.exit(1)
        target_time = datetime(int(args.time[0:4]), int(args.time[4:6]), int(args.time[6:8]),
                               int(args.time[8:10]), int(args.time[10:12]), tzinfo=timezone.utc)
        if target_time > datetime.now(timezone.utc):
            print("ERROR: --time cannot be in the future")
            sys.exit(1)

    single_var = args.var

    if args.dbzh_min is not None:
        if args.dbzh_min < 0:
            print("ERROR: --dbzh-min must be a non-negative number")
            sys.exit(1)

    all_results = geocode_all(args.place)
    if args.index is None:
        if len(all_results) > 1:
            print(f"Multiple matches for '{args.place}':")
            for i, (disp, lat, lon) in enumerate(all_results):
                short = disp[:90] + "..." if len(disp) > 90 else disp
                print(f"  [{i}] {short}  ({lat:.4f}, {lon:.4f})")
            print(f"Rerun with --index N (0-{len(all_results)-1}) to pick one.")
            sys.exit(0)
        args.index = 0
    if args.index >= len(all_results):
        print(f"Index {args.index} out of range (max {len(all_results)-1}).")
        sys.exit(1)
    display_name, place_lat, place_lon = all_results[args.index]

    display_place = display_name.split(",")[0].strip() if args.index > 0 else args.place

    generate_place_image(display_place,
                          zoom=args.zoom, grid_size=args.grid, elevation=args.elev,
                          label_intensity=args.label_intensity, colorscheme=args.scheme,
                          output_dir=args.output,
                          place_lat=place_lat, place_lon=place_lon,
                          include_zdr=args.zdr, include_cc=args.cc,
                          include_w=args.w, include_phidp=args.phidp,
                          station_code=args.station,
                          target_time=target_time,
                          single_var=single_var,
                          dbzh_min=args.dbzh_min,
                          layout=args.layout,
                          basemap=args.basemap)


if __name__ == "__main__":
    main()
