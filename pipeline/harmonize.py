"""Paso 3. Limpieza, unificación y armonización MODIS -> escala VIIRS.

Uso:
  python harmonize.py                 # usa la calibración guardada si existe (la crea la primera vez)
  python harmonize.py --recalibrate   # vuelve a ajustar y crea una nueva versión

Salida (data/processed/):
  detections_clean.parquet   detecciones de ambos sensores con esquema común (filtradas)
  fire_cell_day.parquet      EL dataset armonizado: una fila por celda H3 res 7 y día local
  calibration_factors.csv    parámetros por departamento y métrica (versionados, se congelan)
  validation.json            métricas de validación fuera de muestra

Idea: la serie se ancla en VIIRS (sensor que continúa). Para los días anteriores a VIIRS
se convierte MODIS a "hotspots equivalentes VIIRS" con V = s * exp(a) * M^b ajustada
por departamento sobre totales mensuales del periodo de solapamiento.
"""
import argparse
import datetime as dt
import json

import h3
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import shape

from config import (CALIB_YEARS, H3_RES, MIN_CALIB_MONTHS, NOAA20_FROM, OUT, RAW_API, RAW_SP,
                    REGIONS_DIR, SERIES_START, TZ_OFFSET_HOURS, VALID_SPLIT_YEAR,
                    VIIRS_SWITCH_DATE)

SAT_MAP = {"Terra": "TERRA", "T": "TERRA", "Aqua": "AQUA", "A": "AQUA", "N": "SNPP",
           "N20": "NOAA20", "1": "NOAA20", "N21": "NOAA21", "2": "NOAA21"}


