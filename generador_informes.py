"""
generador_informes.py — Informe web individual de cada PH aprobado.

Genera `informes/<huella>.html`: un HTML autocontenido (sin dependencias
externas), responsive, con modo claro/oscuro y listo para imprimir a PDF.

Secciones (en orden):
    1. Encabezado              6. Según el nivel de obra
    2. Banda de 5 KPIs          7. Comparables con las mismas características
    3. Veredicto ejecutivo      8. Zona por precio USD/m²
    4. Ficha técnica            9. Cómo es la zona (Gemini)
    5. Hasta cuánto ofertar    10. Las cuentas a la oferta máxima (obra intermedia)

Trabaja sobre la fila (dict) tal como se guarda en Supabase, así sirve tanto en
la corrida diaria como para regenerar informes desde la base.
"""
from __future__ import annotations

import html
import logging
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import config as cfg
from motor_financiero import ESCENARIO_BASE, MotorFinanciero

log = logging.getLogger("informes")
ART = timezone(timedelta(hours=-3))
ORDEN_OBRA = ("Simple", "Intermedia", "Integral")
_RE_HUELLA = re.compile(r"[0-9a-f]{40}")


# =========================================================================== #
# Formato
# =========================================================================== #
def e(texto: Any) -> str:
    return html.escape(str(texto if texto is not None else ""), quote=True)


def usd(n: Optional[float]) -> str:
    return "—" if n is None else "USD " + f"{float(n):,.0f}".replace(",", ".")


def num(n: Optional[float], dec: int = 0) -> str:
    if n is None:
        return "—"
    s = f"{float(n):,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def pct(n: Optional[float], signo: bool = False) -> str:
    if n is None:
        return "—"
    s = f"{float(n) * 100:+.1f}" if signo else f"{float(n) * 100:.1f}"
    return s.replace(".", ",") + " %"


def vs_publicado(baja: Optional[float]) -> str:
    """baja_xx_pct = 1 − tope/publicado → texto '−12,3 % vs publicado'."""
    if baja is None:
        return "—"
    return pct(-float(baja), signo=True).replace("-", "−") + " vs publicado"


def url_segura(u: Optional[str]) -> str:
    return e(u) if re.match(r"^https?://", u or "", re.I) else "#"


