"""Paso 2. Límites departamentales de Perú (geoBoundaries ADM1, dominio público)."""
import json

import requests
from shapely.geometry import MultiPolygon, shape

from config import REGIONS_DIR, REGIONS_URL


def main() -> None:
    REGIONS_DIR.mkdir(parents=True, exist_ok=True)
    r = requests.get(REGIONS_URL, timeout=120)
    r.raise_for_status()
    gj = r.json()
    feats = []
    for f in gj["features"]:
        props = f["properties"]
        geom = shape(f["geometry"])
        if geom.geom_type == "Polygon":
            geom = MultiPolygon([geom])
        feats.append({
            "type": "Feature",
            "properties": {
                "region_id": (props.get("shapeISO") or props["shapeID"]).rstrip("*"),  # p. ej. PE-UCA
                "name": props["shapeName"],
            },
            "geometry": geom.__geo_interface__,
        })
    out = REGIONS_DIR / "peru_departamentos.geojson"
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"{len(feats)} regiones -> {out}")


if __name__ == "__main__":
    main()
