"""Paso 1. Descarga de hotspots FIRMS para Perú.

Dos modos:
  archive : CSV anuales por país (Standard Processing). No requiere MAP_KEY.
            python pipeline/download_firms.py archive --from 2003 --to 2025
  api     : Area API por bbox, en bloques de 5 días (SP reciente o NRT). Requiere FIRMS_MAP_KEY.
            python pipeline/download_firms.py api --start 2026-01-01 --end 2026-11-13 --mode sp
            python pipeline/download_firms.py api --start 2026-11-01 --end 2026-11-13 --mode nrt
"""
import argparse
import datetime as dt
import sys
import time

import requests

from config import (API_SOURCES_NRT, API_SOURCES_SP, COUNTRY, FIRMS_BASE, FIRMS_MAP_KEY,
                    PERU_BBOX, PRODUCTS, RAW_API, RAW_SP)


def get(url: str, retries: int = 4) -> requests.Response:
    for i in range(retries):
        try:
            r = requests.get(url, timeout=300)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            wait = 2 ** (i + 1)
            print(f"  reintento en {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError(f"No se pudo descargar {url}")


def download_archive(y0: int, y1: int, products: list[str]) -> None:
    RAW_SP.mkdir(parents=True, exist_ok=True)
    for prod in products:
        for year in range(y0, y1 + 1):
            if year < PRODUCTS[prod]["first_year"]:
                continue
            dest = RAW_SP / f"{prod}_{year}.csv"
            if dest.exists():
                continue
            url = f"{FIRMS_BASE}/data/country/{prod}/{year}/{prod}_{year}_{COUNTRY}.csv"
            r = get(url)
            if r.status_code == 404:
                print(f"  sin archivo para {prod} {year} (aún no publicado)")
                continue
            r.raise_for_status()
            dest.write_bytes(r.content)
            rows = r.content.count(b"\n") - 1
            print(f"  {dest.name}: {rows} filas")


def download_api(start: dt.date, end: dt.date, sources: list[str]) -> None:
    if not FIRMS_MAP_KEY:
        sys.exit("Define FIRMS_MAP_KEY (https://firms.modaps.eosdis.nasa.gov/api/map_key/)")
    RAW_API.mkdir(parents=True, exist_ok=True)
    bbox = ",".join(str(v) for v in PERU_BBOX)
    day = start
    while day <= end:
        n = min(5, (end - day).days + 1)  # el Area API acepta DAY_RANGE 1..5
        for src in sources:
            dest = RAW_API / f"{src}_{day:%Y%m%d}_{n}d.csv"
            if dest.exists():
                continue
            url = f"{FIRMS_BASE}/api/area/csv/{FIRMS_MAP_KEY}/{src}/{bbox}/{n}/{day:%Y-%m-%d}"
            r = get(url)
            r.raise_for_status()
            if not r.text.startswith("latitude"):
                print(f"  {src} {day}: respuesta inesperada: {r.text[:120]!r}")
                continue
            dest.write_text(r.text)
            print(f"  {dest.name}: {r.text.count(chr(10)) - 1} filas")
            time.sleep(0.5)  # límite: 5000 transacciones / 10 min
        day += dt.timedelta(days=n)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("archive")
    a.add_argument("--from", dest="y0", type=int, default=2003)
    a.add_argument("--to", dest="y1", type=int, default=dt.date.today().year - 1)
    a.add_argument("--products", nargs="+", default=list(PRODUCTS))
    b = sub.add_parser("api")
    b.add_argument("--start", type=dt.date.fromisoformat, required=True)
    b.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today())
    b.add_argument("--mode", choices=["sp", "nrt"], default="nrt")
    args = p.parse_args()
    if args.cmd == "archive":
        download_archive(args.y0, args.y1, args.products)
    else:
        download_api(args.start, args.end, API_SOURCES_SP if args.mode == "sp" else API_SOURCES_NRT)
