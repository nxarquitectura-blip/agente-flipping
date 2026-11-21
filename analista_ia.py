"""
analista_ia.py — Análisis de entorno con Gemini 2.5 Flash + Google Search.

Para cada oportunidad aprobada por el motor financiero, Gemini investiga un
radio de 3-4 cuadras (~300-400 m) alrededor de la dirección:
    • estaciones de subte, tren, premetro y Metrobús
    • avenidas principales y corredores comerciales
    • plazas, escuelas, hospitales y puntos de interés
    • riesgos que afectan la reventa (vías, asentamientos, zonas inundables, ruido)
y devuelve un JSON con puntaje de entorno 1-10 e impacto en la reventa.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

import config as cfg

log = logging.getLogger("analista")

ENTORNO_VACIO: dict[str, Any] = {
    "subtes": [],
    "trenes": [],
    "metrobus": [],
    "avenidas": [],
    "puntos_interes": [],
    "puntos_fuertes": [],
    "riesgos": [],
    "puntaje_entorno": None,
    "impacto_reventa": "desconocido",
    "precision_ubicacion": "baja",
    "resumen": "",
    "comentario_inversion": "",
    # Bloque "Cómo es la zona" del informe individual
    "zona_ubicacion": "",
    "zona_comercio": "",
    "zona_movilidad": "",
    "zona_perfil_comprador": "",
    "zona_dia_y_noche": "",
    "fuentes": [],
}
CAMPOS_ZONA = ("zona_ubicacion", "zona_comercio", "zona_movilidad", "zona_perfil_comprador", "zona_dia_y_noche")

PROMPT = """Actuás como analista inmobiliario senior especializado en House Flipping en CABA y GBA Norte.

Propiedad a analizar:
- Dirección: {direccion}
- Barrio: {barrio} ({zona}), Argentina
- Tipo: PH usado de {ambientes} ambientes, {m2} m² cubiertos, antigüedad: {antiguedad}
- Precio publicado: USD {precio:,.0f}  |  Valor estimado reciclado: USD {valor:,.0f}
- ROI escenario obra Intermedia: {roi:.1%}

Investigá con Google Search / Google Maps el entorno en un radio de 3 a 4 cuadras (300-400 metros):
1. Estaciones de SUBTE (línea y estación) y distancia aproximada en cuadras.
2. Estaciones de TREN (línea y estación) y distancia en cuadras.
3. Paradas de METROBÚS cercanas.
4. AVENIDAS principales a menos de 4 cuadras (y si la propiedad está SOBRE una avenida, indicarlo: suma ruido).
5. Puntos de interés que valorizan: plazas, colegios, universidades, hospitales, polos gastronómicos/comerciales.
6. Riesgos que restan valor en la reventa: vías del tren a < 2 cuadras, villas/asentamientos, zonas inundables, autopistas, boliches, inseguridad marcada.

Si la dirección no tiene altura exacta, analizá el centro del barrio e indicá precision_ubicacion = "baja".
No inventes estaciones: si no estás seguro, no las incluyas.

