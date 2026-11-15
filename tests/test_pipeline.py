"""Prueba de punta a punta sin internet:  pytest -q   (desde la raíz del proyecto)
Genera datos sintéticos en una carpeta temporal, corre la armonización y revisa el resultado.
Si DATABASE_URL apunta a un PostGIS de prueba, también carga y consulta la API."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("proj")
    for d in ("pipeline", "sql", "api", "web"):
        shutil.copytree(ROOT / d, tmp / d)
    subprocess.run([sys.executable, str(ROOT / "tests/make_synthetic.py"), str(tmp)], check=True)
    subprocess.run([sys.executable, "harmonize.py"], cwd=tmp / "pipeline", check=True)
    return tmp


def test_calibration_recovers_known_relation(project):
    f = pd.read_csv(project / "data/processed/calibration_factors.csv")
    nat = f[(f.region_id == "PE") & (f.metric == "hs")].iloc[0]
    assert 0.75 < nat.b < 0.95              # los datos se generaron con b = 0.87
    v = json.loads((project / "data/processed/validation.json").read_text())
    assert v["hs"]["national_month"]["r2"] > 0.9
    assert 60 < v["hs"]["range_coverage_pct"] <= 100


def test_single_series_is_continuous(project):
    cd = pd.read_parquet(project / "data/processed/fire_cell_day.parquet")
    cd["year"] = pd.to_datetime(cd["date"]).dt.year
    by_year = cd.groupby("year")["hs_h"].sum()
    before, after = by_year.loc[2005:2011].mean(), by_year.loc[2013:2019].mean()
    assert 0.6 < before / after < 1.6       # sin salto artificial al cambiar de sensor
    assert set(cd["source"]) == {"MODIS_ADJ", "VIIRS"}
    assert (cd["hs_lo"] <= cd["hs_h"] + 1e-6).all() and (cd["hs_hi"] >= cd["hs_h"] - 1e-6).all()


def test_build_web(project):
    subprocess.run([sys.executable, "build_web.py"], cwd=project / "pipeline", check=True)
    html = (project / "web/story/index.html").read_text(encoding="utf-8")
    assert "__DATA__" not in html and "META;" in html
    data = html.split('id="data">', 1)[1].split("</script>", 1)[0].strip().splitlines()
    regions = [l for l in data if l.startswith("PE-")]
    assert len(regions) == 2
    meta = data[0].split(";")
    m = regions[0].split(";")[2].split(",")
    assert len(m) == int(meta[1]) * 12          # un valor MODIS por mes desde 2003


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="define TEST_DATABASE_URL para probar PostGIS + API")
def test_load_and_api(project, monkeypatch):
    env = {**os.environ, "DATABASE_URL": os.environ["TEST_DATABASE_URL"]}
    subprocess.run([sys.executable, "load_postgis.py"], cwd=project / "pipeline", check=True, env=env)
    monkeypatch.setenv("DATABASE_URL", env["DATABASE_URL"])
    sys.path.insert(0, str(project))
    from fastapi.testclient import TestClient
    import api.main as m
    m.DATABASE_URL = env["DATABASE_URL"]
    c = TestClient(m.app)
    assert c.get("/health").status_code == 200
    cal = c.get("/calendar?region_id=PE-UCA").json()
    assert len(cal["years"]) == 20 and len(cal["climatology"]) == 12
    assert c.get("/calendar?region_id=PE").status_code == 200
    aoi = {"type": "Polygon", "coordinates": [[[-75, -10], [-74, -10], [-74, -9], [-75, -9], [-75, -10]]]}
    assert c.post("/calendar/aoi", json=aoi).status_code == 200
    assert c.get("/heatmap?start=2020-08-01&end=2020-09-30").json()["features"]
    assert c.get("/export.csv?start=2020-09-01&end=2020-09-05").text.startswith("date,")
    assert c.get("/").status_code == 200
