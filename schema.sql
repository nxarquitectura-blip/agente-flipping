-- schema.sql — Tablas que usa el Agente de House Flipping.
-- Ejecutar en Supabase → SQL Editor. Es idempotente (se puede correr más de una vez).
-- Si ya tenés tablas con otros nombres/columnas, ajustá TABLA_PROPIEDADES / TABLA_COMPARABLES
-- en config.py o agregá las columnas que falten.

create table if not exists public.propiedades (
  id                    bigint generated always as identity primary key,
  huella                text not null unique,
  url                   text not null unique,
  portal                text not null,
  urls_alternativas     jsonb not null default '[]'::jsonb,
  titulo                text,
  barrio                text not null,
  zona                  text,
  direccion             text,
  precio_usd            numeric(12,2) not null,
  m2_cubiertos          numeric(8,2) not null,
  m2_totales            numeric(8,2),
  ambientes             smallint,
  antiguedad            smallint,
  expensas_usd          numeric(10,2),
  dias_publicado        smallint,
  imagen_url            text,
  reciclado             boolean,
  comparables           jsonb not null default '[]'::jsonb,
  usd_m2_comparables    numeric(10,2),
  valor_venta_estimado  numeric(12,2),
  escenarios            jsonb not null default '{}'::jsonb,
  roi_minima            numeric(8,4),
  roi_media             numeric(8,4),
  roi_integral          numeric(8,4),
  ganancia_media        numeric(12,2),
  precio_max_compra     numeric(12,2),
  rebaja_requerida_pct  numeric(8,4),
  aprobada              boolean not null default false,
  motivo_descarte       text,
  entorno               jsonb,
  puntaje_entorno       smallint,
  estado                text not null default 'nueva'
                        check (estado in ('nueva','me_interesa','descartada','descartada_auto','sin_comparables')),
  creado_en             timestamptz not null default now(),
  actualizado_en        timestamptz not null default now()
);

-- Ficha completa scrapeada de la página individual (sólo PH aprobados).
-- Opcional: si no existe, el motor guarda el resto igual y avisa en el log.
alter table public.propiedades add column if not exists ficha jsonb;

create index if not exists propiedades_aprobada_estado_idx on public.propiedades (aprobada, estado);
create index if not exists propiedades_creado_idx on public.propiedades (creado_en desc);

create table if not exists public.comparables (
  id            bigint generated always as identity primary key,
  url           text not null unique,
  portal        text,
  barrio        text not null,
  precio_usd    numeric(12,2) not null,
  m2_cubiertos  numeric(8,2) not null,
  usd_m2        numeric(10,2) not null,
  ambientes     smallint,
  titulo        text,
  capturado_en  timestamptz not null default now()
);

create index if not exists comparables_barrio_fecha_idx on public.comparables (barrio, capturado_en desc);

-- --------------------------------------------------------------------------
-- Seguridad (RLS). El backend usa la clave secreta (service_role), que saltea RLS.
-- El tablero web usa la clave pública: sólo puede LEER oportunidades aprobadas
-- y cambiar la columna `estado` entre nueva / me_interesa / descartada.
-- --------------------------------------------------------------------------
alter table public.propiedades enable row level security;
alter table public.comparables enable row level security;

drop policy if exists "tablero_lee_aprobadas" on public.propiedades;
create policy "tablero_lee_aprobadas" on public.propiedades
  for select to anon
  using (aprobada);

drop policy if exists "tablero_cambia_estado" on public.propiedades;
create policy "tablero_cambia_estado" on public.propiedades
  for update to anon
  using (aprobada)
  with check (aprobada and estado in ('nueva','me_interesa','descartada'));

revoke all on public.propiedades from anon;
grant select on public.propiedades to anon;
grant update (estado, actualizado_en) on public.propiedades to anon;

revoke all on public.comparables from anon;
