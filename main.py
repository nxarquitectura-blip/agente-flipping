"""
main.py — Orquestador del Agente de House Flipping (CABA + GBA Norte).

Flujo diario:
    1. Recolecta PH usados nuevos de los 3 portales (filtros + anti-duplicados).
    2. Cruza cada aviso con 4-5 comparables PH reciclados del mismo barrio.
    3. Corre el motor financiero (3 niveles de obra × valor de reventa
       pesimista / base / optimista + descarte automático).
    4. Analiza el entorno de las oportunidades aprobadas con Gemini.
    4b. Scrapea la ficha individual SÓLO de los PH aprobados.
    5. Guarda todo en Supabase.
    6. Genera un informe web por PH aprobado (informes/<huella>.html) y el
       Tablero Web (index.html) con botones sincronizados a Supabase.
    7. Envía el resumen diario por Telegram.

Uso:
    python main.py                  # corrida completa
    python main.py --solo-tablero   # sólo regenera index.html desde Supabase
    python main.py --dry-run        # corre todo pero no escribe en Supabase ni envía Telegram
    python main.py --sin-telegram   # no envía el resumen
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests

import config as cfg
import generador_informes
from analista_ia import AnalistaEntorno
from motor_financiero import MotorFinanciero, ResultadoAnalisis
from recolector_portales import (
    Aviso,
    ClienteApify,
    Comparable,
    ExtractorAvisosIA,
    RecolectorPortales,
)
from supabase_db import ErrorSupabase, SupabaseREST

log = logging.getLogger("main")
ART = timezone(timedelta(hours=-3))  # Argentina no usa horario de verano


# =========================================================================== #
# Resumen de la corrida
# =========================================================================== #
@dataclass
class ResumenCorrida:
    inicio: datetime = field(default_factory=lambda: datetime.now(ART))
    crudos: int = 0
    pasan_filtros: int = 0
    nuevos: int = 0
    excluidos_no_ph: int = 0
    aprobadas: list[dict] = field(default_factory=list)
    descartadas_auto: int = 0
    sin_comparables: int = 0
    errores: list[str] = field(default_factory=list)


# =========================================================================== #
# Construcción de filas para Supabase
# =========================================================================== #
def construir_fila(
    aviso: Aviso,
    comparables: list[Comparable],
    analisis: Optional[ResultadoAnalisis],
    motivo: str,
) -> dict:
    escenarios: dict = {}
    if analisis:
        d = analisis.to_dict()
        escenarios = dict(d["escenarios"])
        # Se guarda dentro del jsonb `escenarios` para no tocar el esquema de Supabase.
        escenarios["Reventa"] = {
            k: {**v, "usd_m2": d["usd_m2_reventa"][k]} for k, v in d["sensibilidad"].items()
        }
    base = analisis.base if analisis else None

    def roi(nombre: str) -> Optional[float]:
        return escenarios.get(nombre, {}).get("roi")

    if analisis is None:
        estado = "sin_comparables"
    elif analisis.aprobada:
        estado = "nueva"
    else:
        estado = "descartada_auto"

    return {
        "huella": aviso.huella,
        "url": aviso.url,
        "portal": aviso.portal,
        "urls_alternativas": aviso.urls_alternativas,
        "titulo": aviso.titulo,
        "barrio": aviso.barrio,
        "zona": aviso.zona,
        "direccion": aviso.direccion,
        "precio_usd": aviso.precio_usd,
        "m2_cubiertos": aviso.m2_cubiertos,
        "m2_totales": aviso.m2_totales,
        "ambientes": aviso.ambientes,
        "antiguedad": aviso.antiguedad,
        "expensas_usd": aviso.expensas_usd,
        "dias_publicado": aviso.dias_publicado,
        "imagen_url": aviso.imagen_url,
        "reciclado": aviso.reciclado,
        "comparables": [asdict(c) for c in comparables],
        "usd_m2_comparables": analisis.usd_m2_reciclado if analisis else None,
        "valor_venta_estimado": analisis.valor_reciclado_100 if analisis else None,
        "escenarios": escenarios,
        # Columnas históricas: roi_minima = obra Simple, roi_media = obra Intermedia (base).
        "roi_minima": roi("Simple"),
        "roi_media": roi("Intermedia"),
        "roi_integral": roi("Integral"),
        "ganancia_media": base.ganancia_neta if base else None,
        "precio_max_compra": base.precio_max_compra if base else None,
        "rebaja_requerida_pct": base.rebaja_requerida_pct if base else None,
        "aprobada": bool(analisis and analisis.aprobada),
        "motivo_descarte": motivo,
        "entorno": None,
        "puntaje_entorno": None,
        "ficha": None,
        "estado": estado,
    }


def columna_existe(db: SupabaseREST, tabla: str, columna: str) -> bool:
    try:
        db.select(tabla, columnas=columna, limite=1)
        return True
    except ErrorSupabase:
        return False


def url_informe(huella: str) -> Optional[str]:
    if not cfg.TABLERO_URL:
        return None
    return urljoin(cfg.TABLERO_URL.rstrip("/") + "/", generador_informes.ruta_informe(huella))


# =========================================================================== #
# Pipeline
# =========================================================================== #
def procesar_avisos(db: SupabaseREST, resumen: ResumenCorrida, dry_run: bool) -> None:
    cfg.requerir("APIFY_TOKEN", "GEMINI_API_KEY")
    recolector = RecolectorPortales(
        db=db,
        apify=ClienteApify(cfg.APIFY_TOKEN),
        extractor=ExtractorAvisosIA(cfg.GEMINI_API_KEY),
    )

    avisos = recolector.recolectar()
    resumen.crudos = recolector.stats.crudos
    resumen.pasan_filtros = recolector.stats.pasan_filtros
    resumen.nuevos = recolector.stats.nuevos

    if avisos:
        recolector.preparar_comparables({a.barrio for a in avisos})

    motor = MotorFinanciero()
    filas: list[dict] = []
    for aviso in avisos:
        comps = recolector.comparables_para(aviso)
        if len(comps) < cfg.COMPARABLES_MIN:
            resumen.sin_comparables += 1
            filas.append(construir_fila(
                aviso, comps, None,
                f"Sólo {len(comps)} comparables PH similares en {aviso.barrio} (mínimo {cfg.COMPARABLES_MIN}).",
            ))
            continue
        analisis = motor.analizar(aviso.precio_usd, aviso.m2_cubiertos, [c.usd_m2 for c in comps])
        filas.append(construir_fila(aviso, comps, analisis, analisis.motivo))
        if not analisis.aprobada:
            resumen.descartadas_auto += 1

    aprobadas = [f for f in filas if f["aprobada"]]
    if aprobadas:
        # Ficha completa: se scrapea la URL individual sólo de las aprobadas.
        fichas = recolector.detallar_aprobadas(aprobadas)
        for fila in aprobadas:
            fila["ficha"] = fichas.get(fila["url"])
    resumen.excluidos_no_ph = recolector.stats.excluidos_no_ph
    resumen.errores.extend(recolector.stats.errores)

    if aprobadas:
        log.info("Analizando entorno de %d oportunidades con Gemini…", len(aprobadas))
        analista = AnalistaEntorno()
        for fila, entorno in zip(aprobadas, analista.analizar_lote(aprobadas)):
            fila["entorno"] = entorno
            fila["puntaje_entorno"] = entorno.get("puntaje_entorno")

    resumen.aprobadas = sorted(aprobadas, key=lambda f: f["roi_media"] or 0, reverse=True)

    # Informes individuales con los datos completos en memoria (incluye la ficha).
    generador_informes.generar_informes(aprobadas)

    if dry_run:
        salida = cfg.BASE_DIR / "dry_run_resultados.json"
        salida.write_text(json.dumps(filas, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        log.info("Dry-run: %d filas guardadas en %s (no se escribió en Supabase)", len(filas), salida)
        return

    if filas and not columna_existe(db, cfg.TABLA_PROPIEDADES, "ficha"):
        log.warning("La tabla %s no tiene la columna `ficha` (ver schema.sql): se guarda sin la ficha.", cfg.TABLA_PROPIEDADES)
        for fila in filas:
            fila.pop("ficha", None)
    insertadas = db.insert(cfg.TABLA_PROPIEDADES, filas, on_conflict="huella")
    log.info("Supabase: %d propiedades nuevas guardadas", len(insertadas))


# =========================================================================== #
# Tablero web
# =========================================================================== #
def generar_tablero(propiedades: list[dict], ruta: Path = cfg.TABLERO_HTML) -> Path:
    datos = json.dumps(propiedades, ensure_ascii=False, default=str).replace("</", "<\\/")
    informes = json.dumps(generador_informes.huellas_con_informe())
    generado = datetime.now(ART).strftime("%d/%m/%Y %H:%M")
    contenido = (
        PLANTILLA_TABLERO
        .replace("__SUPABASE_URL__", cfg.SUPABASE_URL)
        .replace("__SUPABASE_ANON_KEY__", cfg.SUPABASE_ANON_KEY)
        .replace("__TABLA__", cfg.TABLA_PROPIEDADES)
        .replace("__GENERADO__", generado)
        .replace("__INFORMES__", informes)
        .replace("__DATA__", datos)  # siempre al final
    )
    ruta.write_text(contenido, encoding="utf-8")
    log.info("Tablero generado: %s (%d oportunidades)", ruta, len(propiedades))
    return ruta


# =========================================================================== #
# Telegram
# =========================================================================== #
def usd(n: Optional[float]) -> str:
    return "—" if n is None else "USD " + f"{n:,.0f}".replace(",", ".")


def pct(n: Optional[float]) -> str:
    return "—" if n is None else f"{n * 100:.1f}%".replace(".", ",")


TELEGRAM_POR_MENSAJE = 7         # propiedades por mensaje (tandas de 6 a 8)
TELEGRAM_LIMITE_CARACTERES = 3800  # Telegram corta en 4096: siempre por debajo


def bloque_oportunidad(i: int, f: dict) -> str:
    """Bloque HTML de una propiedad aprobada para Telegram."""
    e = html.escape
    entorno = f.get("entorno") or {}
    base = (f.get("escenarios") or {}).get("Intermedia") or {}
    links = []
    informe = url_informe(f.get("huella") or "")
    if informe:
        links.append(f"<a href=\"{e(informe, quote=True)}\">Ver informe</a>")
    links.append(f"<a href=\"{e(f['url'], quote=True)}\">Ver aviso</a>")
    return (
        f"{i}. <b>PH {e(str(f.get('barrio') or ''))}</b> · {f.get('ambientes') or '?'} amb · "
        f"{float(f.get('m2_cubiertos') or 0):.0f} m² · {usd(f.get('precio_usd'))}\n"
        f"   ROI al publicado {pct(f.get('roi_media'))} · Oferta máx. 20 % {usd(base.get('oferta_max_20'))}\n"
        f"   Entorno {entorno.get('puntaje_entorno') or '?'}/10 · " + " · ".join(links)
    )


def armar_mensajes(resumen: ResumenCorrida, solo_tablero: bool, cantidad_tablero: int) -> list[str]:
    """
    Mensaje 1: resumen de la corrida. Mensajes siguientes: TODAS las aprobadas,
    ordenadas por ROI descendente, en tandas de TELEGRAM_POR_MENSAJE y siempre
    por debajo de TELEGRAM_LIMITE_CARACTERES.
    """
    e = html.escape
    lineas = [f"🏠 <b>Agente House Flipping PH</b> — {resumen.inicio:%d/%m/%Y}"]
    if solo_tablero:
        lineas.append(f"Tablero actualizado: {cantidad_tablero} oportunidades activas.")
    else:
        lineas += [
            f"📡 PH relevados: {resumen.crudos} · Pasan filtros: {resumen.pasan_filtros} · Nuevos: {resumen.nuevos}",
            f"🚫 Excluidos (no PH / a estrenar / pozo): {resumen.excluidos_no_ph}",
            f"✅ <b>Oportunidades aprobadas hoy: {len(resumen.aprobadas)}</b>",
            f"❌ Descarte automático: {resumen.descartadas_auto} · ⚪ Sin comparables: {resumen.sin_comparables}",
        ]
        if not resumen.aprobadas:
            lineas.append("\nHoy no aparecieron oportunidades que cumplan la regla del 15 %.")
    if resumen.errores:
        lineas.append("\n⚠️ <b>Incidencias:</b>")
        lineas += [f"• {e(err[:180])}" for err in resumen.errores[:6]]
    if cfg.TABLERO_URL:
        lineas.append(f"\n📊 <a href=\"{e(cfg.TABLERO_URL, quote=True)}\">Abrir Tablero</a>")
    mensajes = ["\n".join(lineas)[:TELEGRAM_LIMITE_CARACTERES]]

    if solo_tablero or not resumen.aprobadas:
        return mensajes

    ordenadas = sorted(resumen.aprobadas, key=lambda f: f.get("roi_media") or 0, reverse=True)
    bloques = [bloque_oportunidad(i, f) for i, f in enumerate(ordenadas, 1)]
    total = len(bloques)

    def cabecera(desde: int, hasta: int) -> str:
        return f"<b>🔥 Oportunidades {desde}–{hasta} de {total}</b> (ROI obra Intermedia)\n"

    tandas: list[tuple[int, list[str]]] = []  # (índice inicial, bloques)
    actual: list[str] = []
    inicio = 1
    for n, bloque in enumerate(bloques, 1):
        candidato = actual + [bloque]
        largo = len(cabecera(inicio, n)) + len("\n\n".join(candidato))
        if actual and (len(actual) >= TELEGRAM_POR_MENSAJE or largo > TELEGRAM_LIMITE_CARACTERES):
            tandas.append((inicio, actual))
            actual, inicio = [bloque], n
        else:
            actual = candidato
    if actual:
        tandas.append((inicio, actual))

    for desde, grupo in tandas:
        hasta = desde + len(grupo) - 1
        mensajes.append((cabecera(desde, hasta) + "\n" + "\n\n".join(grupo))[:TELEGRAM_LIMITE_CARACTERES])
    return mensajes


def enviar_telegram(mensajes: list[str]) -> None:
    if not (cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID):
        log.warning("Telegram no configurado: no se envía el resumen.")
        return
    if not cfg.TABLERO_URL:
        log.warning("TABLERO_URL no configurado: los mensajes salen sin el link 'Ver informe'.")
    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage"
    for i, texto in enumerate(mensajes):
        if i:
            time.sleep(1.1)  # Telegram limita ~1 mensaje/seg por chat
        r = requests.post(
            url,
            data={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20,
        )
        if r.status_code == 429:  # demasiados mensajes: esperar lo que pide Telegram y reintentar
            espera = (r.json().get("parameters") or {}).get("retry_after", 5)
            time.sleep(float(espera) + 1)
            r = requests.post(
                url,
                data={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=20,
            )
        if not r.ok:
            raise RuntimeError(f"Telegram respondió {r.status_code}: {r.text[:300]}")
    log.info("Resumen enviado a Telegram (%d mensaje/s)", len(mensajes))


# =========================================================================== #
# Main
# =========================================================================== #
def configurar_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-11s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    for ruidoso in ("httpx", "urllib3", "apify_client", "google_genai"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agente de House Flipping CABA + GBA Norte")
    parser.add_argument("--solo-tablero", action="store_true", help="Sólo regenera index.html desde Supabase")
    parser.add_argument("--dry-run", action="store_true", help="No escribe en Supabase ni envía Telegram")
    parser.add_argument("--sin-telegram", action="store_true", help="No envía el resumen a Telegram")
    args = parser.parse_args()

    configurar_logging()
    cfg.requerir("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY")
    db = SupabaseREST(cfg.SUPABASE_URL, cfg.SUPABASE_SERVICE_KEY)
    resumen = ResumenCorrida()
    codigo_salida = 0

    if not args.solo_tablero:
        try:
            procesar_avisos(db, resumen, args.dry_run)
        except Exception as exc:  # noqa: BLE001 — igual se regenera el tablero y se avisa
            log.exception("Fallo en el procesamiento de avisos")
            resumen.errores.append(f"Fallo general: {exc}")
            codigo_salida = 1

    oportunidades = db.select(
        cfg.TABLA_PROPIEDADES,
        filtros={"aprobada": "is.true"},
        orden="creado_en.desc",
        limite=500,
    )
    # Completa informes que falten (p. ej. corrida --solo-tablero); no pisa los existentes.
    generador_informes.generar_informes(oportunidades, solo_faltantes=True)
    generar_tablero(oportunidades)

    if not (args.sin_telegram or args.dry_run):
        activas = sum(1 for o in oportunidades if o.get("estado") in ("nueva", "me_interesa"))
        try:
            enviar_telegram(armar_mensajes(resumen, args.solo_tablero, activas))
        except Exception:  # noqa: BLE001
            log.exception("No se pudo enviar el resumen a Telegram")
            codigo_salida = 1

    log.info("Fin de la corrida.")
    return codigo_salida


# =========================================================================== #
# Plantilla del tablero (HTML + CSS + JS, sin dependencias externas)
# =========================================================================== #
PLANTILLA_TABLERO = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Tablero House Flipping</title>
<style>
:root{
  --bg:#f6f7f9;--panel:#ffffff;--text:#16181d;--muted:#5f6673;--border:#e2e5ea;
  --accent:#1f6feb;--ok:#1a7f37;--ok-bg:#e8f5ec;--bad:#c62828;--bad-bg:#fdecec;
  --warn:#9a6700;--warn-bg:#fff6dd;--chip:#eef1f5;--shadow:0 1px 2px rgba(0,0,0,.06),0 4px 14px rgba(0,0,0,.05);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0f1115;--panel:#171a21;--text:#e7e9ee;--muted:#9aa3b2;--border:#2a2f3a;
    --accent:#4c8dff;--ok:#3fb950;--ok-bg:#12291a;--bad:#ff6b6b;--bad-bg:#2d1517;
    --warn:#e3b341;--warn-bg:#2b230f;--chip:#222733;--shadow:none;
  }
}
:root[data-theme="dark"]{
  --bg:#0f1115;--panel:#171a21;--text:#e7e9ee;--muted:#9aa3b2;--border:#2a2f3a;
  --accent:#4c8dff;--ok:#3fb950;--ok-bg:#12291a;--bad:#ff6b6b;--bad-bg:#2d1517;
  --warn:#e3b341;--warn-bg:#2b230f;--chip:#222733;--shadow:none;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--border);padding:12px 16px}
.top{max-width:1200px;margin:0 auto;display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;justify-content:space-between}
h1{font-size:18px;margin:0}
.meta{color:var(--muted);font-size:13px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--warn)}
.dot.on{background:var(--ok)}.dot.off{background:var(--bad)}
.controls{max-width:1200px;margin:12px auto 0;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.tab{border:1px solid var(--border);background:var(--panel);color:var(--text);padding:7px 12px;border-radius:999px;cursor:pointer;font-size:14px}
.tab.active{background:var(--accent);border-color:var(--accent);color:#fff}
select,input[type=search]{border:1px solid var(--border);background:var(--panel);color:var(--text);padding:7px 10px;border-radius:8px;font-size:14px}
input[type=search]{flex:1;min-width:160px}
main{max-width:1200px;margin:16px auto;padding:0 16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);overflow:hidden;display:flex;flex-direction:column}
.foto{height:150px;background:var(--chip) center/cover no-repeat}
.body{padding:12px 14px;display:flex;flex-direction:column;gap:10px;flex:1}
.row{display:flex;justify-content:space-between;gap:8px;align-items:baseline;flex-wrap:wrap}
.barrio{font-weight:700;font-size:16px}
.precio{font-weight:700;font-size:17px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--chip);border-radius:6px;padding:2px 8px;font-size:12.5px;color:var(--muted)}
.chip.ok{background:var(--ok-bg);color:var(--ok)}.chip.warn{background:var(--warn-bg);color:var(--warn)}.chip.bad{background:var(--bad-bg);color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:5px 4px;border-bottom:1px solid var(--border);text-align:right}
th:first-child,td:first-child{text-align:left}
tr.base td{font-weight:700;background:var(--chip)}
.pos{color:var(--ok)}.neg{color:var(--bad)}
.oferta{border-radius:10px;padding:8px 10px;font-size:13.5px;background:var(--ok-bg);color:var(--ok)}
.oferta.warn{background:var(--warn-bg);color:var(--warn)}
details{font-size:13.5px}
summary{cursor:pointer;color:var(--accent);font-weight:600}
details ul{margin:6px 0 0;padding-left:18px}
a{color:var(--accent)}
.acciones{display:flex;gap:8px;padding:12px 14px;border-top:1px solid var(--border)}
.btn{flex:1;border:0;border-radius:10px;padding:10px;font-size:14.5px;font-weight:600;cursor:pointer}
.btn.si{background:var(--ok);color:#fff}.btn.no{background:var(--bad-bg);color:var(--bad)}.btn.neutro{background:var(--chip);color:var(--text)}
.btn:disabled{opacity:.5;cursor:wait}
a.btn.informe{display:block;text-align:center;text-decoration:none;flex:none}
.vacio{grid-column:1/-1;text-align:center;color:var(--muted);padding:48px 0}
#toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:10px 16px;border-radius:10px;font-size:14px;opacity:0;transition:opacity .2s;pointer-events:none;max-width:calc(100% - 32px)}
#toast.show{opacity:1}
@media (max-width:420px){main{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div class="top">
    <div>
      <h1>🏠 House Flipping PH · CABA + GBA Norte</h1>
      <div class="meta"><span id="dot" class="dot"></span><span id="conexion">Conectando…</span> · Generado __GENERADO__</div>
    </div>
    <button class="tab" id="tema" title="Cambiar tema">🌓</button>
  </div>
  <div class="controls">
    <button class="tab active" data-estado="nueva">🆕 Nuevas <span id="c-nueva"></span></button>
    <button class="tab" data-estado="me_interesa">✅ Me interesa <span id="c-me_interesa"></span></button>
    <button class="tab" data-estado="descartada">❌ Descartadas <span id="c-descartada"></span></button>
    <select id="orden" aria-label="Ordenar">
      <option value="roi">Mayor ROI (Intermedia)</option>
      <option value="rebaja">Menor rebaja requerida</option>
      <option value="precio">Menor precio</option>
      <option value="entorno">Mejor entorno</option>
      <option value="reciente">Más recientes</option>
    </select>
    <input type="search" id="buscar" placeholder="Buscar barrio, dirección, portal…">
  </div>
</header>
<main id="lista"></main>
<div id="toast" role="status"></div>

<script>
const CONFIG = { url: "__SUPABASE_URL__", key: "__SUPABASE_ANON_KEY__", tabla: "__TABLA__" };
const INFORMES = new Set(__INFORMES__);
const SNAPSHOT = __DATA__;
let props = SNAPSHOT;
let filtroEstado = "nueva";

const fmtUSD = n => n == null ? "—" : "USD " + new Intl.NumberFormat("es-AR", {maximumFractionDigits: 0}).format(n);
const fmtPct = n => n == null ? "—" : (n * 100).toFixed(1).replace(".", ",") + "%";
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const urlSegura = u => /^https?:\/\//i.test(u || "") ? esc(u) : "#";

function headers(extra = {}) {
  const h = { apikey: CONFIG.key, "Content-Type": "application/json", ...extra };
  if (!CONFIG.key.startsWith("sb_")) h.Authorization = "Bearer " + CONFIG.key;
  return h;
}

function estadoConexion(ok, texto) {
  document.getElementById("dot").className = "dot " + (ok ? "on" : "off");
  document.getElementById("conexion").textContent = texto;
}

async function cargar() {
  try {
    const r = await fetch(`${CONFIG.url}/rest/v1/${CONFIG.tabla}?select=*&aprobada=is.true&order=creado_en.desc&limit=500`, { headers: headers() });
    if (!r.ok) throw new Error("HTTP " + r.status);
    props = await r.json();
    estadoConexion(true, "Sincronizado con Supabase");
  } catch (e) {
    estadoConexion(false, "Sin conexión: mostrando la copia del " + "__GENERADO__");
  }
  render();
}

async function cambiarEstado(id, estado, boton) {
  const p = props.find(x => x.id === id);
  if (!p) return;
  const previo = p.estado;
  boton.closest(".acciones").querySelectorAll("button").forEach(b => b.disabled = true);
  try {
    const r = await fetch(`${CONFIG.url}/rest/v1/${CONFIG.tabla}?id=eq.${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: headers({ Prefer: "return=representation" }),
      body: JSON.stringify({ estado, actualizado_en: new Date().toISOString() })
    });
    if (!r.ok) throw new Error(await r.text());
    const filas = await r.json();
    if (!filas.length) throw new Error("Supabase no permitió el cambio (revisá las políticas RLS).");
    p.estado = estado;
    toast(estado === "me_interesa" ? "✅ Marcada como 'Me interesa'" : estado === "descartada" ? "❌ Descartada" : "↩ Restaurada");
  } catch (e) {
    p.estado = previo;
    toast("⚠️ No se pudo guardar: " + e.message);
  }
  render();
}

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2800);
}

function ordenar(lista) {
  const modo = document.getElementById("orden").value;
  const f = {
    roi: (a, b) => (b.roi_media ?? -9) - (a.roi_media ?? -9),
    rebaja: (a, b) => (a.rebaja_requerida_pct ?? 9) - (b.rebaja_requerida_pct ?? 9),
    precio: (a, b) => a.precio_usd - b.precio_usd,
    entorno: (a, b) => (b.puntaje_entorno ?? 0) - (a.puntaje_entorno ?? 0),
    reciente: (a, b) => String(b.creado_en).localeCompare(String(a.creado_en)),
  }[modo];
  return lista.sort(f);
}

// "Mínima"/"Media" son los nombres de las filas guardadas antes del cambio a Simple/Intermedia.
const BASES = ["Intermedia", "Media"];
function filaEscenario(etiqueta, e, esBase) {
  return `<tr class="${esBase ? "base" : ""}">
      <td>${etiqueta}${esBase ? " ★" : ""}</td>
      <td>${fmtUSD(e.costo_reforma)}</td>
      <td>${fmtUSD(e.inversion_total)}</td>
      <td>${fmtUSD(e.precio_venta)}</td>
      <td class="${e.ganancia_neta >= 0 ? "pos" : "neg"}">${fmtUSD(e.ganancia_neta)}</td>
      <td class="${e.roi >= 0.15 ? "pos" : e.roi < 0 ? "neg" : ""}">${fmtPct(e.roi)}</td>
    </tr>`;
}
function tablaEscenarios(p) {
  const orden = ["Simple", "Mínima", "Intermedia", "Media", "Integral"];
  const esc_ = p.escenarios || {};
  const filas = orden.filter(k => esc_[k]).map(k => filaEscenario(k, esc_[k], BASES.includes(k))).join("");
  const cab = `<thead><tr><th>__COL__</th><th>Obra</th><th>Inversión</th><th>Venta</th><th>Ganancia</th><th>ROI</th></tr></thead>`;
  let html_ = `<table>${cab.replace("__COL__", "Obra")}<tbody>${filas}</tbody></table>`;
  const rv = esc_.Reventa;
  if (rv) {
    const f2 = ["Pesimista", "Base", "Optimista"].filter(k => rv[k])
      .map(k => filaEscenario(`${k} <span class="meta">${fmtUSD(rv[k].usd_m2)}/m²</span>`, rv[k], k === "Base")).join("");
    html_ += `<div class="meta">Valor de reventa con obra Intermedia (p25 / mediana / p75 × m² × 0,95)</div>
      <table>${cab.replace("__COL__", "Reventa")}<tbody>${f2}</tbody></table>`;
  }
  return html_;
}

function bloqueEntorno(p) {
  const e = p.entorno;
  if (!e) return "";
  const lista = (arr, f) => (arr || []).map(f).filter(Boolean);
  const transporte = [
    ...lista(e.subtes, s => `🚇 Subte ${esc(s.linea)} · ${esc(s.estacion)} (${esc(s.cuadras ?? "?")} cdas)`),
    ...lista(e.trenes, s => `🚆 Tren ${esc(s.linea)} · ${esc(s.estacion)} (${esc(s.cuadras ?? "?")} cdas)`),
    ...lista(e.metrobus, s => `🚌 Metrobús ${esc(s.corredor)} · ${esc(s.parada)} (${esc(s.cuadras ?? "?")} cdas)`),
    ...lista(e.avenidas, s => `🛣️ ${esc(s.nombre)} (${esc(s.cuadras ?? "?")} cdas)`),
  ];
  const impacto = { positivo: "ok", neutro: "", negativo: "bad" }[e.impacto_reventa] ?? "";
  return `<details>
    <summary>📍 Entorno ${e.puntaje_entorno ?? "?"}/10 <span class="chip ${impacto}">${esc(e.impacto_reventa)}</span></summary>
    <p>${esc(e.resumen)}</p>
    ${transporte.length ? `<ul>${transporte.map(t => `<li>${t}</li>`).join("")}</ul>` : ""}
    ${(e.puntos_fuertes || []).length ? `<p><b>A favor:</b> ${e.puntos_fuertes.map(esc).join(" · ")}</p>` : ""}
    ${(e.riesgos || []).length ? `<p><b>Riesgos:</b> ${e.riesgos.map(esc).join(" · ")}</p>` : ""}
    ${e.comentario_inversion ? `<p><i>${esc(e.comentario_inversion)}</i></p>` : ""}
    ${e.precision_ubicacion === "baja" ? `<p class="chip warn">Ubicación aproximada</p>` : ""}
  </details>`;
}

function bloqueComparables(p) {
  const c = p.comparables || [];
  if (!c.length) return "";
  return `<details>
    <summary>🏷️ ${c.length} comparables PH · mediana ${fmtUSD(p.usd_m2_comparables)}/m²</summary>
    <ul>${c.map(x => `<li><a href="${urlSegura(x.url)}" target="_blank" rel="noopener">${esc(x.portal)}</a> · ${fmtUSD(x.precio_usd)} · ${x.ambientes ?? "?"} amb · ${Math.round(x.m2_cubiertos)} m² · ${fmtUSD(x.usd_m2)}/m²${x.estado ? ` · <span class="chip">${esc(x.estado)}</span>` : ""}</li>`).join("")}</ul>
  </details>`;
}

function botones(p) {
  if (p.estado === "me_interesa")
    return `<button class="btn neutro" data-id="${p.id}" data-accion="nueva">↩ Volver a nuevas</button>
            <button class="btn no" data-id="${p.id}" data-accion="descartada">❌ Descartar</button>`;
  if (p.estado === "descartada")
    return `<button class="btn neutro" data-id="${p.id}" data-accion="nueva">↩ Restaurar</button>`;
  return `<button class="btn si" data-id="${p.id}" data-accion="me_interesa">✅ Me interesa</button>
          <button class="btn no" data-id="${p.id}" data-accion="descartada">❌ Descartar</button>`;
}

function tarjeta(p) {
  const rebaja = p.rebaja_requerida_pct ?? 0;
  const usdM2 = p.precio_usd / p.m2_cubiertos;
  const descuentoVsComps = p.usd_m2_comparables ? 1 - usdM2 / p.usd_m2_comparables : null;
  const oferta = rebaja <= 0
    ? `<div class="oferta">💰 Rinde ≥15% a precio de lista · techo de compra ${fmtUSD(p.precio_max_compra)}</div>`
    : `<div class="oferta warn">🤝 Ofertar hasta ${fmtUSD(p.precio_max_compra)} (rebaja −${fmtPct(rebaja)})</div>`;
  const alt = (p.urls_alternativas || []).map((u, i) => `<a href="${urlSegura(u)}" target="_blank" rel="noopener">alt ${i + 1}</a>`).join(" · ");
  return `<article class="card">
    ${p.imagen_url ? `<div class="foto" style="background-image:url('${urlSegura(p.imagen_url)}')"></div>` : ""}
    <div class="body">
      <div class="row"><span class="barrio">${esc(p.barrio)} <span class="chip">${esc(p.zona)}</span></span><span class="precio">${fmtUSD(p.precio_usd)}</span></div>
      <div class="meta">${esc(p.direccion || p.titulo || "Sin dirección")}</div>
      <div class="chips">
        <span class="chip">${p.ambientes ?? "?"} amb</span>
        <span class="chip">${Math.round(p.m2_cubiertos)} m² cub.</span>
        <span class="chip">${p.antiguedad != null ? p.antiguedad + " años" : "antig. s/d"}</span>
        <span class="chip">${fmtUSD(usdM2)}/m²</span>
        ${descuentoVsComps != null ? `<span class="chip ${descuentoVsComps > 0.3 ? "ok" : ""}">${fmtPct(descuentoVsComps)} bajo reciclados</span>` : ""}
        ${p.expensas_usd ? `<span class="chip">Exp. ${fmtUSD(p.expensas_usd)}</span>` : ""}
        <span class="chip ${(p.roi_media ?? 0) >= 0.15 ? "ok" : "warn"}">ROI Intermedia ${fmtPct(p.roi_media)}</span>
      </div>
      ${oferta}
      ${INFORMES.has(p.huella) ? `<a class="btn neutro informe" href="informes/${encodeURIComponent(p.huella)}.html" target="_blank" rel="noopener">📄 Ver informe completo</a>` : ""}
      ${tablaEscenarios(p)}
      ${bloqueEntorno(p)}
      ${bloqueComparables(p)}
      <div class="meta"><a href="${urlSegura(p.url)}" target="_blank" rel="noopener">Ver en ${esc(p.portal)}</a>${alt ? " · " + alt : ""}</div>
    </div>
    <div class="acciones">${botones(p)}</div>
  </article>`;
}

function render() {
  for (const e of ["nueva", "me_interesa", "descartada"])
    document.getElementById("c-" + e).textContent = "(" + props.filter(p => p.estado === e).length + ")";
  const q = document.getElementById("buscar").value.trim().toLowerCase();
  let lista = props.filter(p => p.estado === filtroEstado);
  if (q) lista = lista.filter(p => [p.barrio, p.direccion, p.portal, p.titulo, p.zona].join(" ").toLowerCase().includes(q));
  ordenar(lista);
  document.getElementById("lista").innerHTML = lista.length
    ? lista.map(tarjeta).join("")
    : `<div class="vacio">No hay propiedades en esta sección.</div>`;
}

document.querySelectorAll("[data-estado]").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll("[data-estado]").forEach(x => x.classList.remove("active"));
  b.classList.add("active"); filtroEstado = b.dataset.estado; render();
}));
document.getElementById("orden").addEventListener("change", render);
document.getElementById("buscar").addEventListener("input", render);
document.getElementById("lista").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-accion]");
  if (b) cambiarEstado(Number(b.dataset.id), b.dataset.accion, b);
});
document.getElementById("tema").addEventListener("click", () => {
  const raiz = document.documentElement;
  const oscuro = raiz.dataset.theme ? raiz.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  raiz.dataset.theme = oscuro ? "light" : "dark";
  try { localStorage.setItem("tema", raiz.dataset.theme); } catch (e) {}
});
try { const t = localStorage.getItem("tema"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}

render();
cargar();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
