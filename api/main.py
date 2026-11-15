"""Paso 5. API de hotspots armonizados (FastAPI + PostGIS) y web app.

Ejecutar desde la raíz del proyecto:  uvicorn api.main:app --reload
Web app: http://localhost:8000/      Documentación: http://localhost:8000/docs
"""
import csv
import datetime as dt
import io
import json
import os
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Literal

import h3
import psycopg
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://fire:fire@localhost:5432/fire")
UNIT = "hotspots equivalentes VIIRS 375 m"
SWITCH = dt.date(2012, 1, 20)          # antes: MODIS ajustado (estimado); después: VIIRS observado
BASELINE = (2003, 2022)                # años de referencia para la mediana histórica
WEB = Path(__file__).resolve().parents[1] / "web"
AOI_EXAMPLE = {"type": "Polygon", "coordinates": [[[-70.5, -13], [-69.5, -13], [-69.5, -12], [-70.5, -12], [-70.5, -13]]]}

app = FastAPI(title="Hotspots armonizados MODIS–VIIRS · Perú", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """Los datos cambian una vez al día: el navegador puede reutilizar respuestas por 10 minutos."""
    resp = await call_next(request)
    if request.method == "GET" and resp.status_code == 200:
        resp.headers.setdefault("Cache-Control", "public, max-age=600")
    return resp


def q(sql: str, params: dict | tuple = ()) -> list[dict]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def check_region(region_id: str) -> str:
    r = q("SELECT name FROM region WHERE region_id = %s", (region_id,))
    if not r:
        raise HTTPException(404, f"región desconocida: {region_id}")
    return r[0]["name"]


# ------------------------------------------------------------ calendario
def build_calendar(monthly: list[dict], from_year: int, to_year: int) -> dict:
    """Matriz año × mes + mediana histórica por mes + estado frente a lo normal.
    `monthly` = filas {month: date, hs, hs_lo, hs_hi}. Los meses sin fuego cuentan como 0."""
    last = q("SELECT max(date) AS d FROM fire_cell_day")[0]["d"] or dt.date.today()
    by = {(m["month"].year, m["month"].month): m for m in monthly}
    series = {(y, mo): by.get((y, mo), {"hs": 0, "hs_lo": 0, "hs_hi": 0})
              for y in range(BASELINE[0], last.year + 1) for mo in range(1, 13)
              if dt.date(y, mo, 1) <= last}
    clim = {}
    for mo in range(1, 13):
        vals = [float(v["hs"]) for (y, m), v in series.items() if m == mo and BASELINE[0] <= y <= BASELINE[1]]
        p25, med, p75 = statistics.quantiles(vals, n=4, method="inclusive") if len(vals) > 1 else (0, 0, 0)
        clim[mo] = {"mon": mo, "median": round(med, 1), "p25": round(p25, 1), "p75": round(p75, 1)}
    years = []
    for y in range(max(from_year, BASELINE[0]), min(to_year, last.year) + 1):
        months = []
        for mo in range(1, 13):
            if (y, mo) not in series:
                months.append(None)
                continue
            v, c = series[(y, mo)], clim[mo]
            hs = float(v["hs"])
            months.append({
                "mon": mo, "hs": round(hs, 1),
                "hs_lo": round(float(v["hs_lo"]), 1), "hs_hi": round(float(v["hs_hi"]), 1),
                "estimated": dt.date(y, mo, 1) < SWITCH.replace(day=1),
                "pct_of_median": round(hs / c["median"] * 100) if c["median"] > 0 else None,
                "status": "alto" if hs > c["p75"] else "bajo" if hs < c["p25"] else "normal",
            })
        years.append({"year": y, "total": round(sum(m["hs"] for m in months if m), 1), "months": months})
    cur = series.get((last.year, last.month))
    current = None
    if cur is not None:
        c = clim[last.month]
        current = {"year": last.year, "mon": last.month, "hs": round(float(cur["hs"]), 1),
                   "median": c["median"], "partial_month": True,
                   "pct_of_median": round(float(cur["hs"]) / c["median"] * 100) if c["median"] > 0 else None}
    return {"unit": UNIT, "baseline": f"{BASELINE[0]}-{BASELINE[1]}", "last_date": last,
            "estimated_before": SWITCH, "climatology": list(clim.values()), "current_month": current,
            "years": years}


@app.get("/calendar")
def calendar(region_id: str, from_year: int = 2003, to_year: int = dt.date.today().year):
    """Calendario de quemas (año × mes) con anomalía frente a la mediana. region_id=PE = todo Perú."""
    if region_id == "PE":  # todo el país
        name = "Perú"
        monthly = q("""SELECT month, sum(hs) AS hs, sum(hs_lo) AS hs_lo, sum(hs_hi) AS hs_hi
                       FROM region_month GROUP BY month""")
    else:
        name = check_region(region_id)
        monthly = q("SELECT month, hs, hs_lo, hs_hi FROM region_month WHERE region_id = %s", (region_id,))
    return {"region_id": region_id, "name": name, **build_calendar(monthly, from_year, to_year)}


AOI_SQL = "ST_Intersects(geom, ST_SetSRID(ST_GeomFromGeoJSON(%(g)s), 4326))"


@app.post("/calendar/aoi")
def calendar_aoi(geometry: dict = Body(..., examples=[AOI_EXAMPLE]), from_year: int = 2003,
                 to_year: int = dt.date.today().year):
    """El mismo calendario para un área dibujada por el usuario (GeoJSON Polygon/MultiPolygon)."""
    monthly = q(f"""SELECT date_trunc('month', date)::date AS month, sum(hs_h) AS hs,
                           sum(hs_lo) AS hs_lo, sum(hs_hi) AS hs_hi
                    FROM fire_cell_day WHERE {AOI_SQL} GROUP BY 1""", {"g": json.dumps(geometry)})
    return {"region_id": "AOI", "name": "Área dibujada", **build_calendar(monthly, from_year, to_year)}


@app.get("/anomaly")
def anomaly(region_id: str, year: int, month: int):
    """¿Un mes estuvo por encima de lo normal? Compara con la mediana y el rango p25-p75 del mismo mes."""
    cal = calendar(region_id, year, year)
    m = cal["years"][0]["months"][month - 1] if cal["years"] else None
    if not m:
        raise HTTPException(404, "sin datos para ese mes")
    return {"region_id": region_id, "year": year, **m, **cal["climatology"][month - 1], "unit": UNIT}


# ------------------------------------------------------------ otros endpoints
@app.get("/health")
def health():
    r = q("SELECT min(date) AS first_date, max(date) AS last_date, count(*) AS cell_days FROM fire_cell_day")[0]
    v = q("SELECT max(version) AS v FROM calibration_factor")[0]["v"]
    return {"status": "ok", **r, "calibration_version": v, "unit": UNIT}


@app.get("/regions")
def regions(geometry: bool = False):
    geom = ", ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.01))::json AS geometry" if geometry else ""
    return q(f"SELECT region_id, name, round(area_km2) AS area_km2{geom} FROM region ORDER BY name")


@app.get("/calendar/daily")
def calendar_daily(region_id: str, year: int):
    """Serie diaria de un año (calendario de 365 celdas)."""
    check_region(region_id)
    return q("""SELECT date, round(hs::numeric, 1) AS hs, round(frp::numeric) AS frp, active_cells, source
                FROM region_day WHERE region_id = %s AND date >= make_date(%s,1,1) AND date < make_date(%s+1,1,1)
                ORDER BY date""", (region_id, year, year))


@app.get("/timeseries")
def timeseries(start: dt.date, end: dt.date, region_id: str | None = None,
               freq: Literal["day", "month", "year"] = "month"):
    where, params = "date BETWEEN %(s)s AND %(e)s", {"s": start, "e": end, "f": freq}
    if region_id:
        where += " AND region_id = %(r)s"; params["r"] = region_id
    return q(f"""SELECT date_trunc(%(f)s, date)::date AS period, round(sum(hs)::numeric, 1) AS hs,
                        round(sum(hs_lo)::numeric, 1) AS hs_lo, round(sum(hs_hi)::numeric, 1) AS hs_hi,
                        round(sum(frp)::numeric) AS frp
                 FROM region_day WHERE {where} GROUP BY 1 ORDER BY 1""", params)


@app.get("/ranking")
def ranking(start: dt.date, end: dt.date):
    """Departamentos ordenados por hotspots en el rango, con densidad por 1000 km²."""
    return q("""SELECT r.region_id, r.name, round(sum(d.hs)::numeric, 1) AS hs,
                       round((sum(d.hs) / r.area_km2 * 1000)::numeric, 2) AS hs_per_1000km2
                FROM region_day d JOIN region r USING (region_id)
                WHERE d.date BETWEEN %s AND %s
                GROUP BY r.region_id, r.name, r.area_km2 ORDER BY hs DESC""", (start, end))


@app.get("/heatmap")
def heatmap(start: dt.date, end: dt.date, region_id: str | None = None,
            res: int = Query(6, ge=4, le=7), fmt: Literal["json", "geojson"] = "geojson"):
    """Hexágonos H3 con la suma de hotspots armonizados en el rango (mapa de calor)."""
    where, params = "date BETWEEN %(s)s AND %(e)s", {"s": start, "e": end}
    if region_id:
        where += " AND region_id = %(r)s"; params["r"] = region_id
    rows = q(f"SELECT cell_h3, sum(hs_h) AS hs, sum(frp_h) AS frp FROM fire_cell_day WHERE {where} GROUP BY cell_h3", params)
    cells: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        c = r["cell_h3"] if res == 7 else h3.cell_to_parent(r["cell_h3"], res)
        cells[c][0] += r["hs"]; cells[c][1] += r["frp"] or 0
    if fmt == "json":
        return [{"h3": c, "lat": h3.cell_to_latlng(c)[0], "lon": h3.cell_to_latlng(c)[1],
                 "hs": round(v[0], 2), "frp": round(v[1], 1)} for c, v in cells.items()]
    feats = []
    for c, v in cells.items():
        ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(c)]
        feats.append({"type": "Feature", "properties": {"h3": c, "hs": round(v[0], 2), "frp": round(v[1], 1)},
                      "geometry": {"type": "Polygon", "coordinates": [ring + ring[:1]]}})
    return {"type": "FeatureCollection", "features": feats}


