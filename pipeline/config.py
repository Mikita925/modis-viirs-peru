"""Configuración común del pipeline MODIS–VIIRS (alcance: Perú por departamentos)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Lee el archivo .env de la raíz (FIRMS_MAP_KEY, DATABASE_URL) si existe
_env = ROOT / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
DATA = ROOT / "data"
RAW_SP = DATA / "raw" / "sp"      # archivo histórico (Standard Processing)
RAW_API = DATA / "raw" / "api"    # descargas del Area API (SP reciente o NRT)
REGIONS_DIR = DATA / "regions"
OUT = DATA / "processed"

COUNTRY = "Peru"
PERU_BBOX = (-81.5, -18.5, -68.5, 0.1)  # oeste, sur, este, norte
FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov"

# Producto en el archivo por país de FIRMS -> metadatos
PRODUCTS = {
    "modis": {"family": "MODIS", "first_year": 2000},
    "viirs-snpp": {"family": "VIIRS", "first_year": 2012},
    "viirs-jpss1": {"family": "VIIRS", "first_year": 2018},  # NOAA-20
}
# Fuentes del Area API (máx. 5 días por petición)
API_SOURCES_SP = ["MODIS_SP", "VIIRS_NOAA20_SP", "VIIRS_SNPP_SP"]
API_SOURCES_NRT = ["MODIS_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]

# geoBoundaries (dominio público), Perú nivel ADM1 = departamentos (26 unidades).
# El archivo está en Git LFS: se descarga desde media.githubusercontent.com, no desde raw.
REGIONS_URL = (
    "https://media.githubusercontent.com/media/wmgeolab/geoBoundaries/90a1d52/releaseData/"
    "gbOpen/PER/ADM1/geoBoundaries-PER-ADM1_simplified.geojson"
)

H3_RES = 7                 # ~5 km² por celda: unidad espacial común
TZ_OFFSET_HOURS = -5       # Perú (UTC-5, sin horario de verano)
SERIES_START = "2003-01-01"  # primer año completo con Terra + Aqua
VIIRS_SWITCH_DATE = "2012-01-20"  # desde aquí se usa VIIRS observado
NOAA20_FROM = "2018-04-01"        # desde aquí la referencia VIIRS es NOAA-20
CALIB_YEARS = (2012, 2022)        # MODIS 2023+ se degrada por deriva de órbita
VALID_SPLIT_YEAR = 2020           # validación: entrena 2012-2019, prueba 2020-2022
MIN_CALIB_MONTHS = 24             # mínimo de meses con fuego para un ajuste regional

FIRMS_MAP_KEY = os.getenv("FIRMS_MAP_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://fire:fire@localhost:5432/fire")
