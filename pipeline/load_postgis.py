"""Paso 4. Carga en PostgreSQL/PostGIS.

  python load_postgis.py                     # carga completa: reconstruye las tablas (primera vez o tras --recalibrate)
  python load_postgis.py --since 2026-11-10  # carga diaria: reemplaza solo los días desde esa fecha
  --skip-detections                          # no sube el detalle por detección (ahorra espacio en
                                             # planes gratuitos; la API solo usa fire_cell_day)
"""
import argparse
import datetime as dt
import io
import json

import pandas as pd
import psycopg

from config import DATABASE_URL, OUT, REGIONS_DIR, ROOT

DET_COLS = ["family", "satellite", "processing", "acq_ts", "acq_date_local", "latitude", "longitude",
            "confidence_raw", "confidence_class", "frp", "brightness", "bright2", "scan", "track",
            "daynight", "fire_type", "version", "cell_h3", "region_id"]


def copy_df(cur, table: str, df: pd.DataFrame) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    with cur.copy(f"COPY {table} ({', '.join(df.columns)}) FROM STDIN WITH (FORMAT csv, NULL '\\N')") as cp:
        cp.write(buf.getvalue())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=dt.date.fromisoformat, help="solo reemplaza días >= esta fecha")
    ap.add_argument("--skip-detections", action="store_true", help="no cargar hotspot_detection")
    args = ap.parse_args()

    det = pd.read_parquet(OUT / "detections_clean.parquet")
    cd = pd.read_parquet(OUT / "fire_cell_day.parquet")
    if args.since:
        since = pd.Timestamp(args.since)
        det = det[det["acq_date_local"] >= since]
        cd = cd[pd.to_datetime(cd["date"]) >= since]
    det = det.assign(acq_ts=det["acq_ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     acq_date_local=det["acq_date_local"].dt.strftime("%Y-%m-%d"),
                     fire_type=det["fire_type"].astype("Int16"))[DET_COLS]
    cd = cd.assign(date=pd.to_datetime(cd["date"]).dt.strftime("%Y-%m-%d"))

    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        if args.since:
            # Carga diaria: borra y vuelve a insertar solo la ventana reciente (NRT -> SP)
            cur.execute("DELETE FROM fire_cell_day WHERE date >= %s", (args.since,))
            cur.execute("DELETE FROM hotspot_detection WHERE acq_date_local >= %s", (args.since,))
        else:
            # Carga completa = reconstruir desde cero (así un cambio de esquema nunca deja tablas viejas)
            cur.execute("DROP TABLE IF EXISTS fire_cell_day, hotspot_detection, calibration_factor, region CASCADE")
            cur.execute((ROOT / "sql" / "schema.sql").read_text())
            gj = json.loads((REGIONS_DIR / "peru_departamentos.geojson").read_text())
            for f in gj["features"]:
                g = json.dumps(f["geometry"])
                cur.execute(
                    """INSERT INTO region (region_id, name, geom, area_km2)
                       VALUES (%s, %s, ST_Multi(ST_GeomFromGeoJSON(%s)), ST_Area(ST_GeomFromGeoJSON(%s)::geography) / 1e6)
                       ON CONFLICT (region_id) DO UPDATE
                       SET name = EXCLUDED.name, geom = EXCLUDED.geom, area_km2 = EXCLUDED.area_km2""",
                    (f["properties"]["region_id"], f["properties"]["name"], g, g))
            fac = pd.read_csv(OUT / "calibration_factors.csv")
            copy_df(cur, "calibration_factor", fac[["region_id", "metric", "method", "a", "d", "b", "smear", "res_p10",
                                                   "res_p90", "high_months", "n_months", "r2_test", "mape_test",
                                                   "version"]])
        if not args.skip_detections:
            copy_df(cur, "hotspot_detection", det)
        copy_df(cur, "fire_cell_day", cd)
        cur.execute("REFRESH MATERIALIZED VIEW region_day")
        cur.execute("REFRESH MATERIALIZED VIEW region_month")
        conn.commit()
        cur.execute("SELECT count(*), min(date), max(date) FROM fire_cell_day")
        print("fire_cell_day:", cur.fetchone())


if __name__ == "__main__":
    main()
