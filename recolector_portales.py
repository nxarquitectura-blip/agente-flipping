"""
recolector_portales.py — Extracción de avisos de Zonaprop, Argenprop, MercadoLibre,
Mudafy, Mitula y RE/MAX vía Apify.

Estrategia por portal:
  1. Si hay un actor dedicado configurado (APIFY_ACTOR_<PORTAL>), se usa ese actor
     y sus resultados se normalizan con un mapeo flexible de campos.
  2. Si no, se usa el crawler genérico de Apify (website-content-crawler) para
     bajar la página de resultados de cada barrio y Gemini extrae los avisos en
     JSON estructurado. Es resistente a cambios de diseño de los portales.

Luego:
  • Filtros: 18 barrios, 3+ ambientes, ≤ USD 150k, > 40 m² cubiertos,
    antigüedad > 20 años, publicados en los últimos 3 días.
  • Anti-duplicados: huella (dirección + m² + ambientes) entre portales y contra
    lo ya guardado en Supabase.
  • Comparables: 4-5 avisos "reciclados a nuevo" del mismo barrio y superficie
    similar, cacheados en Supabase (tabla `comparables`) para no re-scrapear a diario.
"""
from __future__ import annotations

import hashlib
import logging
import re
import statistics
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import requests
from pydantic import BaseModel, Field

import config as cfg
from supabase_db import SupabaseREST, filtro_in

log = logging.getLogger("recolector")


# =========================================================================== #
# Modelos
# =========================================================================== #
@dataclass
class Aviso:
    portal: str
    url: str
    barrio: str
    zona: str
    precio_usd: float
    m2_cubiertos: float
    ambientes: Optional[int] = None
    titulo: str = ""
    direccion: str = ""
    m2_totales: Optional[float] = None
    antiguedad: Optional[int] = None
    expensas_usd: Optional[float] = None
    dias_publicado: Optional[int] = None
    imagen_url: Optional[str] = None
    reciclado: Optional[bool] = None
    urls_alternativas: list[str] = field(default_factory=list)
    huella: str = ""

    @property
    def usd_m2(self) -> float:
        return self.precio_usd / self.m2_cubiertos if self.m2_cubiertos else 0.0

    def completitud(self) -> int:
        return sum(1 for v in asdict(self).values() if v not in (None, "", [], False))


@dataclass
class Comparable:
    url: str
    portal: str
    barrio: str
    precio_usd: float
    m2_cubiertos: float
    usd_m2: float
    ambientes: Optional[int] = None
    titulo: str = ""


class AvisoExtraido(BaseModel):
    """Esquema que Gemini completa para cada aviso de una página de resultados."""
    url: Optional[str] = Field(None, description="Link a la ficha del aviso")
    titulo: Optional[str] = None
    direccion: Optional[str] = Field(None, description="Calle y altura, tal como figura")
    barrio: Optional[str] = None
    precio: Optional[float] = Field(None, description="Número sin separadores de miles")
    moneda: Optional[str] = Field(None, description="USD o ARS")
    m2_cubiertos: Optional[float] = None
    m2_totales: Optional[float] = None
    ambientes: Optional[int] = None
    antiguedad_anios: Optional[int] = None
    expensas: Optional[float] = None
    moneda_expensas: Optional[str] = Field(None, description="ARS o USD")
    dias_publicado: Optional[int] = None
    reciclado: Optional[bool] = Field(None, description="true si está reciclado/a nuevo; false si es para reciclar")
    imagen_url: Optional[str] = None


# =========================================================================== #
# Utilidades
# =========================================================================== #
def con_reintentos(fn: Callable[[], Any], intentos: int = 3, espera: float = 3.0, etiqueta: str = "") -> Any:
    ultimo: Optional[Exception] = None
    for i in range(intentos):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — se registra y reintenta
            ultimo = exc
            log.warning("%s falló (intento %d/%d): %s", etiqueta or "operación", i + 1, intentos, exc)
            time.sleep(espera * (2 ** i))
    raise RuntimeError(f"{etiqueta} falló tras {intentos} intentos") from ultimo


