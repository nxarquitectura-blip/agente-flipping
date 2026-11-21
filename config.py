"""
config.py — Configuración central del Agente de House Flipping (CABA + GBA Norte).

- Lee credenciales desde variables de entorno (GitHub Actions) o desde el `.env`
  local. El `.env` puede estar en formato estándar (SUPABASE_URL=...) o en el
  formato "humano" con espacios (URL SUPABASE = ...): ambos se aceptan.
- Centraliza barrios, filtros de búsqueda y plantillas de URL de cada portal.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# Carga del .env
# --------------------------------------------------------------------------- #
_ALIAS_ENV = {
    "CLAVE PUBLICA SUPABASE": "SUPABASE_ANON_KEY",
    "CLAVE PÚBLICA SUPABASE": "SUPABASE_ANON_KEY",
    "LLAVE PUBLICA SUPABASE": "SUPABASE_ANON_KEY",
    "LLAVE SECRETA": "SUPABASE_SERVICE_KEY",
    "LLAVE SECRETA SUPABASE": "SUPABASE_SERVICE_KEY",
    "CLAVE SECRETA SUPABASE": "SUPABASE_SERVICE_KEY",
    "URL SUPABASE": "SUPABASE_URL",
    "APIFY TOKEN": "APIFY_TOKEN",
    "GOOGLE STUDIO AI": "GEMINI_API_KEY",
    "GOOGLE AI STUDIO": "GEMINI_API_KEY",
    "GEMINI API KEY": "GEMINI_API_KEY",
    "TELEGRAM TOKEN": "TELEGRAM_BOT_TOKEN",
    "TELEGRAM BOT TOKEN": "TELEGRAM_BOT_TOKEN",
    "TELEGRAM CHAT ID": "TELEGRAM_CHAT_ID",
}


def _cargar_env_local(ruta: Path = BASE_DIR / ".env") -> None:
    """Carga el .env sin pisar variables ya definidas en el entorno."""
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8-sig").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        clave = re.sub(r"\s+", " ", clave.strip()).upper()
        valor = valor.strip().strip('"').strip("'")
        nombre = _ALIAS_ENV.get(clave, clave.replace(" ", "_"))
        if valor and not os.environ.get(nombre):
            os.environ[nombre] = valor


def _normalizar_url_supabase(url: str) -> str:
    """Acepta la URL de la API o la del dashboard y devuelve https://<ref>.supabase.co"""
    url = (url or "").strip()
    m = re.search(r"/project/([a-z0-9]{20})", url)
    if m:
        return f"https://{m.group(1)}.supabase.co"
    return url.rstrip("/")


_cargar_env_local()

SUPABASE_URL = _normalizar_url_supabase(os.getenv("SUPABASE_URL", ""))
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")   # solo backend
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")         # va al tablero web
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TABLERO_URL = os.getenv("TABLERO_URL", "")  # ej: https://usuario.github.io/flip-motor/

TABLA_PROPIEDADES = os.getenv("TABLA_PROPIEDADES", "propiedades")
TABLA_COMPARABLES = os.getenv("TABLA_COMPARABLES", "comparables")

TABLERO_HTML = BASE_DIR / "index.html"
INFORMES_DIR = BASE_DIR / "informes"   # informes/<huella>.html, publicados junto al tablero


def requerir(*nombres: str) -> None:
    faltantes = [n for n in nombres if not globals().get(n)]
    if faltantes:
        raise RuntimeError(
            "Faltan credenciales: " + ", ".join(faltantes)
            + ". Revisá el .env local o los Secrets del repositorio en GitHub."
        )


# --------------------------------------------------------------------------- #
# Apify
# --------------------------------------------------------------------------- #
# Crawler genérico: descarga cada página de resultados y Gemini extrae los avisos.
APIFY_CRAWLER_ACTOR = os.getenv("APIFY_CRAWLER_ACTOR", "apify/website-content-crawler")
APIFY_PROXY_GROUP = os.getenv("APIFY_PROXY_GROUP", "RESIDENTIAL")  # "" = proxy datacenter
APIFY_TIMEOUT_SEG = int(os.getenv("APIFY_TIMEOUT_SEG", "1500"))
APIFY_MEMORIA_MB = int(os.getenv("APIFY_MEMORIA_MB", "4096"))

# Tipo de cambio de respaldo (ARS por USD) si dolarapi.com no responde.
TIPO_CAMBIO_DEFAULT = float(os.getenv("TIPO_CAMBIO_DEFAULT", "1450"))