CSV_FIELDS = ["date", "region_id", "cell_h3", "lat", "lon", "hs_h", "hs_lo", "hs_hi", "frp_h", "source",
              "modis_n", "viirs_n", "k_hs", "calib_version"]


def csv_response(rows: list[dict], name: str) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader(); w.writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/export.csv")
def export_csv(start: dt.date, end: dt.date, region_id: str | None = None):
    """CSV descargable de hotspots armonizados (celda-día) de un departamento o de todo Perú."""
    where, params = "date BETWEEN %(s)s AND %(e)s", {"s": start, "e": end}
    if region_id:
        where += " AND region_id = %(r)s"; params["r"] = region_id
    rows = q(f"SELECT {', '.join(CSV_FIELDS)} FROM fire_cell_day WHERE {where} ORDER BY date, region_id", params)
    return csv_response(rows, f"hotspots_{region_id or 'PE'}_{start}_{end}.csv")


@app.post("/export/aoi.csv")
def export_aoi_csv(start: dt.date, end: dt.date, geometry: dict = Body(..., examples=[AOI_EXAMPLE])):
    """CSV descargable para un área dibujada."""
    rows = q(f"""SELECT {', '.join(CSV_FIELDS)} FROM fire_cell_day
                 WHERE date BETWEEN %(s)s AND %(e)s AND {AOI_SQL} ORDER BY date""",
             {"s": start, "e": end, "g": json.dumps(geometry)})
    return csv_response(rows, f"hotspots_area_{start}_{end}.csv")


@app.get("/calibration")
def calibration():
    """Parámetros del ajuste por departamento: transparencia del método."""
    return q("SELECT * FROM calibration_factor WHERE version = (SELECT max(version) FROM calibration_factor) "
             "ORDER BY metric, region_id")


@app.get("/historia", include_in_schema=False)
def story():
    """Web "Calendario del Fuego" (la genera pipeline/build_web.py con los datos reales)."""
    page = WEB / "story" / "index.html"
    if not page.exists():
        raise HTTPException(404, "Primero corre: cd pipeline && python build_web.py")
    return FileResponse(page)


@app.get("/", include_in_schema=False)
def web_app():
    return FileResponse(WEB / "index.html")
