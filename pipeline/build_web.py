"""Paso 7. Regenera la web "Calendario del Fuego" con los datos reales del pipeline.

  python build_web.py          # después de harmonize.py

Lee data/processed/detections_clean.parquet y los departamentos, y escribe
web/story/index.html: una sola página con los datos incrustados (se abre con doble clic,
se publica tal cual o la sirve la API en /historia).
"""
import datetime as dt
import json

import numpy as np
import pandas as pd
from shapely.geometry import shape

from config import OUT, REGIONS_DIR, ROOT
from harmonize import viirs_reference

Y0, VIIRS_FROM = 2003, 2012
TEMPLATE = ROOT / "web" / "story" / "template.html"
TARGET = ROOT / "web" / "story" / "index.html"


def monthly_counts(df: pd.DataFrame, regions: list[str], y1: int) -> pd.DataFrame:
    """Conteo por departamento y mes (filas = meses desde Y0 hasta diciembre de y1, ceros incluidos)."""
    months = pd.period_range(f"{Y0}-01", f"{y1}-12", freq="M")
    g = df.groupby(["region_id", df["acq_date_local"].dt.to_period("M")]).size()
    return g.unstack("region_id").reindex(index=months, columns=regions).fillna(0).astype(int)


def rings(geom) -> list[str]:
    """Contorno simplificado en enteros de 1/20 de grado (pocos KB para los 26 departamentos)."""
    out = []
    polys = getattr(geom.simplify(0.03, preserve_topology=True), "geoms", None) or [geom.simplify(0.03)]
    for p in polys:
        pts, last = [], None
        for x, y in p.exterior.coords:
            q = (round(x * 20), round(y * 20))
            if q != last:
                pts.append(f"{q[0]},{q[1]}")
                last = q
        if len(pts) >= 4:
            out.append(" ".join(pts))
    return out


def main() -> None:
    det = pd.read_parquet(OUT / "detections_clean.parquet",
                          columns=["family", "satellite", "acq_date_local", "latitude", "longitude", "region_id"])
    det = det[det["region_id"].notna() & (det["acq_date_local"] >= f"{Y0}-01-01")]
    gj = json.loads((REGIONS_DIR / "peru_departamentos.geojson").read_text())
    feats = {f["properties"]["region_id"]: f for f in gj["features"]}
    regions = list(feats)

    y1 = int(det["acq_date_local"].dt.year.max())
    ny = y1 - Y0 + 1
    vref = viirs_reference(det)
    mod = monthly_counts(det[det["family"].eq("MODIS")], regions, y1)
    vir = monthly_counts(vref, regions, y1)
    vall = monthly_counts(det[det["family"].eq("VIIRS")], regions, y1)
    vall_year = vall.groupby(vall.index.year).sum()

    # Mapa de calor: último año completo (con datos en diciembre), celdas de 0,25°
    last = det["acq_date_local"].max()
    hy = y1 if last.month == 12 and last.day >= 25 else y1 - 1
    h = vref[vref["acq_date_local"].dt.year.eq(hy)]
    hx = np.floor((h["longitude"] + 81.5) * 4).astype(int)
    hyy = np.floor((h["latitude"] + 18.5) * 4).astype(int)
    heat = pd.Series(1, index=pd.MultiIndex.from_arrays([hx, hyy])).groupby(level=[0, 1]).size()
    heat = heat[heat >= 3]

    lines = [f"META;{ny};{hy}"]
    vi = (VIIRS_FROM - Y0) * 12
    for rid in regions:
        f = feats[rid]
        lines.append(";".join([
            rid, f["properties"]["name"],
            ",".join(map(str, mod[rid].tolist())),
            ",".join(map(str, vir[rid].tolist()[vi:])),
            ",".join(str(int(vall_year.at[y, rid])) if y in vall_year.index else "0" for y in range(VIIRS_FROM, y1 + 1)),
            "|".join(rings(shape(f["geometry"]))),
        ]))
    lines.append("HEAT;" + " ".join(f"{x},{y},{c}" for (x, y), c in heat.items()))

    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", "\n".join(lines))
    TARGET.write_text(html, encoding="utf-8")
    print(f"web/story/index.html: {len(regions)} departamentos, {Y0}–{y1}, mapa de calor {hy}, "
          f"{len(html) / 1024:.0f} KB · {dt.datetime.now():%Y-%m-%d %H:%M}")


if __name__ == "__main__":
    main()