# --------------------------------------------------------------------------- #
# Barrios objetivo (18)
# --------------------------------------------------------------------------- #
def slugify(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


@dataclass(frozen=True)
class Barrio:
    nombre: str
    zona: str                      # "CABA" o "GBA Norte"
    partido: Optional[str] = None  # solo GBA
    slug_custom: Optional[str] = None

    @property
    def slug(self) -> str:
        return self.slug_custom or slugify(self.nombre)

    @property
    def ml_path(self) -> str:
        """Ruta de ubicación de MercadoLibre Inmuebles."""
        if self.zona == "CABA":
            return f"capital-federal/{self.slug}"
        return f"bsas-gba-norte/{slugify(self.partido or self.nombre)}/{self.slug}"

    def variables(self) -> dict:
        return {"slug": self.slug, "ml_path": self.ml_path, "nombre": self.nombre}


BARRIOS: tuple[Barrio, ...] = (
    # CABA
    Barrio("Palermo", "CABA"),
    Barrio("Belgrano", "CABA"),
    Barrio("Núñez", "CABA"),
    Barrio("Colegiales", "CABA"),
    Barrio("Villa Urquiza", "CABA"),
    Barrio("Coghlan", "CABA"),
    Barrio("Saavedra", "CABA"),
    Barrio("Villa Crespo", "CABA"),
    Barrio("Caballito", "CABA"),
    Barrio("Almagro", "CABA"),
    Barrio("Villa Devoto", "CABA"),
    Barrio("Villa del Parque", "CABA"),
    # GBA Norte
    Barrio("Vicente López", "GBA Norte", partido="Vicente López"),
    Barrio("Olivos", "GBA Norte", partido="Vicente López"),
    Barrio("Florida", "GBA Norte", partido="Vicente López"),
    Barrio("Martínez", "GBA Norte", partido="San Isidro"),
    Barrio("San Isidro", "GBA Norte", partido="San Isidro"),
    Barrio("Acassuso", "GBA Norte", partido="San Isidro"),
)
BARRIOS_POR_NOMBRE = {slugify(b.nombre): b for b in BARRIOS}


# --------------------------------------------------------------------------- #
# Filtros de búsqueda
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Filtros:
    precio_max_usd: float = 150_000
    precio_min_usd: float = 35_000          # descarta cocheras/errores de carga
    ambientes_min: int = 3
    m2_cubiertos_min: float = 40
    antiguedad_min_anios: int = 20          # se exige antigüedad > 20
    dias_publicacion_max: int = 3
    aceptar_antiguedad_desconocida: bool = True   # muchos listados no la muestran
    aceptar_fecha_desconocida: bool = True        # el anti-duplicados garantiza que sea "nuevo para nosotros"


FILTROS = Filtros()

# Comparables: PH reciclados del mismo barrio
COMPARABLES_MIN = 4
COMPARABLES_MAX = 5
COMPARABLES_VIGENCIA_DIAS = 60
COMPARABLES_AMBIENTES_TOLERANCIA = 1        # ±1 ambiente como máximo
# Rango de m² cubiertos como fracción de los m² del aviso. El primero es el
# estricto; los siguientes sólo se usan si no se llega a COMPARABLES_MIN.
COMPARABLES_RANGOS_M2: tuple[tuple[float, float], ...] = (
    (0.65, 1.40),
    (0.55, 1.60),
    (0.45, 1.80),
)
COMPARABLES_USD_M2_RANGO = (900, 6_000)     # descarta USD/m² absurdos
COMPARABLES_RECORTE_OUTLIERS = 0.10         # recorta 10 % superior e inferior…
COMPARABLES_MUESTRA_PARA_RECORTE = 10       # …cuando hay al menos 10 comparables
# El −5 % de cierre y las fórmulas financieras viven en motor_financiero.ParametrosMercado.


# --------------------------------------------------------------------------- #
# Portales
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Portal:
    clave: str
    nombre: str
    dominio: str
    plantilla_busqueda: str
    plantilla_comparables: Optional[str] = None
    activo: bool = True
    # Si se define esta variable de entorno con un actor de Apify dedicado
    # (ej. un scraper específico de Zonaprop), se usa en lugar del crawler + IA.
    actor_env: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def actor_dedicado(self) -> Optional[str]:
        return os.getenv(self.actor_env) if self.actor_env else None


# Las plantillas aceptan {slug}, {ml_path} y {nombre}.
# Verificá cada URL en el navegador la primera vez: si un portal cambia su
# esquema de URLs, sólo hay que corregir la plantilla acá.
PORTALES: tuple[Portal, ...] = (
    Portal(
        clave="zonaprop",
        nombre="Zonaprop",
        dominio="https://www.zonaprop.com.ar",
        plantilla_busqueda="https://www.zonaprop.com.ar/ph-venta-{slug}-desde-3-ambientes-menos-150000-dolar-orden-publicado-descendente.html",
        plantilla_comparables="https://www.zonaprop.com.ar/ph-venta-{slug}-desde-3-ambientes-reciclado-orden-publicado-descendente.html",
        actor_env="APIFY_ACTOR_ZONAPROP",
    ),
    Portal(
        clave="argenprop",
        nombre="Argenprop",
        dominio="https://www.argenprop.com",
        plantilla_busqueda="https://www.argenprop.com/ph/venta/{slug}/3-ambientes-o-mas/dolares-hasta-150000?orden-masnuevos",
        plantilla_comparables="https://www.argenprop.com/ph/venta/{slug}/3-ambientes-o-mas?estado-excelente&orden-masnuevos",
        actor_env="APIFY_ACTOR_ARGENPROP",
    ),
    Portal(
        clave="mercadolibre",
        nombre="MercadoLibre",
        dominio="https://inmuebles.mercadolibre.com.ar",
        plantilla_busqueda="https://inmuebles.mercadolibre.com.ar/ph/venta/mas-de-3-ambientes/{ml_path}/_PriceRange_0USD-150000USD_OrderId_BEGINS*DESC_NoIndex_True",
        plantilla_comparables="https://inmuebles.mercadolibre.com.ar/ph/venta/mas-de-3-ambientes/{ml_path}/reciclado_NoIndex_True",
        actor_env="APIFY_ACTOR_MELI",
    ),
)