def a_numero(valor: Any) -> Optional[float]:
    """Convierte '$ 120.000', 'USD 95,500', '72 m²', 3 → float. Formato AR (punto de miles)."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, dict):
        for k in ("amount", "value", "valor", "price"):
            if k in valor:
                return a_numero(valor[k])
        return None
    s = re.sub(r"[^\d,\.]", "", str(valor))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "." in s and re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def a_entero(valor: Any) -> Optional[int]:
    n = a_numero(valor)
    return int(round(n)) if n is not None else None


def normalizar_texto(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def obtener_tipo_cambio() -> float:
    """Dólar blue (venta) desde dolarapi.com; si falla, usa el valor de respaldo."""
    try:
        r = requests.get("https://dolarapi.com/v1/dolares/blue", timeout=10)
        r.raise_for_status()
        venta = float(r.json()["venta"])
        if venta > 0:
            return venta
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo obtener el tipo de cambio (%s). Uso %s", exc, cfg.TIPO_CAMBIO_DEFAULT)
    return cfg.TIPO_CAMBIO_DEFAULT


def buscar_barrio(*textos: Optional[str]) -> Optional[cfg.Barrio]:
    """Identifica el barrio a partir de una URL o texto (prioriza slugs más largos)."""
    candidatos = sorted(cfg.BARRIOS, key=lambda b: len(b.slug), reverse=True)
    for texto in textos:
        if not texto:
            continue
        t = cfg.slugify(texto)
        for b in candidatos:
            if b.slug in t:
                return b
    return None


# =========================================================================== #
# Apify
# =========================================================================== #
class ClienteApify:
    def __init__(self, token: str):
        from apify_client import ApifyClient

        self.cliente = ApifyClient(token)

    def correr_actor(self, actor_id: str, entrada: dict) -> list[dict]:
        log.info("Apify: corriendo %s", actor_id)
        run = self.cliente.actor(actor_id).call(
            run_input=entrada, timeout_secs=cfg.APIFY_TIMEOUT_SEG, memory_mbytes=cfg.APIFY_MEMORIA_MB
        )
        if not run:
            raise RuntimeError(f"El actor {actor_id} no devolvió una ejecución")
        if run.get("status") not in ("SUCCEEDED", None):
            log.warning("Actor %s terminó con estado %s", actor_id, run.get("status"))
        return list(self.cliente.dataset(run["defaultDatasetId"]).iterate_items())

    def crawlear(self, urls: list[str]) -> list[dict]:
        """Descarga páginas de resultados (sin seguir links) y devuelve su markdown."""
        entrada = {
            "startUrls": [{"url": u} for u in urls],
            "crawlerType": "playwright:firefox",
            "maxCrawlDepth": 0,
            "maxCrawlPages": len(urls),
            "htmlTransformer": "none",          # no recortar: necesitamos todas las tarjetas
            "saveMarkdown": True,
            "removeCookieWarnings": True,
            "dynamicContentWaitSecs": 8,
            "maxRequestRetries": 3,
        }
        if cfg.APIFY_PROXY_GROUP:
            entrada["proxyConfiguration"] = {"useApifyProxy": True, "apifyProxyGroups": [cfg.APIFY_PROXY_GROUP]}
        else:
            entrada["proxyConfiguration"] = {"useApifyProxy": True}
        return self.correr_actor(cfg.APIFY_CRAWLER_ACTOR, entrada)


# =========================================================================== #
# Extracción con Gemini
# =========================================================================== #
class ExtractorAvisosIA:
    MAX_CARACTERES = 120_000

    PROMPT = """Sos un extractor de datos inmobiliarios de Argentina.
Te paso el contenido (markdown) de una página de RESULTADOS del portal {portal}: {url}

