-- Esquema PostgreSQL + PostGIS para hotspots armonizados MODIS–VIIRS (Perú)
CREATE EXTENSION IF NOT EXISTS postgis;

-- Regiones (departamentos). level permite agregar provincias/distritos después.
CREATE TABLE IF NOT EXISTS region (
    region_id  text PRIMARY KEY,               -- p. ej. 'PE-UCA'
    name       text NOT NULL,
    level      smallint NOT NULL DEFAULT 1,     -- 1 = departamento
    geom       geometry(MultiPolygon, 4326) NOT NULL,
    area_km2   double precision
);
CREATE INDEX IF NOT EXISTS region_geom_gix ON region USING gist (geom);

-- Capa de detalle: cada detección de ambos sensores con un esquema común (trazabilidad).
CREATE TABLE IF NOT EXISTS hotspot_detection (
    id               bigserial PRIMARY KEY,
    family           text NOT NULL CHECK (family IN ('MODIS', 'VIIRS')),
    satellite        text NOT NULL,             -- TERRA, AQUA, SNPP, NOAA20, NOAA21
    processing       text NOT NULL,             -- SP | NRT
    acq_ts           timestamptz NOT NULL,      -- UTC
    acq_date_local   date NOT NULL,             -- fecha en Perú (UTC-5)
    latitude         double precision NOT NULL,
    longitude        double precision NOT NULL,
    geom             geometry(Point, 4326)
                     GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)) STORED,
    confidence_raw   text,
    confidence_class text,                      -- low | nominal | high
    frp              real,
    brightness       real,
    bright2          real,
    scan             real,
    track            real,
    daynight         char(1),
    fire_type        smallint,                  -- 0 vegetación (solo SP)
    version          text,
    cell_h3          text NOT NULL,
    region_id        text REFERENCES region,
    UNIQUE (satellite, acq_ts, latitude, longitude)
);
CREATE INDEX IF NOT EXISTS det_date_idx ON hotspot_detection (region_id, acq_date_local);
CREATE INDEX IF NOT EXISTS det_geom_gix ON hotspot_detection USING gist (geom);

-- EL dataset armonizado: una fila por celda H3 (res 7, ~5 km²) y día local.
-- hs_h = hotspots en escala VIIRS (observados desde 2012-01-20, MODIS ajustado antes).
CREATE TABLE IF NOT EXISTS fire_cell_day (
    cell_h3    text NOT NULL,
    date       date NOT NULL,
    region_id  text NOT NULL REFERENCES region,
    lat        double precision NOT NULL,
    lon        double precision NOT NULL,
    geom       geometry(Point, 4326)
               GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
    hs_h       real NOT NULL,
    hs_lo      real NOT NULL,                   -- rango p10 (= hs_h si VIIRS)
    hs_hi      real NOT NULL,                   -- rango p90 (= hs_h si VIIRS)
    frp_h      real,
    source     text NOT NULL,                   -- VIIRS | MODIS_ADJ
    modis_n    integer NOT NULL DEFAULT 0,
    viirs_n    integer NOT NULL DEFAULT 0,
    k_hs       real,                            -- factor aplicado (NULL si VIIRS)
    calib_version text,
    PRIMARY KEY (cell_h3, date)
);
CREATE INDEX IF NOT EXISTS fcd_region_date_idx ON fire_cell_day (region_id, date);
CREATE INDEX IF NOT EXISTS fcd_date_brin ON fire_cell_day USING brin (date);
CREATE INDEX IF NOT EXISTS fcd_geom_gix ON fire_cell_day USING gist (geom);

-- Parámetros de calibración (transparencia y reproducibilidad)
CREATE TABLE IF NOT EXISTS calibration_factor (
    region_id  text NOT NULL,
    metric     text NOT NULL,                   -- hs | frp
    method     text NOT NULL,
    a          double precision,               -- log(V+1) = a + d*alta + b*log(M+1)
    d          double precision,               -- ajuste extra en temporada alta
    b          double precision,
    smear      double precision,
    res_p10    double precision,               -- rango de incertidumbre (log)
    res_p90    double precision,
    high_months text,                          -- meses de temporada alta, p. ej. '7,8,9,10'
    n_months   integer,
    r2_test    double precision,
    mape_test  double precision,
    version    text NOT NULL,
    PRIMARY KEY (version, region_id, metric)
);

-- Agregados para el calendario (se refrescan tras cada carga)
DROP MATERIALIZED VIEW IF EXISTS region_month CASCADE;
DROP MATERIALIZED VIEW IF EXISTS region_day CASCADE;

CREATE MATERIALIZED VIEW region_day AS
SELECT region_id, date,
       sum(hs_h)::real  AS hs,
       sum(hs_lo)::real AS hs_lo,
       sum(hs_hi)::real AS hs_hi,
       sum(frp_h)::real AS frp,
       count(*)         AS active_cells,
       min(source)      AS source
FROM fire_cell_day
GROUP BY region_id, date;
CREATE UNIQUE INDEX ON region_day (region_id, date);

-- Serie mensual con ceros explícitos (necesario para medianas correctas)
CREATE MATERIALIZED VIEW region_month AS
WITH bounds AS (
    SELECT date_trunc('month', min(date))::date AS m0, date_trunc('month', max(date))::date AS m1
    FROM fire_cell_day
), months AS (
    SELECT generate_series(m0, m1, interval '1 month')::date AS month FROM bounds
), grid AS (
    SELECT r.region_id, m.month FROM region r CROSS JOIN months m
), agg AS (
    SELECT region_id, date_trunc('month', date)::date AS month,
           sum(hs) AS hs, sum(hs_lo) AS hs_lo, sum(hs_hi) AS hs_hi,
           sum(frp) AS frp, sum(active_cells) AS active_cells
    FROM region_day GROUP BY 1, 2
)
SELECT g.region_id, g.month,
       extract(year FROM g.month)::int  AS year,
       extract(month FROM g.month)::int AS mon,
       coalesce(a.hs, 0)::real           AS hs,
       coalesce(a.hs_lo, 0)::real        AS hs_lo,
       coalesce(a.hs_hi, 0)::real        AS hs_hi,
       coalesce(a.frp, 0)::real          AS frp,
       coalesce(a.active_cells, 0)::int  AS active_cells
FROM grid g LEFT JOIN agg a USING (region_id, month);
CREATE UNIQUE INDEX ON region_month (region_id, month);