Respondé ÚNICAMENTE con un objeto JSON válido (sin ```), con esta estructura exacta:
{{
  "subtes": [{{"linea": "D", "estacion": "Juramento", "cuadras": 3}}],
  "trenes": [{{"linea": "Mitre", "estacion": "Belgrano C", "cuadras": 4}}],
  "metrobus": [{{"corredor": "Cabildo", "parada": "Juramento", "cuadras": 2}}],
  "avenidas": [{{"nombre": "Av. Cabildo", "cuadras": 1}}],
  "puntos_interes": ["Barrancas de Belgrano (3 cuadras)"],
  "puntos_fuertes": ["..."],
  "riesgos": ["..."],
  "puntaje_entorno": 8,
  "impacto_reventa": "positivo | neutro | negativo",
  "precision_ubicacion": "alta | media | baja",
  "resumen": "2-3 oraciones sobre conectividad y atractivo para un comprador final",
  "comentario_inversion": "1-2 oraciones: ¿el entorno justifica el valor de reventa estimado?",
  "zona_ubicacion": "2-3 oraciones: dónde queda exactamente dentro del barrio, entre qué calles/referencias, tipo de cuadra (residencial, PH, comercial)",
  "zona_comercio": "2-3 oraciones: comercio de cercanía, supermercados, polos gastronómicos o ejes comerciales",
  "zona_movilidad": "2-3 oraciones: subte, tren, colectivos, Metrobús y avenidas, con distancias en cuadras",
  "zona_perfil_comprador": "2-3 oraciones: quién compra un PH reciclado acá (familias, parejas jóvenes, etc.) y qué valora",
  "zona_dia_y_noche": "2-3 oraciones: cómo es la cuadra de día y de noche (movimiento, ruido, iluminación, seguridad percibida)"
}}"""


class AnalistaEntorno:
    def __init__(self, api_key: str = cfg.GEMINI_API_KEY, modelo: str = cfg.GEMINI_MODEL):
        if not api_key:
            raise RuntimeError("Falta GEMINI_API_KEY")
        from google import genai
        from google.genai import types

        self._types = types
        self.cliente = genai.Client(api_key=api_key)
        self.modelo = modelo

    # ------------------------------------------------------------------ #
    def analizar(self, propiedad: dict) -> dict:
        """`propiedad` es la fila (dict) que se guarda en Supabase."""
        prompt = PROMPT.format(
            direccion=propiedad.get("direccion") or f"(sin dirección exacta) {propiedad.get('barrio')}",
            barrio=propiedad.get("barrio", ""),
            zona=propiedad.get("zona", ""),
            ambientes=propiedad.get("ambientes") or "?",
            m2=propiedad.get("m2_cubiertos") or "?",
            antiguedad=f"{propiedad['antiguedad']} años" if propiedad.get("antiguedad") is not None else "no informada",
            precio=float(propiedad.get("precio_usd") or 0),
            valor=float(propiedad.get("valor_venta_estimado") or 0),
            roi=float(propiedad.get("roi_media") or 0),
        )
        herramientas = [self._types.Tool(google_search=self._types.GoogleSearch())]
        ultimo_error: Optional[Exception] = None

        for intento in range(3):
            try:
                resp = self.cliente.models.generate_content(
                    model=self.modelo,
                    contents=prompt,
                    config=self._types.GenerateContentConfig(tools=herramientas, temperature=0.2),
                )
                datos = self._parsear_json(resp.text or "")
                datos["fuentes"] = self._fuentes(resp)
                return self._sanear(datos)
            except Exception as exc:  # noqa: BLE001
                ultimo_error = exc
                log.warning("Gemini entorno falló (intento %d/3): %s", intento + 1, exc)
                time.sleep(4 * (2 ** intento))

        log.error("No se pudo analizar el entorno de %s: %s", propiedad.get("url"), ultimo_error)
        return {**ENTORNO_VACIO, "resumen": f"Análisis de entorno no disponible ({ultimo_error})."}

    def analizar_lote(self, propiedades: list[dict], pausa_seg: float = 1.0) -> list[dict]:
        resultados = []
        for p in propiedades:
            resultados.append(self.analizar(p))
            time.sleep(pausa_seg)  # respeta los límites de la API
        return resultados

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parsear_json(texto: str) -> dict:
        texto = re.sub(r"^```(?:json)?|```$", "", texto.strip(), flags=re.MULTILINE).strip()
        try:
            return json.loads(texto)
        except json.JSONDecodeError:
            inicio, fin = texto.find("{"), texto.rfind("}")
            if inicio == -1 or fin <= inicio:
                raise ValueError(f"Gemini no devolvió JSON: {texto[:200]}")
            return json.loads(texto[inicio : fin + 1])

    @staticmethod
    def _fuentes(resp: Any) -> list[dict]:
        fuentes = []
        try:
            meta = resp.candidates[0].grounding_metadata
            for chunk in (meta.grounding_chunks or [])[:8]:
                if chunk.web:
                    fuentes.append({"titulo": chunk.web.title, "url": chunk.web.uri})
        except (AttributeError, IndexError, TypeError):
            pass
        return fuentes

    @staticmethod
    def _sanear(datos: dict) -> dict:
        limpio = {**ENTORNO_VACIO, **{k: v for k, v in datos.items() if k in ENTORNO_VACIO}}
        for clave in ("subtes", "trenes", "metrobus", "avenidas", "puntos_interes", "puntos_fuertes", "riesgos", "fuentes"):
            if not isinstance(limpio[clave], list):
                limpio[clave] = []
        try:
            puntaje = int(limpio["puntaje_entorno"])
            limpio["puntaje_entorno"] = max(1, min(10, puntaje))
        except (TypeError, ValueError):
            limpio["puntaje_entorno"] = None
        if limpio["impacto_reventa"] not in ("positivo", "neutro", "negativo"):
            limpio["impacto_reventa"] = "desconocido"
        for clave in CAMPOS_ZONA:
            limpio[clave] = str(limpio[clave] or "").strip()[:1200]
        return limpio


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo = {
        "direccion": "Av. Cabildo 2000", "barrio": "Belgrano", "zona": "CABA", "ambientes": 3,
        "m2_cubiertos": 70, "antiguedad": 45, "precio_usd": 140000, "valor_venta_estimado": 190000, "roi_media": 0.12,
    }
    print(json.dumps(AnalistaEntorno().analizar(demo), indent=2, ensure_ascii=False))