Extraé TODOS los avisos de venta de departamentos o PH listados. Reglas:
- url: link absoluto a la ficha del aviso. Si es relativo, anteponé {dominio}.
- precio: número sin separadores. moneda = "USD" si dice USD/U$S/US$/u$d, "ARS" si sólo dice $.
- m2_cubiertos: superficie CUBIERTA. Si sólo figura una superficie total, ponela en m2_totales y dejá m2_cubiertos en null.
- ambientes: si sólo figuran dormitorios, ambientes = dormitorios + 1. Monoambiente = 1.
- antiguedad_anios: 0 si dice "a estrenar". null si no figura.
- expensas y moneda_expensas si figuran.
- dias_publicado: "hoy"=0, "ayer"=1, "hace N días"=N, "hace N semanas"=N*7, "más de un mes"=31. null si no figura.
- reciclado: true si el texto dice reciclado / a nuevo / totalmente renovado / refaccionado a estrenar;
  false si dice a reciclar / para refaccionar / a refaccionar / a actualizar; null si no se sabe.
- imagen_url: URL de la foto principal si aparece.
- NO inventes datos. Lo que no figure va en null.
- Ignorá emprendimientos en pozo, banners publicitarios y avisos de alquiler."""

    def __init__(self, api_key: str, modelo: str = cfg.GEMINI_MODEL):
        from google import genai
        from google.genai import types

        self._types = types
        self.cliente = genai.Client(api_key=api_key)
        self.modelo = modelo

    def extraer(self, portal: cfg.Portal, url: str, markdown: str) -> list[dict]:
        if not markdown or len(markdown) < 300:
            return []
        prompt = self.PROMPT.format(portal=portal.nombre, url=url, dominio=portal.dominio)
        contenido = markdown[: self.MAX_CARACTERES]

        def _llamar() -> list[dict]:
            resp = self.cliente.models.generate_content(
                model=self.modelo,
                contents=[prompt, contenido],
                config=self._types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=list[AvisoExtraido],
                    temperature=0,
                ),
            )
            avisos = resp.parsed or []
            return [a.model_dump() if isinstance(a, BaseModel) else dict(a) for a in avisos]

        return con_reintentos(_llamar, etiqueta=f"Gemini extracción {portal.nombre}")


# =========================================================================== #
# Normalización de items de actores dedicados
# =========================================================================== #
_CLAVES_ACTOR = {
    "url": ["url", "link", "permalink", "postingUrl", "detailUrl", "propertyUrl"],
    "titulo": ["title", "titulo", "name", "headline"],
    "direccion": ["address", "direccion", "location.address", "streetAddress", "postingLocation.address.name"],
    "barrio": ["neighborhood", "barrio", "location.neighborhood", "location.name", "zone"],
    "precio": ["price", "precio", "priceAmount", "price.amount", "priceOperationTypes.0.prices.0.amount"],
    "moneda": ["currency", "moneda", "price.currency", "currency_id", "priceOperationTypes.0.prices.0.currency"],
    "m2_cubiertos": ["coveredArea", "m2_cubiertos", "coveredSurface", "superficie_cubierta", "covered_area", "surfaceCovered"],
    "m2_totales": ["totalArea", "m2_totales", "totalSurface", "superficie_total", "total_area", "surfaceTotal"],
    "ambientes": ["rooms", "ambientes", "environments", "totalRooms"],
    "antiguedad_anios": ["age", "antiguedad", "antiquity", "propertyAge"],
    "expensas": ["expenses", "expensas", "maintenanceFee", "expenses.amount"],
    "moneda_expensas": ["expensesCurrency", "expenses.currency"],
    "dias_publicado": ["daysPublished", "dias_publicado", "publishedDaysAgo"],
    "fecha_publicacion": ["publishedAt", "publicationDate", "date_created", "createdAt", "fecha_publicacion"],
    "imagen_url": ["image", "thumbnail", "imageUrl", "mainImage", "pictures.0.url", "images.0"],
    "descripcion": ["description", "descripcion"],
}


def _obtener(item: Any, ruta: str) -> Any:
    actual = item
    for parte in ruta.split("."):
        if isinstance(actual, dict):
            actual = actual.get(parte)
        elif isinstance(actual, list) and parte.isdigit():
            idx = int(parte)
            actual = actual[idx] if idx < len(actual) else None
        else:
            return None
        if actual is None:
            return None
    return actual


def normalizar_item_actor(item: dict) -> dict:
    datos: dict[str, Any] = {}
    for destino, rutas in _CLAVES_ACTOR.items():
        for ruta in rutas:
            v = _obtener(item, ruta)
            if v not in (None, "", []):
                datos[destino] = v
                break
    # Días publicados a partir de una fecha
    if datos.get("dias_publicado") is None and datos.get("fecha_publicacion"):
        try:
            f = datetime.fromisoformat(str(datos["fecha_publicacion"]).replace("Z", "+00:00"))
            if f.tzinfo is None:
                f = f.replace(tzinfo=timezone.utc)
            datos["dias_publicado"] = max(0, (datetime.now(timezone.utc) - f).days)
        except ValueError:
            pass
    # Reciclado a partir del texto
    texto = normalizar_texto(f"{datos.get('titulo', '')} {datos.get('descripcion', '')}")
    if re.search(r"\b(a reciclar|para reciclar|a refaccionar|para refaccionar|a actualizar)\b", texto):
        datos["reciclado"] = False
    elif re.search(r"\b(reciclad[oa]|a nuevo|totalmente renovad[oa]|refaccionad[oa])\b", texto):
        datos["reciclado"] = True
    if isinstance(datos.get("imagen_url"), dict):
        datos["imagen_url"] = datos["imagen_url"].get("url")
    return datos


# =========================================================================== #
# Filtros, huella y duplicados
# =========================================================================== #
def evaluar_filtros(a: Aviso, f: cfg.Filtros = cfg.FILTROS) -> list[str]:
    """Devuelve la lista de motivos de rechazo (vacía = pasa)."""
    motivos = []
    if a.precio_usd > f.precio_max_usd:
        motivos.append("precio > máximo")
    if a.precio_usd < f.precio_min_usd:
        motivos.append("precio sospechosamente bajo")
    if a.ambientes is None or a.ambientes < f.ambientes_min:
        motivos.append("ambientes insuficientes o no informados")
    if a.m2_cubiertos <= f.m2_cubiertos_min:
        motivos.append("m² cubiertos ≤ mínimo")
    if a.antiguedad is None:
        if not f.aceptar_antiguedad_desconocida:
            motivos.append("antigüedad no informada")
    elif a.antiguedad <= f.antiguedad_min_anios:
        motivos.append("antigüedad ≤ 20 años")
    if a.dias_publicado is None:
        if not f.aceptar_fecha_desconocida:
            motivos.append("fecha de publicación no informada")
    elif a.dias_publicado > f.dias_publicacion_max:
        motivos.append("publicado hace más de 3 días")
    return motivos


_PALABRAS_RUIDO = r"\b(av|avda|avenida|calle|al|piso|depto|dpto|departamento|entre|y|esq|esquina|n|nro|numero)\b"


def calcular_huella(a: Aviso) -> str:
    m2_bucket = int(round(a.m2_cubiertos / 3.0) * 3)  # tolerancia ±1,5 m²
    amb = a.ambientes or 0
    direccion = re.sub(_PALABRAS_RUIDO, " ", normalizar_texto(a.direccion))
    m = re.search(r"([a-z][a-z ]{2,}?)\s+(\d{2,5})\b", direccion)
    if m:
        base = f"dir|{m.group(1).strip()}|{m.group(2)}|{amb}|{m2_bucket}"
    else:
        base = f"bar|{cfg.slugify(a.barrio)}|{amb}|{m2_bucket}|{int(round(a.precio_usd / 2000.0))}"
    return hashlib.sha1(base.encode()).hexdigest()


def deduplicar(avisos: list[Aviso], huellas_existentes: set[str], urls_existentes: set[str]) -> list[Aviso]:
    """Une el mismo inmueble publicado en varios portales y quita lo ya visto."""
    grupos: dict[str, list[Aviso]] = {}
    for a in avisos:
        a.huella = calcular_huella(a)
        grupos.setdefault(a.huella, []).append(a)

    unicos: list[Aviso] = []
    urls_vistas = set(urls_existentes)
    for huella, grupo in grupos.items():
        grupo.sort(key=lambda x: (x.completitud(), -x.precio_usd), reverse=True)
        principal = grupo[0]
        principal.urls_alternativas = sorted({g.url for g in grupo[1:] if g.url != principal.url})
        urls = {principal.url, *principal.urls_alternativas}
        if huella in huellas_existentes or urls & urls_vistas:
            continue
        urls_vistas.update(urls)
        # Si alguna publicación duplicada informa un dato que falta, se completa.
        for otro in grupo[1:]:
            for campo in ("direccion", "antiguedad", "expensas_usd", "dias_publicado", "imagen_url", "m2_totales"):
                if getattr(principal, campo) in (None, "") and getattr(otro, campo) not in (None, ""):
                    setattr(principal, campo, getattr(otro, campo))
            if otro.precio_usd < principal.precio_usd:
                principal.precio_usd = otro.precio_usd  # nos quedamos con el precio más bajo publicado
        unicos.append(principal)
    return unicos


# =========================================================================== #
# Recolector
# =========================================================================== #
@dataclass
class EstadisticasRecoleccion:
    por_portal: dict[str, dict] = field(default_factory=dict)
    crudos: int = 0
    pasan_filtros: int = 0
    nuevos: int = 0
    errores: list[str] = field(default_factory=list)


class RecolectorPortales:
    def __init__(
        self,
        db: SupabaseREST,
        apify: ClienteApify,
        extractor: ExtractorAvisosIA,
        tipo_cambio: Optional[float] = None,
        portales: tuple[cfg.Portal, ...] = cfg.PORTALES,
        barrios: tuple[cfg.Barrio, ...] = cfg.BARRIOS,
    ):
        self.db = db
        self.apify = apify
        self.extractor = extractor
        self.tipo_cambio = tipo_cambio or obtener_tipo_cambio()
        self.portales = tuple(p for p in portales if p.activo)
        self.barrios = barrios
        self.stats = EstadisticasRecoleccion()
        self._comparables: dict[str, list[Comparable]] = {}

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    def recolectar(self) -> list[Aviso]:
        """Scrapea, filtra y deduplica. Devuelve sólo avisos nuevos que pasan los filtros."""
        log.info("Tipo de cambio usado: ARS %.0f / USD", self.tipo_cambio)
        crudos = self._scrapear_todos("plantilla_busqueda", self.barrios)
        self.stats.crudos = len(crudos)

        filtrados = []
        for a in crudos:
            if not evaluar_filtros(a):
                filtrados.append(a)
        self.stats.pasan_filtros = len(filtrados)

        huellas, urls = self._claves_existentes()
        nuevos = deduplicar(filtrados, huellas, urls)
        self.stats.nuevos = len(nuevos)
        log.info("Avisos: %d crudos → %d pasan filtros → %d nuevos sin duplicar", len(crudos), len(filtrados), len(nuevos))
        return nuevos

    def preparar_comparables(self, nombres_barrios: set[str]) -> None:
        """Carga comparables vigentes desde Supabase y scrapea los barrios con menos de COMPARABLES_MAX."""
        if not nombres_barrios:
            return
        self._comparables = self._cargar_comparables(nombres_barrios)
        faltantes = [
            b for b in self.barrios
            if b.nombre in nombres_barrios and len(self._comparables.get(b.nombre, [])) < cfg.COMPARABLES_MAX
        ]
        if faltantes:
            log.info("Actualizando comparables reciclados para: %s", ", ".join(b.nombre for b in faltantes))
            nuevos = self._scrapear_todos("plantilla_comparables", tuple(faltantes))
            filas = []
            ahora = datetime.now(timezone.utc).isoformat()
            for a in nuevos:
                lo, hi = cfg.COMPARABLES_USD_M2_RANGO
                if a.reciclado is True and a.m2_cubiertos >= 30 and lo <= a.usd_m2 <= hi:
                    filas.append({
                        "url": a.url, "portal": a.portal, "barrio": a.barrio,
                        "precio_usd": round(a.precio_usd, 2), "m2_cubiertos": a.m2_cubiertos,
                        "usd_m2": round(a.usd_m2, 2), "ambientes": a.ambientes, "titulo": a.titulo[:300],
                        "capturado_en": ahora,
                    })
            filas = list({f["url"]: f for f in filas}.values())  # sin URLs repetidas en el mismo upsert
            if filas:
                self.db.insert(cfg.TABLA_COMPARABLES, filas, on_conflict="url", actualizar_si_existe=True)
            self._comparables = self._cargar_comparables(nombres_barrios)

    def comparables_para(self, aviso: Aviso) -> list[Comparable]:
        """Hasta 5 comparables reciclados del mismo barrio con m² y ambientes similares."""
        tol = cfg.COMPARABLES_TOLERANCIA_M2
        urls_propias = {aviso.url, *aviso.urls_alternativas}
        pool = [
            c for c in self._comparables.get(aviso.barrio, [])
            if c.url not in urls_propias
            and abs(c.m2_cubiertos - aviso.m2_cubiertos) <= aviso.m2_cubiertos * tol
            and (c.ambientes is None or aviso.ambientes is None or abs(c.ambientes - aviso.ambientes) <= 1)
        ]
        pool.sort(key=lambda c: abs(c.m2_cubiertos - aviso.m2_cubiertos))
        candidatos = pool[: cfg.COMPARABLES_MAX + 3]
        # Descarta outliers (±35 % de la mediana) antes de quedarnos con los 5 más parecidos.
        if len(candidatos) >= 3:
            med = statistics.median(c.usd_m2 for c in candidatos)
            candidatos = [c for c in candidatos if abs(c.usd_m2 - med) <= med * 0.35]
        return candidatos[: cfg.COMPARABLES_MAX]

    # ------------------------------------------------------------------ #
    # Scraping
    # ------------------------------------------------------------------ #
    def _scrapear_todos(self, atributo_plantilla: str, barrios: tuple[cfg.Barrio, ...]) -> list[Aviso]:
        avisos: list[Aviso] = []
        for portal in self.portales:
            plantilla = getattr(portal, atributo_plantilla)
            if not plantilla:
                continue
            urls = {plantilla.format(**b.variables()): b for b in barrios}
            try:
                if portal.actor_dedicado:
                    lote = self._via_actor(portal, urls)
                else:
                    lote = self._via_crawler(portal, urls)
                avisos.extend(lote)
                self.stats.por_portal.setdefault(portal.nombre, {})[atributo_plantilla] = len(lote)
                log.info("%s (%s): %d avisos", portal.nombre, atributo_plantilla, len(lote))
            except Exception as exc:  # noqa: BLE001 — un portal caído no frena el resto
                msg = f"{portal.nombre}: {exc}"
                log.exception("Error en %s", portal.nombre)
                self.stats.errores.append(msg)
        return avisos

    def _via_actor(self, portal: cfg.Portal, urls: dict[str, cfg.Barrio]) -> list[Aviso]:
        entrada = {"startUrls": [{"url": u} for u in urls], "maxItems": 60 * len(urls)}
        items = self.apify.correr_actor(portal.actor_dedicado, entrada)
        avisos = []
        for item in items:
            datos = normalizar_item_actor(item)
            barrio = buscar_barrio(datos.get("barrio"), datos.get("direccion"), item.get("searchUrl"), datos.get("url"))
            aviso = self._a_aviso(datos, portal, barrio)
            if aviso:
                avisos.append(aviso)
        return avisos

    def _via_crawler(self, portal: cfg.Portal, urls: dict[str, cfg.Barrio]) -> list[Aviso]:
        paginas = self.apify.crawlear(list(urls))
        log.info("%s: %d/%d páginas descargadas", portal.nombre, len(paginas), len(urls))

        def procesar(pagina: dict) -> list[Aviso]:
            url_cargada = pagina.get("url") or _obtener(pagina, "crawl.loadedUrl") or ""
            barrio_pagina = urls.get(url_cargada) or buscar_barrio(url_cargada)
            markdown = pagina.get("markdown") or pagina.get("text") or ""
            try:
                extraidos = self.extractor.extraer(portal, url_cargada, markdown)
            except Exception as exc:  # noqa: BLE001
                self.stats.errores.append(f"{portal.nombre} extracción {url_cargada}: {exc}")
                return []
            resultado = []
            for datos in extraidos:
                barrio = buscar_barrio(datos.get("barrio")) or barrio_pagina
                aviso = self._a_aviso(datos, portal, barrio)
                if aviso:
                    resultado.append(aviso)
            return resultado

        avisos: list[Aviso] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for lote in pool.map(procesar, paginas):
                avisos.extend(lote)
        return avisos

    def _a_aviso(self, datos: dict, portal: cfg.Portal, barrio: Optional[cfg.Barrio]) -> Optional[Aviso]:
        if barrio is None:
            return None
        url = (datos.get("url") or "").strip()
        if not url:
            return None
        url = urljoin(portal.dominio + "/", url)
        if urlsplit(url).scheme not in ("http", "https"):
            return None

        precio = a_numero(datos.get("precio"))
        moneda = str(datos.get("moneda") or "USD").upper()
        if not precio or ("USD" not in moneda and "U$S" not in moneda and "DOLAR" not in moneda):
            return None  # sólo avisos en dólares

        m2_cub = a_numero(datos.get("m2_cubiertos"))
        m2_tot = a_numero(datos.get("m2_totales"))
        m2 = m2_cub or m2_tot
        if not m2 or m2 < 15 or m2 > 1000:
            return None

        expensas = a_numero(datos.get("expensas"))
        if expensas:
            moneda_exp = str(datos.get("moneda_expensas") or "ARS").upper()
            if "USD" not in moneda_exp:
                expensas = expensas / self.tipo_cambio
            expensas = round(expensas, 2) if 0 < expensas < 2000 else None

        reciclado = datos.get("reciclado")
        return Aviso(
            portal=portal.nombre,
            url=url,
            barrio=barrio.nombre,
            zona=barrio.zona,
            precio_usd=precio,
            m2_cubiertos=m2,
            m2_totales=m2_tot,
            ambientes=a_entero(datos.get("ambientes")),
            titulo=str(datos.get("titulo") or "")[:300],
            direccion=str(datos.get("direccion") or "")[:200],
            antiguedad=a_entero(datos.get("antiguedad_anios")),
            expensas_usd=expensas,
            dias_publicado=a_entero(datos.get("dias_publicado")),
            imagen_url=datos.get("imagen_url") if str(datos.get("imagen_url") or "").startswith("http") else None,
            reciclado=reciclado if isinstance(reciclado, bool) else None,
        )

    # ------------------------------------------------------------------ #
    # Supabase
    # ------------------------------------------------------------------ #
    def _claves_existentes(self) -> tuple[set[str], set[str]]:
        filas = self.db.select(cfg.TABLA_PROPIEDADES, columnas="huella,url,urls_alternativas")
        huellas = {f["huella"] for f in filas if f.get("huella")}
        urls: set[str] = set()
        for f in filas:
            if f.get("url"):
                urls.add(f["url"])
            urls.update(f.get("urls_alternativas") or [])
        return huellas, urls

    def _cargar_comparables(self, nombres_barrios: set[str]) -> dict[str, list[Comparable]]:
        desde = (datetime.now(timezone.utc) - timedelta(days=cfg.COMPARABLES_VIGENCIA_DIAS)).isoformat()
        filas = self.db.select(
            cfg.TABLA_COMPARABLES,
            filtros={"barrio": filtro_in(nombres_barrios), "capturado_en": f"gte.{desde}"},
        )
        por_barrio: dict[str, list[Comparable]] = {}
        for f in filas:
            por_barrio.setdefault(f["barrio"], []).append(
                Comparable(
                    url=f["url"], portal=f.get("portal") or "", barrio=f["barrio"],
                    precio_usd=float(f["precio_usd"]), m2_cubiertos=float(f["m2_cubiertos"]),
                    usd_m2=float(f["usd_m2"]), ambientes=f.get("ambientes"), titulo=f.get("titulo") or "",
                )
            )
        return por_barrio
