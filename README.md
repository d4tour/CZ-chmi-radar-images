# Radar Image Generator

Generates a **side-by-side** radar image for any place name, with dark basemap.
All radar data is fetched from **CHMI open data** (Czech Hydrometeorological Institute,
`https://opendata.chmi.cz/`) under the **CC BY 4.0** licence.

Used on Windows 11.

## Required files

Copy these **3 files** (and optionally `fonts/` for bundled fonts) to any device:

```
radar_server.py
generate_place_image.py
telegram_bot.py
```

## Install dependencies

```bash
pip install -r requirements.txt
```

## Data source

- **CHMI open data** (`opendata.chmi.cz`) — all radar products below.
- Basemap: CartoDB dark tiles (labels always on top).
- Each generated image shows a bottom bar: station, city, and a disclaimer
  `Radar data: CHMI open data (CC BY 4.0)`.

## Stations (Czechia only)

| ID   | Name    | Lat       | Lon        |
|------|---------|-----------|------------|
| czbrd | Brdicky | 49.6583   | 13.8178    |
| czska | Skalky  | 49.5011   | 16.7885   |

Default: **DBZH + VRADH/VRAD** only. Add `--zdr`, `--cc`, `--w`, `--phidp` to include extra panels, or `--var <name>` for a single variable.

| Parameter | CHMI product |
|-----------|--------------|
| DBZH      | `vol_z` (reflectivity, multi-angle volume) |
| VRADH     | `vol_v` (radial velocity) |
| ZDR       | `vol_zdr` |
| CC        | `vol_rhohv` |
| W         | `vol_w` |
| PHIDP     | `vol_phidp` |

> **Note:** DBZH is now fetched as the full multi-angle volume, so `--elev` works
> like for every other parameter.

## Basic usage

```powershell
python generate_place_image.py "Brno"
python generate_place_image.py "Praha"
```

Output: `brno_z12_czska_combined.png` in the script folder.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--zoom` | `12` | Map zoom level (higher = more detail) |
| `--grid` | `5` | Tile grid size (3=tighter, 7=wider) |
| `--scheme` | `1` | Reflectivity color scheme: `1`=green-white, `2`=gray-to-red |
| `--elev` | `0.1` | Elevation angle for non-DBZH panels, e.g. `0.5`, `2.2`, `13.7` |
| `--label-intensity` | `1.8` | Map label brightness (higher = more visible) |
| `--index` | – | If multiple place matches, pick this index |
| `--dbzh-min` | – | Mask out DBZH values below this dBZ (transparent), e.g. `--dbzh-min 0` |
| `--var` | – | Single variable only: `DBZH`, `VRADH`, `VRAD`, `ZDR`, `CC`, `W`, `PHIDP` |
| `--zdr` | off | Include ZDR panel |
| `--cc` | off | Include CC panel |
| `--w` | off | Include W panel |
| `--phidp` | off | Include PHIDP panel |
| `--basemap` | `dark` | Map style: `dark`, `light`, `voyager`, `satellite` |
| `--station` | auto | Force a station, e.g. `--station czska` |
| `--output` / `-o` | script folder | Save destination |
| `--time` | now | Past timestamp, e.g. `--time 202607021235` (UTC). Picks the nearest CHMI scan within 30 min; only works inside CHMI's retention window (~last 4 days) |

Available elevations: `0.1, 0.5, 0.9, 1.3, 1.7, 2.2, 3.2, 4.5, 6.3, 8.7, 13.7, 21.6`.

> **Past data / retention:** `--time` selects the nearest CHMI scan within 30 minutes.
> CHMI open data only keeps a rolling archive (~last 4 days), so requests older than that
> fail with `ERROR: no radar data ...`. There is no hard 48h cap anymore — the limit is
> whatever CHMI currently retains.

## Examples

```powershell
# Gray-to-red reflectivity scale
python generate_place_image.py "Praha" --scheme 2

# Full 6-panel view (default is DBZH + VRADH only)
python generate_place_image.py "Brno" --zdr --cc --w --phidp

# Single variable only
python generate_place_image.py "Brno" --var ZDR

# Specific elevation, tighter view
python generate_place_image.py "Brno" --elev 2.2 --grid 3

# Higher zoom, wider grid, custom output
python generate_place_image.py "Ostrava" --zoom 13 --grid 5 -o C:\Users\Public

# Boost labels for readability
python generate_place_image.py "Liberec" --label-intensity 2.5

# Disambiguate when multiple places match
python generate_place_image.py "Mokre" --index 1

# Past timestamp (UTC)
python generate_place_image.py "Liberec" --time 202607021235

# Force station + elevation + dbzh cutoff
python generate_place_image.py "Brno" --station czska --elev 0.5 --dbzh-min 0

# Satellite basemap with on-map city labels
python generate_place_image.py "Praha" --basemap satellite
```

## Output

- One panel per parameter (default: DBZH + VRADH; add `--zdr --cc --w --phidp` for more).
- Each panel bottom line shows: `Parameter name  elev X.X  time (UTC)`.
- Dark basemap (CartoDB) + labels always on top.
- Legend bars at top matching tile colors.
- Bottom bar: station, city, and CHMI open-data CC BY 4.0 disclaimer.

## Color legends

Each panel's top bar shows a gradient from the parameter's minimum (left) to
maximum (right). Hex swatches below are sampled at 0% / 25% / 50% / 75% / 100%
of the range (low → high).

| Parameter | Range | Gradient (low → high) |
|-----------|-------|------------------------|
| **DBZH** (scheme 1) | 0 – 70 dBZ | `#99D8FF` → `#24A741` → `#FFB600` → `#AF0026` → `#FFFFFF` |
| **DBZH** (scheme 2) | 0 – 70 dBZ | `#B3B1B1` → `#3EB094` → `#0C5400` → `#DE1301` → `#E197E0` |
| **VRADH** | -50 – 50 m/s | `#84E8EB` → `#81EABA` → `#866B70` → `#FA789C` → `#FCACC1` |
| **ZDR** | -2 – 5 dB | `#131363` → `#1C3697` → `#7695D1` → `#FFCA96` → `#C73113` |
| **CC** | 0.2 – 1.0 | `#313131` → `#1E4559` → `#276381` → `#3B8BB3` → `#EFF9FF` |
| **W** | 0 – 10 m/s | `#090909` → `#1D3B9E` → `#76C6DB` → `#FFB979` → `#800909` |
| **PHIDP** | 0 – 360 ° | `#313131` → `#099C21` → `#9F0C1D` → `#0B9E9F` → `#9F0909` |

Notes:
- DBZH uses scheme 1 (green→white) by default; `--scheme 2` switches to the
  gray→red scale shown above.
- VRADH (radial velocity) is diverging around 0 m/s: blues ≈ approaching,
  reds/pinks ≈ receding.
- PHIDP wraps 0–360° (phase), so the ends are both dark and the middle shows
  the wrap-around.
- `--dbzh-min <v>` makes DBZH values below `v` transparent (not colored). There is no
  upper cutoff — DBZH always scales 0 → 70 dBZ.