# ---------------------------------------------------------------- 3.1 lectura
def read_raw() -> pd.DataFrame:
    frames = []
    for f in sorted(RAW_SP.glob("*.csv")):
        frames.append(pd.read_csv(f, dtype={"satellite": str, "confidence": str, "version": str})
                      .assign(processing="SP"))
    for f in sorted(RAW_API.glob("*.csv")):
        df = pd.read_csv(f, dtype={"satellite": str, "confidence": str, "version": str})
        if len(df):
            frames.append(df.assign(processing="NRT" if "_NRT_" in f.name else "SP"))
    if not frames:
        raise SystemExit("No hay CSV en data/raw. Ejecuta download_firms.py primero.")
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------- 3.2 esquema común
def standardize(df: pd.DataFrame) -> pd.DataFrame:
    is_modis = df["instrument"].str.upper().eq("MODIS")
    t = df["acq_time"].astype(int)
    ts = pd.to_datetime(df["acq_date"]) + pd.to_timedelta(t // 100, "h") + pd.to_timedelta(t % 100, "min")

    conf = df["confidence"].astype(str).str.strip().str.lower()
    conf_num = pd.to_numeric(conf, errors="coerce")
    conf_cls = np.where(
        is_modis,
        np.select([conf_num < 30, conf_num < 80], ["low", "nominal"], "high"),  # MODIS 0-100
        conf.str[0].map({"l": "low", "n": "nominal", "h": "high"}),             # VIIRS l/n/h
    )

    def col(name):
        return df[name] if name in df else pd.Series(np.nan, index=df.index)

    out = pd.DataFrame({
        "family": np.where(is_modis, "MODIS", "VIIRS"),
        "satellite": df["satellite"].astype(str).map(SAT_MAP).fillna(df["satellite"].astype(str)),
        "processing": df["processing"],
        "acq_ts": ts,  # UTC
        "latitude": df["latitude"].astype(float),
        "longitude": df["longitude"].astype(float),
        "confidence_raw": df["confidence"].astype(str),
        "confidence_class": conf_cls,
        "frp": col("frp").astype(float),
        "brightness": col("brightness").fillna(col("bright_ti4")).astype(float),
        "bright2": col("bright_t31").fillna(col("bright_ti5")).astype(float),
        "scan": col("scan").astype(float),
        "track": col("track").astype(float),
        "daynight": col("daynight").astype(str),
        "fire_type": pd.to_numeric(col("type"), errors="coerce"),  # solo existe en SP
        "version": df["version"].astype(str),
    })
    out["acq_date_local"] = (out["acq_ts"] + pd.Timedelta(hours=TZ_OFFSET_HOURS)).dt.normalize()
    return out


# ------------------------------------------------------- 3.3 limpieza y filtros
def clean(det: pd.DataFrame) -> pd.DataFrame:
    n0 = len(det)
    # SP sustituye a NRT: deduplicar por satélite+hora+posición, prefiriendo SP
    det = (det.assign(_p=det["processing"].eq("SP").astype(int),
                      _la=det["latitude"].round(3), _lo=det["longitude"].round(3))
              .sort_values("_p", ascending=False)
              .drop_duplicates(["satellite", "acq_ts", "_la", "_lo"])
              .drop(columns=["_p", "_la", "_lo"]))
    det["cell_h3"] = [h3.latlng_to_cell(la, lo, H3_RES)
                      for la, lo in zip(det["latitude"], det["longitude"])]
    # Máscara de fuentes estáticas: celdas que alguna vez fueron volcán (1), fuente estática (2)
    # u offshore (3) en SP. Se aplica también a NRT, que no trae 'type'.
    static_cells = set(det.loc[det["fire_type"].isin([1, 2, 3]), "cell_h3"])
    keep = (
        det["confidence_class"].isin(["nominal", "high"])
        & (det["fire_type"].isna() | det["fire_type"].eq(0))
        & ~det["cell_h3"].isin(static_cells)
        & (det["acq_date_local"] >= pd.Timestamp(SERIES_START))
    )
    det = det[keep].copy()
    print(f"limpieza: {n0} -> {len(det)} detecciones ({len(static_cells)} celdas estáticas)")
    return det


# ------------------------------------------------------- 3.4 región por celda
def assign_regions(det: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    gj = json.loads((REGIONS_DIR / "peru_departamentos.geojson").read_text())
    regions = [{"region_id": f["properties"]["region_id"], "name": f["properties"]["name"],
                "geom": shape(f["geometry"])} for f in gj["features"]]
    cells = pd.Series(det["cell_h3"].unique())
    ll = np.array([h3.cell_to_latlng(c) for c in cells])
    lat, lon = ll[:, 0], ll[:, 1]
    rid = np.full(len(cells), None, dtype=object)
    for r in regions:
        shapely.prepare(r["geom"])
        inside = shapely.contains_xy(r["geom"], lon, lat) & (rid == None)  # noqa: E711
        rid[inside] = r["region_id"]
    # Detecciones en el borde: usar la posición de la detección como respaldo
    lut = dict(zip(cells, rid))
    det["region_id"] = det["cell_h3"].map(lut)
    miss = det["region_id"].isna()
    if miss.any():
        for r in regions:
            m = miss & shapely.contains_xy(r["geom"], det["longitude"].to_numpy(), det["latitude"].to_numpy())
            det.loc[m, "region_id"] = r["region_id"]
            miss &= ~m
    det = det[det["region_id"].notna()].copy()
    return det, regions


# ------------------------------------- 3.5 serie de referencia VIIRS (un satélite)
def viirs_reference(det: pd.DataFrame) -> pd.DataFrame:
    """Un solo satélite VIIRS por día para que el número de pasadas no cambie en el tiempo:
    NOAA-20 desde 2018-04 (sigue operando); S-NPP antes o si NOAA-20 falta ese día;
    NOAA-21 como último respaldo (S-NPP dejó de entregarse el 2026-11-02)."""
    v = det[det["family"].eq("VIIRS")]
    present = v.groupby("acq_date_local")["satellite"].agg(set)
    pref_new, pref_old = ["NOAA20", "SNPP", "NOAA21"], ["SNPP", "NOAA20", "NOAA21"]
    chosen = {d: next((s for s in (pref_new if d >= pd.Timestamp(NOAA20_FROM) else pref_old)
                       if s in sats), None) for d, sats in present.items()}
    return v[v["satellite"].eq(v["acq_date_local"].map(chosen))]


# --------------------------------------------------- 3.6 calibración por región
def monthly(df: pd.DataFrame, label: str) -> pd.DataFrame:
    g = df.groupby(["region_id", df["acq_date_local"].dt.to_period("M")])
    return pd.DataFrame({f"{label}_hs": g.size(), f"{label}_frp": g["frp"].sum()})


def fit_powerlaw(m, v, high=None) -> dict:
    """log(V+1) = a + d*temporada_alta + b*log(M+1). Sin 'high' es la ley de potencia simple (d=0)."""
    x, y = np.log1p(np.asarray(m, float)), np.log1p(np.asarray(v, float))
    h = np.zeros_like(x) if high is None else np.asarray(high, float)
    if h.min() == h.max():  # sin las dos temporadas en los datos -> sin término estacional
        b, a = np.polyfit(x, y, 1); d = 0.0
    else:
        (a, d, b), *_ = np.linalg.lstsq(np.column_stack([np.ones_like(x), h, x]), y, rcond=None)
    res = y - (a + d * h + b * x)
    return {"a": float(a), "d": float(d), "b": float(b),
            "smear": float(np.mean(np.exp(res))),            # corrección de Duan
            "res_p10": float(np.quantile(res, 0.10)),        # rango simple de incertidumbre:
            "res_p90": float(np.quantile(res, 0.90))}        # percentiles 10 y 90 del error


def predict(p: dict, m, high=0, q: str | None = None):
    """Estimación central (q=None) o límites del rango (q='lo' / 'hi')."""
    lin = p["a"] + p["d"] * np.asarray(high, float) + p["b"] * np.log1p(np.asarray(m, float))
    if q is None:
        return np.maximum(p["smear"] * np.exp(lin) - 1, 0)
    return np.maximum(np.exp(lin + (p["res_p10"] if q == "lo" else p["res_p90"])) - 1, 0)


def season_map(full: pd.DataFrame) -> dict[str, list[int]]:
    """Temporada alta de cada departamento = meses cuyo promedio MODIS supera su promedio anual.
    Se calcula con el registro largo de MODIS (2003-2022), así cada microclima define la suya."""
    base = full[full["year"] <= CALIB_YEARS[1]]
    clim = base.groupby(["region_id", "mon"])["MODIS_hs"].mean().unstack(fill_value=0)
    return {rid: [int(m) for m in row.index if row[m] >= row.mean() and row.mean() > 0]
            for rid, row in clim.iterrows()}


def scores(obs, pred) -> dict:
    obs, pred = np.asarray(obs, float), np.asarray(pred, float)
    ss = ((obs - pred) ** 2).sum(); st = ((obs - obs.mean()) ** 2).sum()
    mape = np.mean(np.abs(pred - obs) / np.maximum(obs, 1)) * 100
    bias = (pred.sum() - obs.sum()) / max(obs.sum(), 1) * 100
    wape = np.abs(pred - obs).sum() / max(obs.sum(), 1) * 100  # error ponderado: el más fácil de explicar
    return {"r2": round(1 - ss / st, 3) if st else None, "wape": round(wape, 1), "mape": round(mape, 1),
            "bias_pct": round(bias, 1), "n": int(len(obs))}


def calibrate(full: pd.DataFrame, regions: list[dict], seasonal: bool = True):
    """Ajusta V = f(M, temporada) por departamento con los meses de solapamiento.
    Devuelve (factores, validación). La validación entrena 2012-2019 y prueba 2020-2022."""
    seasons = season_map(full)
    full = full.assign(high=[int(m in seasons.get(r, [])) for r, m in zip(full["region_id"], full["mon"])])
    cal = full[full["year"].between(*CALIB_YEARS) & (full["MODIS_hs"] + full["VIIRS_hs"] > 0)]
    version = f"v{dt.date.today():%Y%m%d}"
    rows, validation = [], {}
    for metric in ("hs", "frp"):
        M, V = f"MODIS_{metric}", f"VIIRS_{metric}"
        fit = lambda d: fit_powerlaw(d[M], d[V], d["high"] if seasonal else None)  # noqa: E731
        train, test = cal[cal["year"] < VALID_SPLIT_YEAR], cal[cal["year"] >= VALID_SPLIT_YEAR]
        nat, nat_tr = fit(cal), fit(train)
        tests = []
        for r in regions:
            rid = r["region_id"]
            tr, te, al = (d[d["region_id"].eq(rid)] for d in (train, test, cal))
            # Ajuste propio solo si el departamento tiene suficientes meses con fuego
            p, p_tr, method = nat, nat_tr, "national"
            if (al[M] > 0).sum() >= MIN_CALIB_MONTHS and (tr[M] > 0).sum() >= MIN_CALIB_MONTHS // 2:
                cand = fit(al)
                if 0.5 <= cand["b"] <= 1.3:  # pendiente razonable; si no, respaldo nacional
                    p, p_tr, method = cand, fit(tr), "region"
            if len(te):
                tests.append(te.assign(pred=predict(p_tr, te[M], te["high"]),
                                       lo=predict(p_tr, te[M], te["high"], "lo"),
                                       hi=predict(p_tr, te[M], te["high"], "hi")))
            s = scores(te[V], tests[-1]["pred"]) if len(te) else {}
            rows.append({"region_id": rid, "metric": metric, "method": method, **p,
                         "high_months": ",".join(map(str, seasons.get(rid, []))),
                         "n_months": int((al[M] > 0).sum()), "r2_test": s.get("r2"),
                         "mape_test": s.get("mape"), "version": version})
        rows.append({"region_id": "PE", "metric": metric, "method": "national", **nat, "high_months": "",
                     "n_months": int((cal[M] > 0).sum()), "r2_test": None, "mape_test": None,
                     "version": version})
        pt = pd.concat(tests)
        nat_test = pt.groupby("acq_date_local")[[V, "pred"]].sum()
        validation[metric] = {
            "version": version, "seasonal": seasonal,
            "train_years": f"{CALIB_YEARS[0]}-{VALID_SPLIT_YEAR - 1}",
            "test_years": f"{VALID_SPLIT_YEAR}-{CALIB_YEARS[1]}",
            "region_month": scores(pt[V], pt["pred"]),
            "national_month": scores(nat_test[V], nat_test["pred"]),
            # % de meses de prueba cuyo valor real cayó dentro del rango p10-p90 (ideal ~80%)
            "range_coverage_pct": round(float(((pt[V] >= pt["lo"]) & (pt[V] <= pt["hi"])).mean()) * 100, 1),
        }
    return pd.DataFrame(rows), validation


def monthly_table(mod_m: pd.DataFrame, vref_m: pd.DataFrame) -> pd.DataFrame:
    full = mod_m.join(vref_m, how="outer").fillna(0).reset_index()
    full["year"] = full["acq_date_local"].dt.year
    full["mon"] = full["acq_date_local"].dt.month
    return full


# ---------------------------------------- 3.7 dataset armonizado celda-día
def harmonize(det: pd.DataFrame, vref: pd.DataFrame, factors: pd.DataFrame, full_m: pd.DataFrame) -> pd.DataFrame:
    switch = pd.Timestamp(VIIRS_SWITCH_DATE)
    keys = ["cell_h3", "acq_date_local", "region_id"]
    agg = {"n": ("frp", "size"), "frp": ("frp", "sum")}
    mod = det[det["family"].eq("MODIS")].groupby(keys).agg(**agg).add_prefix("modis_")
    vir = vref.groupby(keys).agg(**agg).add_prefix("viirs_")
    cd = mod.join(vir, how="outer").fillna(0).reset_index()
    idx = pd.MultiIndex.from_arrays([cd["region_id"], cd["acq_date_local"].dt.to_period("M")])

    # Factor efectivo por departamento-mes: k = V_estimado(M_mes) / M_mes.
    # Cada celda-día MODIS se multiplica por k, así la suma reproduce el total mensual estimado.
    fm = full_m.set_index(["region_id", "acq_date_local"])
    def factor(metric: str, q: str | None) -> np.ndarray:
        f = factors[factors["metric"].eq(metric)].set_index("region_id")
        mm = fm[f"MODIS_{metric}"]
        rid = mm.index.get_level_values(0)
        par = f.reindex(rid)
        p = {c: par[c].to_numpy() for c in ("a", "d", "b", "smear", "res_p10", "res_p90")}
        hm = f["high_months"].fillna("").astype(str).to_dict()
        high = [int(str(per.month) in hm.get(r, "").split(",")) for r, per in mm.index]
        k = np.where(mm > 0, predict(p, mm.to_numpy(), high, q) / np.maximum(mm.to_numpy(), 1e-9), np.nan)
        return pd.Series(k, index=mm.index).reindex(idx).to_numpy()

    pre = (cd["acq_date_local"] < switch).to_numpy()
    cd["k_hs"] = np.where(pre, factor("hs", None), np.nan)
    cd["source"] = np.where(pre, "MODIS_ADJ", "VIIRS")
    cd["hs_h"] = np.where(pre, cd["modis_n"] * cd["k_hs"], cd["viirs_n"])
    cd["hs_lo"] = np.where(pre, cd["modis_n"] * factor("hs", "lo"), cd["viirs_n"])
    cd["hs_hi"] = np.where(pre, cd["modis_n"] * factor("hs", "hi"), cd["viirs_n"])
    cd["frp_h"] = np.where(pre, cd["modis_frp"] * factor("frp", None), cd["viirs_frp"])
    cd["calib_version"] = factors["version"].iloc[0]
    cd = cd[cd["hs_h"] > 0]
    ll = np.array([h3.cell_to_latlng(c) for c in cd["cell_h3"]])
    cd["lat"], cd["lon"] = ll[:, 0], ll[:, 1]
    return cd.rename(columns={"acq_date_local": "date"})[
        ["cell_h3", "date", "region_id", "lat", "lon", "hs_h", "hs_lo", "hs_hi", "frp_h", "source",
         "modis_n", "viirs_n", "k_hs", "calib_version"]].astype({"modis_n": "int32", "viirs_n": "int32"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recalibrate", action="store_true", help="ajustar de nuevo y crear otra versión")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    det = clean(standardize(read_raw()))
    det, regions = assign_regions(det)
    det.to_parquet(OUT / "detections_clean.parquet", index=False)

    vref = viirs_reference(det)
    full_m = monthly_table(monthly(det[det["family"].eq("MODIS")], "MODIS"), monthly(vref, "VIIRS"))
    fac_path = OUT / "calibration_factors.csv"
    if fac_path.exists() and not args.recalibrate:
        # Calibración congelada: las cargas diarias no cambian la historia ya publicada
        factors = pd.read_csv(fac_path)
        print(f"usando calibración guardada {factors['version'].iloc[0]}")
    else:
        factors, validation = calibrate(full_m, regions, seasonal=True)
        _, base = calibrate(full_m, regions, seasonal=False)  # para comparar en el pitch
        validation["hs_without_season"] = base["hs"]
        factors.to_csv(fac_path, index=False)
        (OUT / "validation.json").write_text(json.dumps(validation, indent=2))
        print(json.dumps(validation, indent=2))

    cd = harmonize(det, vref, factors, full_m)
    cd.to_parquet(OUT / "fire_cell_day.parquet", index=False)
    print(f"fire_cell_day: {len(cd)} filas, {cd['date'].min():%Y-%m-%d} a {cd['date'].max():%Y-%m-%d}")


if __name__ == "__main__":
    main()
