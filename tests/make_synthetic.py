"""Genera datos falsos con el formato de FIRMS (2 departamentos, 2003-2022) para probar sin internet.
La relación real usada es V = 12.8 * M^0.87, así la prueba verifica que el ajuste la recupere."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

COLS_M = "latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_t31,frp,daynight,type".split(",")
COLS_V = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight,type".split(",")
REGS = {"PE-UCA": (-76, -10, -73, -7), "PE-CUS": (-73, -15, -70, -12)}
SEASON = np.array([.3, .2, .2, .3, .5, 1, 2.5, 8, 10, 3, 1.5, .6])


def main(root: Path) -> None:
    rng = np.random.default_rng(0)
    (root / "data/raw/sp").mkdir(parents=True, exist_ok=True)
    (root / "data/regions").mkdir(parents=True, exist_ok=True)
    feats = [{"type": "Feature", "properties": {"region_id": k, "name": k},
              "geometry": {"type": "MultiPolygon", "coordinates": [[[[w, s], [e, s], [e, n], [w, n], [w, s]]]]}}
             for k, (w, s, e, n) in REGS.items()]
    (root / "data/regions/peru_departamentos.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    for y in range(2003, 2023):
        yf = rng.lognormal(0, .3)
        for prod, first in [("modis", 2000), ("viirs-snpp", 2012), ("viirs-jpss1", 2018)]:
            if y < first:
                continue
            rows = []
            for k, (w, s, e, n) in REGS.items():
                for m in range(12):
                    nm = rng.poisson(SEASON[m] * yf * (40 if k == "PE-UCA" else 15))
                    count = nm if prod == "modis" else int(rng.poisson(12.8 * max(nm, 0.1) ** 0.87))
                    for _ in range(count):
                        d = (pd.Timestamp(y, m + 1, 1) + pd.Timedelta(days=int(rng.integers(0, 28)))).date()
                        la, lo = rng.uniform(s, n), rng.uniform(w, e)
                        if prod == "modis":
                            t = int(rng.choice([1530, 1830]))
                            rows.append([la, lo, 320, 1, 1, d, t, "Aqua" if t == 1830 else "Terra", "MODIS",
                                         int(rng.integers(0, 100)), "6.03", 300, rng.gamma(2, 10), "D",
                                         int(rng.choice([0] * 9 + [2]))])
                        else:
                            rows.append([la, lo, 330, .4, .5, d, 1800, "N" if "snpp" in prod else "N20", "VIIRS",
                                         rng.choice(["l", "n", "n", "h"]), "2", 290, rng.gamma(2, 4), "D", 0])
            pd.DataFrame(rows, columns=COLS_M if prod == "modis" else COLS_V).to_csv(
                root / f"data/raw/sp/{prod}_{y}.csv", index=False)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
