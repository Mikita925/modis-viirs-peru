# Calendario de quemas armonizado MODIS–VIIRS · Perú

Prototipo para NASA Space Apps 2026, reto *Harmonization of MODIS and VIIRS Hot Spots*.
Une los hotspots de MODIS (2003–2012) y VIIRS (2012–hoy) en **una sola serie** en escala VIIRS,
la guarda en PostgreSQL + PostGIS y la muestra en una web app con mapa de calor, calendario de
quemas, comparación con lo normal, área dibujada y descarga en CSV.

```
FIRMS (CSV) ──► pipeline/ (Python) ──► PostgreSQL + PostGIS ──► api/ (FastAPI) ──► web/ (mapa + calendario)
```

## Contenido

| Carpeta / archivo | Qué hace |
| --- | --- |
| `pipeline/config.py` | Parámetros: periodo de calibración, fechas de cambio de sensor, grilla H3 |
| `pipeline/download_firms.py` | Paso 1: descarga hotspots de FIRMS para Perú |
| `pipeline/download_regions.py` | Paso 2: descarga los 26 departamentos (geoBoundaries) |
| `pipeline/harmonize.py` | Paso 3: limpia, une y armoniza; crea `fire_cell_day.parquet` |
| `pipeline/load_postgis.py` | Paso 4: carga en PostGIS (completa o diaria) |
| `sql/schema.sql` | Tablas, índices y vistas del calendario |
| `api/main.py` | Paso 5: API + sirve la web en `/` |
| `web/index.html` | La web app (una sola página, sin compilar) |
| `pipeline/build_web.py` | Paso 7: regenera la web de presentación con tus datos reales |
| `web/story/` | Web de presentación "Calendario del Fuego" (`template.html` + `index.html` ya generado) |
| `tests/` | Prueba de punta a punta con datos sintéticos |
| `Dockerfile`, `docker-compose.yml` | Base de datos local y contenedor de la API |
| `.github/workflows/daily.yml` | Actualización diaria automática |

## Requisitos

- Python 3.11 o más reciente
- Docker Desktop (para la base de datos local)
- Una MAP_KEY gratuita de FIRMS (solo para datos de 2026 en adelante):
  https://firms.modaps.eosdis.nasa.gov/api/map_key/

## Paso a paso (en tu computadora)

### 0. Preparar el proyecto

```bash
cd modis-viirs-peru
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # luego edita .env y pega tu FIRMS_MAP_KEY
docker compose up -d db              # PostgreSQL 16 + PostGIS en localhost:5432
```

### 1. Descargar el histórico de FIRMS (sin MAP_KEY)

FIRMS publica un CSV por país y año (producto científico "SP"). El script baja MODIS (desde 2003),
VIIRS S-NPP (desde 2012) y VIIRS NOAA-20 (desde 2018) hasta 2025.

```bash
cd pipeline
python download_firms.py archive --from 2003 --to 2025
```

Resultado: `data/raw/sp/modis_2003.csv` … (unos 45 archivos).

### 2. Completar 2026 (con MAP_KEY)

El año en curso todavía no está en los CSV por país; se pide al Area API de FIRMS en bloques de 5 días.

```bash
python download_firms.py api --start 2026-01-01 --end 2026-11-13 --mode sp    # procesado científico
python download_firms.py api --start 2026-11-01 --mode nrt                    # casi tiempo real
```

Si el paso *sp* responde "respuesta inesperada" o 0 filas en los últimos meses (FIRMS publica el SP con
unos meses de atraso), adelanta el corte: por ejemplo `--end 2026-06-30` en *sp* y `--start 2026-07-01` en *nrt*.

**¿Ya cargaste la base sin 2026?** No hace falta empezar de nuevo: corre este paso y luego
`python harmonize.py`, `python load_postgis.py --since 2026-01-01` y `python build_web.py`.

### 3. Descargar los departamentos

```bash
python download_regions.py        # data/regions/peru_departamentos.geojson (26 regiones, código ISO PE-XXX)
```

### 4. Armonizar

```bash
python harmonize.py
```

Qué hace, en orden (todo en `harmonize.py`):

1. **Esquema común**: une columnas distintas de MODIS y VIIRS y pasa la hora a fecha de Perú (UTC−5).
2. **Filtros**: solo fuegos de vegetación (`type = 0`) y confianza nominal o alta en ambos sensores.
3. **Fuentes fijas**: quita celdas que alguna vez fueron volcán, fuente industrial u offshore.
4. **Duplicados**: el dato SP reemplaza al NRT cuando ambos existen.
5. **Grilla común**: cada detección cae en un hexágono H3 de ~5 km² y en su departamento.
6. **Un solo satélite VIIRS por día**: NOAA-20 desde abril de 2018, S-NPP antes (S-NPP dejó de
   entregar datos el 2 de noviembre de 2026), NOAA-21 de respaldo.
7. **Calibración por departamento y temporada** con los meses en que ambos sensores observaron
   (2012–2022):

   ```
   log(VIIRS + 1) = a + d · temporada_alta + b · log(MODIS + 1)
   ```

   - La *temporada alta* de cada departamento son los meses en que su promedio de hotspots supera
     su promedio anual. Así cada microclima (costa, sierra, selva) tiene su propia temporada.
   - Si un departamento tiene pocos meses con fuego (menos de 24), usa el ajuste nacional.
   - El **rango** (p10–p90) sale de los errores del ajuste: cerca de 8 de cada 10 meses reales caen dentro.