def fecha_corta(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(ART).strftime("%d/%m/%Y")
    except ValueError:
        return None


def ruta_informe(huella: str) -> str:
    """Ruta relativa (desde index.html) del informe de una propiedad."""
    return f"informes/{huella}.html"


# =========================================================================== #
# Secciones
# =========================================================================== #
def _encabezado(f: dict, base: dict, fecha_comps: str) -> str:
    titulo = f.get("direccion") or f.get("titulo") or f"PH en {f.get('barrio')}"
    sub = (
        f"PH de {f.get('ambientes') or '?'} ambientes · {num(f.get('m2_cubiertos'))} m² cubiertos · "
        f"obra intermedia estimada {usd(base.get('costo_reforma'))} · ROI neto al publicado {pct(base.get('roi'))}"
    )
    return f"""
<header class="hero">
  <div class="eyebrow">{e(f.get('barrio'))} · {e(f.get('zona'))} · Comparables al {e(fecha_comps)}</div>
  <h1>{e(titulo)}</h1>
  <p class="sub">{e(sub)}</p>
  <div class="acciones no-print">
    <a class="btn primario" href="{url_segura(f.get('url'))}" target="_blank" rel="noopener">Ver aviso en {e(f.get('portal'))}</a>
    <button class="btn" type="button" onclick="window.print()">Imprimir / PDF</button>
    <a class="btn" href="../index.html">← Tablero</a>
  </div>
</header>"""


def _kpis(f: dict, base: dict) -> str:
    m2 = float(f.get("m2_cubiertos") or 0) or None
    precio = float(f["precio_usd"])
    valor = f.get("valor_venta_estimado")
    tarjetas = [
        ("Publicado", usd(precio), f"{usd(precio / m2) if m2 else '—'}/m²", ""),
        ("Vale reciclado", usd(valor), f"{usd(float(valor) / m2) if (m2 and valor) else '—'}/m² al cierre", ""),
        ("Oferta máx. 20 %", usd(base.get("oferta_max_20")), vs_publicado(base.get("baja_20_pct")), "ok"),
        ("Techo 15 %", usd(base.get("techo_15")), vs_publicado(base.get("baja_15_pct")), "warn"),
        ("ROI al publicado", pct(base.get("roi")), f"Break-even {usd(base.get('break_even'))}",
         "ok" if (base.get("roi") or 0) >= 0.20 else "warn" if (base.get("roi") or 0) >= 0.15 else "bad"),
    ]
    celdas = "".join(
        f'<div class="kpi {c}"><div class="kpi-l">{e(l)}</div><div class="kpi-v">{e(v)}</div><div class="kpi-s">{e(s)}</div></div>'
        for l, v, s, c in tarjetas
    )
    return f'<section class="kpis" aria-label="Indicadores clave">{celdas}</section>'


def veredicto(base: dict) -> tuple[str, str, str]:
    """(título, nota, clase de color)."""
    roi = base.get("roi") or 0
    oferta20 = base.get("oferta_max_20") or 0
    limite = base.get("limite_negociacion") or 0
    if roi >= 0.20:
        return (
            "Oportunidad directa",
            f"Rinde {pct(roi)} neto al precio publicado (break-even {usd(base.get('break_even'))}). "
            "Moverse rápido: coordinar visita y reservar si el estado coincide con el aviso.",
            "ok",
        )
    if oferta20 >= limite:
        return (
            "Ofertar con margen",
            f"Para 20 % neto hay que entrar en {usd(oferta20)} ({vs_publicado(base.get('baja_20_pct'))}), "
            f"dentro del −15 % de negociación ({usd(limite)}). Techo para 15 %: {usd(base.get('techo_15'))}.",
            "accent",
        )
    return (
        "Coordinar visita",
        f"El 20 % exige {usd(oferta20)} ({vs_publicado(base.get('baja_20_pct'))}), por debajo del −15 % "
        f"({usd(limite)}). El 15 % se logra hasta {usd(base.get('techo_15'))}: verificar estado y obra "
        "en la visita antes de ofertar.",
        "warn",
    )


def _veredicto(base: dict) -> str:
    titulo, nota, clase = veredicto(base)
    return f"""
<section class="veredicto {clase}">
  <div class="v-et">Veredicto</div>
  <div class="v-t">{e(titulo)}</div>
  <p>{e(nota)}</p>
</section>"""


def _ficha(f: dict) -> str:
    fi = f.get("ficha") or {}
    sup = [f"{num(fi.get('m2_cubiertos') or f.get('m2_cubiertos'))} m² cub."]
    if fi.get("m2_totales") or f.get("m2_totales"):
        sup.append(f"{num(fi.get('m2_totales') or f.get('m2_totales'))} m² tot.")
    if fi.get("m2_patio"):
        sup.append(f"patio {num(fi['m2_patio'])} m²")
    if fi.get("m2_terraza"):
        sup.append(f"terraza {num(fi['m2_terraza'])} m²")

    tipo = " · ".join(x for x in [fi.get("tipo") or "PH", fi.get("disposicion")] if x)
    if fi.get("unidades_en_lote"):
        tipo += f" · {fi['unidades_en_lote']} unidades en el lote"
    amb = f"{fi.get('ambientes') or f.get('ambientes') or '—'}"
    if fi.get("dormitorios"):
        amb += f" ({fi['dormitorios']} dormitorios)"
    antig = fi.get("antiguedad_anios") if fi.get("antiguedad_anios") is not None else f.get("antiguedad")

    if fi.get("expensas"):
        mon = str(fi.get("moneda_expensas") or "ARS").upper()
        expensas = f"{'USD' if 'USD' in mon else 'ARS'} {num(fi['expensas'])} (informativo)"
    elif f.get("expensas_usd"):
        expensas = f"{usd(f['expensas_usd'])} (informativo)"
    else:
        expensas = "No informa"

    otros = list(fi.get("otros") or [])
    if fi.get("apto_credito") is True:
        otros.insert(0, "Apto crédito")
    elif fi.get("apto_credito") is False:
        otros.insert(0, "No apto crédito")

    corredor = " · ".join(x for x in [
        f.get("portal"), fi.get("inmobiliaria"), f"Mat. {fi['matricula']}" if fi.get("matricula") else None
    ] if x)

    filas = [
        ("Tipo", e(tipo)),
        ("Superficie", e(" · ".join(sup))),
        ("Ambientes", e(amb)),
        ("Baños", e(fi.get("banos") or "—")),
        ("Antigüedad", e(f"{antig} años" if antig is not None else "No informa")),
        ("Estado", e(fi.get("estado") or ("Reciclado" if f.get("reciclado") else "A reciclar" if f.get("reciclado") is False else "—"))),
        ("Planta / Piso", e(fi.get("planta_piso") or "—")),
        ("Expensas", e(expensas)),
        ("Otros", e(" · ".join(otros[:10]) or "—")),
        ("Aviso / Inmobiliaria", f'{e(corredor)} · <a href="{url_segura(f.get("url"))}" target="_blank" rel="noopener">ver aviso</a>'),
    ]
    cuerpo = "".join(f"<tr><th scope='row'>{k}</th><td>{v}</td></tr>" for k, v in filas)
    desc = fi.get("descripcion")
    extra = f'<details class="desc"><summary>Descripción del aviso</summary><p>{e(desc)}</p></details>' if desc else ""
    aviso = "" if fi else '<p class="nota">No se pudo leer la ficha individual del aviso: se muestran los datos del listado.</p>'
    return f"""
<section>
  <h2>Ficha técnica</h2>
  {aviso}
  <div class="tabla-wrap"><table class="ficha"><tbody>{cuerpo}</tbody></table></div>
  {extra}
</section>"""


def _escala(f: dict, base: dict) -> str:
    precio = float(f["precio_usd"])
    marcas = [
        ("−15 %", base.get("limite_negociacion"), "neutro"),
        ("Oferta 20 %", base.get("oferta_max_20"), "ok"),
        ("Techo 15 %", base.get("techo_15"), "warn"),
        ("Publicado", precio, "pub"),
        ("Break-even", base.get("break_even"), "bad"),
    ]
    marcas = [(n, float(v), c) for n, v, c in marcas if v]
    vals = [v for _, v, _ in marcas]
    lo, hi = min(vals) * 0.93, max(vals) * 1.04
    pos = lambda v: max(0.0, min(100.0, (v - lo) / (hi - lo) * 100))  # noqa: E731
    p20, p15 = pos(base.get("oferta_max_20") or lo), pos(base.get("techo_15") or lo)

    zonas = (
        f'<div class="zona z-ok" style="left:0;width:{p20:.2f}%"></div>'
        f'<div class="zona z-warn" style="left:{p20:.2f}%;width:{max(0, p15 - p20):.2f}%"></div>'
        f'<div class="zona z-bad" style="left:{p15:.2f}%;width:{max(0, 100 - p15):.2f}%"></div>'
    )
    pines = []
    for i, (nombre, v, clase) in enumerate(sorted(marcas, key=lambda m: m[1])):
        lado = "arriba" if i % 2 == 0 else "abajo"
        x = pos(v)
        ancla = "izq" if x < 12 else "der" if x > 88 else ""
        pines.append(
            f'<div class="pin {clase} {lado} {ancla}" style="left:{x:.2f}%">'
            f'<span class="pin-t"><b>{e(nombre)}</b> {e(usd(v))}</span></div>'
        )
    leyenda = "".join(
        f'<li><span class="pt {c}"></span>{e(n)} <b>{e(usd(v))}</b> <span class="muted">{e(pct(v / precio - 1, True))}</span></li>'
        for n, v, c in sorted(marcas, key=lambda m: m[1])
    )
    return f"""
<section>
  <h2>Hasta cuánto ofertar</h2>
  <p class="muted">Precio de compra con obra intermedia y venta Base. Verde: ROI neto ≥ 20 % · ámbar: 15–20 % · rojo: &lt; 15 %.</p>
  <div class="escala" role="img" aria-label="Escala de precios de compra">
    <div class="barra">{zonas}</div>
    {''.join(pines)}
  </div>
  <ul class="leyenda">{leyenda}</ul>
</section>"""


def _obra(f: dict, escenarios: dict) -> str:
    filas = []
    for k in ORDEN_OBRA:
        s = escenarios.get(k)
        if not s:
            continue
        clase = "base" if k == ESCENARIO_BASE else ""
        filas.append(
            f"<tr class='{clase}'><th scope='row'>{e(k)}{' ★' if clase else ''}"
            f"<span class='muted'> USD {num(s.get('costo_m2_usd'))}/m²</span></th>"
            f"<td>{usd(s.get('costo_reforma'))}</td>"
            f"<td>{usd(s.get('oferta_max_20'))}<span class='muted'>{e(pct(-(s.get('baja_20_pct') or 0), True))}</span></td>"
            f"<td>{usd(s.get('techo_15'))}<span class='muted'>{e(pct(-(s.get('baja_15_pct') or 0), True))}</span></td>"
            f"<td class='{'pos' if (s.get('roi') or 0) >= 0.15 else 'neg' if (s.get('roi') or 0) < 0 else ''}'>{pct(s.get('roi'))}"
            f"<span class='muted'>{pct(s.get('roi_publicado_pesimista'))} a {pct(s.get('roi_publicado_optimista'))}</span></td></tr>"
        )
    return f"""
<section>
  <h2>Según el nivel de obra</h2>
  <p class="muted">El nivel de obra refleja cuánto trabajo necesita el PH para quedar reciclado a nuevo; el valor de venta es el mismo.</p>
  <div class="tabla-wrap"><table class="num">
    <thead><tr><th>Obra</th><th>Costo</th><th>Oferta máx. 20 %</th><th>Techo 15 %</th><th>ROI al publicado<span class="muted">pesim. a optim.</span></th></tr></thead>
    <tbody>{''.join(filas)}</tbody>
  </table></div>
</section>"""


def _comparables(f: dict, reventa: dict) -> str:
    comps = f.get("comparables") or []
    filas = "".join(
        f"<tr><td class='izq'><a href='{url_segura(c.get('url'))}' target='_blank' rel='noopener'>{e((c.get('titulo') or c.get('portal') or 'Aviso')[:90])}</a>"
        f"<span class='muted'>{e(c.get('portal'))} · {e(c.get('ambientes') or '?')} amb</span></td>"
        f"<td>{num(c.get('m2_cubiertos'))}</td><td class='izq'>{e(c.get('estado') or 'Reciclado')}</td>"
        f"<td>{usd(c.get('precio_usd'))}</td><td>{usd(c.get('usd_m2'))}</td></tr>"
        for c in comps
    )
    pie = []
    for k, etiqueta in (("Base", "mediana − 5 % de cierre"), ("Pesimista", "percentil 25 − 5 %"), ("Optimista", "percentil 75 − 5 %")):
        r = reventa.get(k)
        if r:
            pie.append(
                f"<li><b>{e(k)}</b> ({e(etiqueta)}): {usd(r.get('usd_m2'))}/m² × 0,95 × {num(f.get('m2_cubiertos'))} m² = "
                f"<b>{usd(r.get('precio_venta'))}</b></li>"
            )
    return f"""
<section>
  <h2>Lo que vale: comparables con las mismas características</h2>
  <p class="muted">PH reciclados de {e(f.get('barrio'))}, ±1 ambiente y superficie similar.</p>
  <div class="tabla-wrap"><table class="num">
    <thead><tr><th class="izq">Descripción</th><th>m² cub.</th><th class="izq">Estado</th><th>Precio</th><th>USD/m²</th></tr></thead>
    <tbody>{filas or "<tr><td colspan='5'>Sin comparables guardados.</td></tr>"}</tbody>
  </table></div>
  <ul class="pie">{''.join(pie)}</ul>
</section>"""


def _barras(f: dict, base: dict) -> str:
    m2 = float(f.get("m2_cubiertos") or 0)
    if not m2:
        return ""
    comps = f.get("comparables") or []
    a_nuevo = [float(c["usd_m2"]) for c in comps if c.get("estado") == "Reciclado a nuevo" and c.get("usd_m2")]
    items = [
        ("Compra para 20 %", (base.get("oferta_max_20") or 0) / m2, "ok"),
        ("Este aviso", float(f["precio_usd"]) / m2, "pub"),
        ("Comparables (mediana)", f.get("usd_m2_comparables"), "accent"),
        ("Reciclados a nuevo", statistics.median(a_nuevo) if a_nuevo else None, "accent2"),
    ]
    maximo = max(float(v) for _, v, _ in items if v) or 1
    filas = "".join(
        f'<div class="bar-fila"><div class="bar-l">{e(n)}</div>'
        f'<div class="bar-pista"><div class="bar {c}" style="width:{(float(v) / maximo * 100) if v else 0:.1f}%"></div></div>'
        f'<div class="bar-v">{e(usd(v) if v else "sin datos")}</div></div>'
        for n, v, c in items
    )
    return f"""
<section>
  <h2>Zona por precio USD/m²</h2>
  <div class="barras">{filas}</div>
</section>"""


def _zona(f: dict) -> str:
    en = f.get("entorno") or {}
    bloques = [
        ("Ubicación", en.get("zona_ubicacion")),
        ("Comercio", en.get("zona_comercio")),
        ("Movilidad", en.get("zona_movilidad")),
        ("Perfil de comprador", en.get("zona_perfil_comprador")),
        ("Día y noche", en.get("zona_dia_y_noche")),
    ]
    bloques = [(t, x) for t, x in bloques if x]
    if not bloques and en.get("resumen"):
        bloques = [("Resumen", en["resumen"])]
    if not bloques:
        return ""
    cuerpo = "".join(f"<div class='zona-b'><h3>{e(t)}</h3><p>{e(x)}</p></div>" for t, x in bloques)
    puntaje = f"<span class='chip'>Entorno {e(en['puntaje_entorno'])}/10</span>" if en.get("puntaje_entorno") else ""
    riesgos = en.get("riesgos") or []
    riesgo_html = f"<p class='muted'><b>Riesgos:</b> {e(' · '.join(map(str, riesgos)))}</p>" if riesgos else ""
    aprox = "<p class='nota'>Ubicación aproximada: el aviso no informa la altura exacta.</p>" if en.get("precision_ubicacion") == "baja" else ""
    return f"""
<section>
  <h2>Cómo es la zona {puntaje}</h2>
  {aprox}
  <div class="zona-grid">{cuerpo}</div>
  {riesgo_html}
</section>"""


def _cuentas(f: dict, base: dict, motor: MotorFinanciero) -> str:
    oferta = base.get("oferta_max_20") or 0
    m2 = float(f.get("m2_cubiertos") or 0)
    c = motor.cuentas(oferta, m2, float(base.get("precio_venta") or 0), float(base.get("costo_m2_usd") or 0))
    p = motor.p
    filas = [
        ("Compra (oferta máx. 20 %)", c["compra"], ""),
        (f"Gastos de compra ({num(p.gastos_compra_pct * 100, 2)} %)", c["gastos_compra"], ""),
        (f"Obra intermedia ({num(m2)} m² × USD {num(base.get('costo_m2_usd'))})", c["obra"], ""),
        (f"Tenencia durante la obra (~{p.meses_tenencia} meses)", c["tenencia"], ""),
        ("Inversión total", c["inversion_total"], "total"),
        (f"Venta estimada (mediana − {num((1 - p.ajuste_cierre) * 100)} % cierre)", c["valor_venta"], ""),
        (f"Gastos de venta ({num(p.gastos_venta_pct * 100, 2)} %)", -c["gastos_venta"], ""),
        ("Venta neta", c["venta_neta"], "total"),
        ("Ganancia bruta", c["ganancia_bruta"], ""),
        (f"Impuesto ({num(p.impuesto_ganancia_pct * 100)} % sobre la ganancia)", -c["impuesto"], ""),
        ("Ganancia neta", c["ganancia_neta"], "total fuerte"),
    ]
    cuerpo = "".join(
        f"<tr class='{cl}'><th scope='row'>{e(k)}</th><td>{e(usd(v).replace('USD -', '− USD '))}</td></tr>"
        for k, v, cl in filas
    )
    return f"""
<section class="cuentas">
  <h2>Las cuentas a la oferta máxima (obra intermedia)</h2>
  <div class="tabla-wrap"><table class="num"><tbody>
    {cuerpo}
    <tr class="total fuerte"><th scope="row">ROI neto</th><td>{pct(c['roi'])}</td></tr>
  </tbody></table></div>
  <p class="rango">Rango de ROI neto a esta oferta: <b>{pct(base.get('roi_oferta20_pesimista'))}</b> (venta pesimista)
     a <b>{pct(base.get('roi_oferta20_optimista'))}</b> (venta optimista).</p>
</section>"""


# =========================================================================== #
# Documento
# =========================================================================== #
def generar_html(f: dict, motor: Optional[MotorFinanciero] = None) -> Optional[str]:
    escenarios = f.get("escenarios") or {}
    base = escenarios.get(ESCENARIO_BASE)
    if not base or "oferta_max_20" not in base:
        return None  # fila anterior al formato nuevo
    motor = motor or MotorFinanciero()
    reventa = escenarios.get("Reventa") or {}
    fechas = [x for x in (fecha_corta(c.get("capturado_en")) for c in (f.get("comparables") or [])) if x]
    fecha_comps = max(fechas, key=lambda d: datetime.strptime(d, "%d/%m/%Y")) if fechas else (
        fecha_corta(f.get("creado_en")) or datetime.now(ART).strftime("%d/%m/%Y")
    )
    titulo_doc = f"PH {f.get('barrio') or ''} · {usd(f.get('precio_usd'))}"
    cuerpo = "".join([
        _encabezado(f, base, fecha_comps),
        _kpis(f, base),
        _veredicto(base),
        _ficha(f),
        _escala(f, base),
        _obra(f, escenarios),
        _comparables(f, reventa),
        _barras(f, base),
        _zona(f),
        _cuentas(f, base, motor),
    ])
    generado = datetime.now(ART).strftime("%d/%m/%Y %H:%M")
    return (
        PLANTILLA.replace("__TITULO__", e(titulo_doc))
        .replace("__GENERADO__", e(generado))
        .replace("__CUERPO__", cuerpo)
    )


def generar_informe(fila: dict, directorio: Path = cfg.INFORMES_DIR, motor: Optional[MotorFinanciero] = None) -> Optional[Path]:
    huella = str(fila.get("huella") or "")
    if not _RE_HUELLA.fullmatch(huella):
        return None
    contenido = generar_html(fila, motor)
    if contenido is None:
        return None
    directorio.mkdir(parents=True, exist_ok=True)
    ruta = directorio / f"{huella}.html"
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def generar_informes(filas: Iterable[dict], solo_faltantes: bool = False, directorio: Path = cfg.INFORMES_DIR) -> list[Path]:
    motor = MotorFinanciero()
    rutas = []
    for f in filas:
        if not f.get("aprobada"):
            continue
        if solo_faltantes and (directorio / f"{f.get('huella')}.html").exists():
            continue
        try:
            r = generar_informe(f, directorio, motor)
        except Exception:  # noqa: BLE001 — un informe roto no frena al resto
            log.exception("No se pudo generar el informe de %s", f.get("url"))
            continue
        if r:
            rutas.append(r)
    if rutas:
        log.info("Informes generados: %d en %s", len(rutas), directorio)
    return rutas


def huellas_con_informe(directorio: Path = cfg.INFORMES_DIR) -> list[str]:
    if not directorio.exists():
        return []
    return sorted(p.stem for p in directorio.glob("*.html") if _RE_HUELLA.fullmatch(p.stem))


# =========================================================================== #
# Plantilla
# =========================================================================== #
PLANTILLA = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>__TITULO__</title>
<style>
:root{
  --bg:#f5f6f8;--panel:#fff;--text:#15181e;--muted:#5d6572;--border:#e1e4e9;--chip:#eef1f5;
  --accent:#1f5fd1;--accent-bg:#e8f0fd;--accent2:#7a4cc2;
  --ok:#17803a;--ok-bg:#e6f4ea;--warn:#9a6200;--warn-bg:#fff4d6;--bad:#c22a2a;--bad-bg:#fdeaea;
  --pub:#15181e;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0f1115;--panel:#171a21;--text:#e8eaef;--muted:#9aa3b2;--border:#2a2f3a;--chip:#222733;
    --accent:#5b93ff;--accent-bg:#14213b;--accent2:#a985e8;
    --ok:#3fb950;--ok-bg:#12291a;--warn:#e3b341;--warn-bg:#2b230f;--bad:#ff6b6b;--bad-bg:#2d1517;
    --pub:#e8eaef;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
main{max-width:900px;margin:0 auto;padding:20px 16px 48px;display:flex;flex-direction:column;gap:16px}
section,.hero{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px}
h1{font-size:clamp(20px,4.6vw,28px);line-height:1.2;margin:4px 0 6px;text-wrap:balance}
h2{font-size:17px;margin:0 0 10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
h3{font-size:14px;margin:0 0 4px}
p{margin:0 0 8px}
a{color:var(--accent)}
.eyebrow{font-size:12.5px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:600}
.sub{color:var(--muted)}
.muted{color:var(--muted);font-size:12.5px;font-weight:400}
td .muted,th .muted{display:block}
.nota{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:6px 10px;font-size:13px}
.chip{background:var(--chip);color:var(--muted);border-radius:6px;padding:2px 8px;font-size:12.5px;font-weight:600}
.acciones{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.btn{display:inline-flex;align-items:center;border:1px solid var(--border);background:var(--panel);color:var(--text);
  padding:9px 14px;border-radius:10px;font:600 14px/1 inherit;text-decoration:none;cursor:pointer}
.btn.primario{background:var(--accent);border-color:var(--accent);color:#fff}

/* KPIs */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;padding:12px}
.kpi{border-radius:10px;padding:10px 12px;background:var(--chip)}
.kpi.ok{background:var(--ok-bg)}.kpi.warn{background:var(--warn-bg)}.kpi.bad{background:var(--bad-bg)}
.kpi-l{font-size:12px;color:var(--muted);font-weight:600}
.kpi-v{font-size:20px;font-weight:750;font-variant-numeric:tabular-nums;margin:2px 0}
.kpi.ok .kpi-v{color:var(--ok)}.kpi.warn .kpi-v{color:var(--warn)}.kpi.bad .kpi-v{color:var(--bad)}
.kpi-s{font-size:12px;color:var(--muted)}

/* Veredicto */
.veredicto{border-left:6px solid var(--accent)}
.veredicto.ok{border-left-color:var(--ok)}.veredicto.warn{border-left-color:var(--warn)}
.v-et{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600}
.v-t{font-size:22px;font-weight:800;margin:2px 0 6px}
.veredicto.ok .v-t{color:var(--ok)}.veredicto.warn .v-t{color:var(--warn)}.veredicto.accent .v-t{color:var(--accent)}

/* Tablas */
.tabla-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 6px;border-bottom:1px solid var(--border);vertical-align:top}
thead th{font-size:12.5px;color:var(--muted);font-weight:600;text-align:right}
table.num td,table.num thead th{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.num th:first-child,.izq{text-align:left!important;white-space:normal!important}
table.ficha th{text-align:left;width:34%;color:var(--muted);font-weight:600}
tbody th{text-align:left;font-weight:600}
tr.base{background:var(--accent-bg)}
tr.total th,tr.total td{border-top:2px solid var(--border);font-weight:700}
tr.fuerte th,tr.fuerte td{font-size:15.5px}
.pos{color:var(--ok)}.neg{color:var(--bad)}
.desc summary{cursor:pointer;color:var(--accent);font-weight:600;margin-top:10px}
.desc p{white-space:pre-line;margin-top:8px}
.pie{margin:10px 0 0;padding-left:18px;font-size:13.5px}
.rango{margin-top:10px;font-size:14px}

/* Escala */
.escala{position:relative;height:132px;margin:8px 4px}
.barra{position:absolute;left:0;right:0;top:64px;height:16px;border-radius:8px;overflow:hidden;background:var(--chip)}
.zona{position:absolute;top:0;bottom:0}
.z-ok{background:var(--ok)}.z-warn{background:var(--warn)}.z-bad{background:var(--bad)}
.zona{opacity:.85}
.pin{position:absolute;top:58px;height:28px;width:0;border-left:2px solid var(--text)}
.pin.pub{border-left-width:3px}
.pin-t{position:absolute;left:0;transform:translateX(-50%);white-space:nowrap;font-size:11.5px;line-height:1.25;
  background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:2px 6px;text-align:center}
.pin-t b{display:block;font-size:11px;color:var(--muted)}
.pin.arriba .pin-t{bottom:calc(100% + 4px)}.pin.abajo .pin-t{top:calc(100% + 4px)}
.pin.izq .pin-t{transform:translateX(-8px)}.pin.der .pin-t{transform:translateX(calc(-100% + 8px))}
.leyenda{list-style:none;padding:0;margin:8px 0 0;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:4px 14px;font-size:13.5px}
.pt{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;background:var(--muted)}
.pt.ok{background:var(--ok)}.pt.warn{background:var(--warn)}.pt.bad{background:var(--bad)}.pt.pub{background:var(--pub)}

/* Barras USD/m² */
.barras{display:flex;flex-direction:column;gap:10px}
.bar-fila{display:grid;grid-template-columns:minmax(110px,170px) 1fr auto;gap:10px;align-items:center;font-size:13.5px}
.bar-pista{background:var(--chip);border-radius:6px;height:14px;overflow:hidden}
.bar{height:100%;border-radius:6px;background:var(--muted)}
.bar.ok{background:var(--ok)}.bar.pub{background:var(--pub)}.bar.accent{background:var(--accent)}.bar.accent2{background:var(--accent2)}
.bar-v{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}

/* Zona */
.zona-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.zona-b p{font-size:14px;color:var(--text)}

footer{color:var(--muted);font-size:12px;text-align:center}

@media (max-width:520px){
  section,.hero{padding:14px}
  .bar-fila{grid-template-columns:1fr auto}.bar-pista{grid-column:1/-1;order:3}
  .kpi-v{font-size:18px}
  /* En pantallas chicas las etiquetas se pisan: quedan las marcas y la leyenda. */
  .escala{height:44px}.barra{top:14px}.pin{top:8px}.pin-t{display:none}
}
@media print{
  :root{--bg:#fff;--panel:#fff;--text:#000;--muted:#444;--border:#ccc;--chip:#f1f1f1;--pub:#000}
  body{font-size:12px;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  main{max-width:none;padding:0;gap:10px}
  section,.hero{border:1px solid #ccc;break-inside:avoid;page-break-inside:avoid}
  .no-print{display:none!important}
  .tabla-wrap{overflow:visible}
  a{color:#000;text-decoration:none}
  @page{margin:12mm}
}
</style>
</head>
<body>
<main>
__CUERPO__
<footer>Informe generado el __GENERADO__ · Valores estimados a partir de avisos publicados; no constituyen tasación.</footer>
</main>
</body>
</html>
"""