8. **Serie única**: antes del 20-01-2012 cada celda-día MODIS se multiplica por el factor de su
   departamento y mes; desde esa fecha se usa VIIRS tal cual.

Archivos creados en `data/processed/`:

- `fire_cell_day.parquet`: **el dataset armonizado** (una fila por hexágono y día).
- `calibration_factors.csv`: parámetros por departamento. Se **congelan**: la próxima vez se
  reutilizan para que la historia publicada no cambie. Para volver a ajustar: `python harmonize.py --recalibrate`.
- `validation.json`: errores medidos en años que el ajuste no vio (2020–2022), con y sin temporada.

### 5. Cargar en la base de datos

```bash
python load_postgis.py            # crea las tablas y carga todo
```

### 6. Levantar la API y la web

```bash
cd ..
uvicorn api.main:app --reload
```

- Web app: http://localhost:8000/
- Documentación interactiva de la API: http://localhost:8000/docs

### 7. Refrescar la web de presentación con datos reales

La web "Calendario del Fuego" (portada, reloj del fuego, costa/sierra/selva, explorador, EN/ES) es un solo
archivo HTML con los datos adentro. El zip trae una versión hecha con datos FIRMS 2003–2025. Para rehacerla
con lo que tú descargaste:

```bash
cd pipeline
python harmonize.py      # si aún no lo corriste
python build_web.py      # escribe web/story/index.html
```

- Ábrela con doble clic en `web/story/index.html`, o con la API en http://localhost:8000/historia
- Toma los años que tengas (desde 2003 hasta el último año descargado) y usa el último año completo
  para el mapa de calor.
- En internet: sube el `index.html` nuevo a GitHub y Render vuelve a publicar solo; queda en
  `https://<nombre>.onrender.com/historia`.

### 8. Probar

```bash
pytest -q                                                                  # sin base de datos
TEST_DATABASE_URL=postgresql://fire:fire@localhost:5432/fire pytest -q     # incluye PostGIS + API
```

### 9. Actualización diaria

```bash
cd pipeline
python download_firms.py api --start 2026-11-11 --mode nrt
python harmonize.py                          # usa la calibración congelada
python load_postgis.py --since 2026-11-11    # reemplaza solo esos días
```

En la nube esto lo hace `.github/workflows/daily.yml` cada mañana.

## Endpoints de la API

| Endpoint | Uso |
| --- | --- |
| `GET /calendar?region_id=PE-UCA` | Calendario año × mes: mediana, % de la mediana, estado (bajo/normal/alto) y rango. `region_id=PE` = todo Perú |
| `POST /calendar/aoi` | El mismo calendario para un polígono GeoJSON dibujado |
| `GET /anomaly?region_id=&year=&month=` | Un mes frente a lo normal |
| `GET /heatmap?start=&end=&res=6` | Hexágonos con la suma de hotspots (GeoJSON) |
| `GET /calendar/daily`, `/timeseries`, `/ranking` | Serie diaria, series y ranking de departamentos |
| `GET /export.csv`, `POST /export/aoi.csv` | CSV descargable |
| `GET /regions`, `/calibration`, `/health` | Departamentos, parámetros del ajuste, estado |
| `GET /historia` | Web de presentación "Calendario del Fuego" (generada con `build_web.py`) |

## Publicar en internet (gratis)

| Pieza | Herramienta | Alternativa |
| --- | --- | --- |
| Código | GitHub | GitLab |
| Base de datos PostGIS | Supabase (plan gratis, 500 MB) | Neon |
| API + web (una sola URL) | Render, "Web Service" con Docker | Railway, Fly.io |
| Actualización diaria | GitHub Actions | cron de Render |

1. **GitHub**: crea un repositorio y sube esta carpeta después del paso 4. El `.gitignore` deja fuera
   los datos pesados y tu `.env`, pero **sí sube** `data/processed/calibration_factors.csv` y
   `validation.json`: la actualización diaria necesita la calibración congelada.
2. **Supabase**: crea un proyecto → *Database → Extensions* → activa `postgis`.
   Copia la cadena de conexión (*Connect → Session pooler*) y agrégale `?sslmode=require`.
3. **Carga inicial desde tu computadora**: pon esa cadena en `DATABASE_URL` de tu `.env` y corre
   `python load_postgis.py --skip-detections` (sin el detalle por detección cabe en el plan gratis).
4. **Render**: *New → Web Service* → elige tu repositorio → *Runtime: Docker* → variable
   `DATABASE_URL` = la cadena de Supabase → *Create*. Tu app queda en `https://<nombre>.onrender.com/`.
   El plan gratis se "duerme" tras 15 minutos sin uso: ábrela un minuto antes de la demo.
5. **GitHub Actions**: en el repositorio, *Settings → Secrets and variables → Actions*, crea
   `FIRMS_MAP_KEY` y `DATABASE_URL`. El flujo `actualizar-hotspots` corre cada día a las 06:15 de Perú
   (también puedes lanzarlo a mano desde la pestaña *Actions*).

## Limitaciones conocidas

- Los valores antes de 2012 son **estimados** (la app los marca con borde punteado y un asterisco).
- En la costa (pocos fuegos por mes) el error es mayor: úsala para tendencias, no para un mes puntual.
- Un hotspot no distingue incendio forestal de quema agrícola.
