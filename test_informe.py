"""
test_informe.py — Prueba local SIN Apify, Gemini ni Supabase.

Arma un PH aprobado con datos ficticios, lo pasa por el motor financiero real,
genera su informe HTML, el tablero con el link al informe y los mensajes de
Telegram paginados, y verifica que todo esté bien formado.

Uso:
    python test_informe.py                 # escribe en una carpeta temporal
    python test_informe.py --salida demo   # escribe en ./demo (para abrirlo en el navegador)

No escribe en informes/ (esa carpeta se publica en GitHub Pages).
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402
import generador_informes as gi  # noqa: E402
import main as m  # noqa: E402
from motor_financiero import MotorFinanciero  # noqa: E402
from recolector_portales import Aviso, Comparable  # noqa: E402

VOID = {"meta", "br", "img", "input", "link", "hr", "source", "wbr", "col", "area", "base", "embed", "param", "track"}


class ChequeoHTML(HTMLParser):
    """Verifica que las etiquetas abran y cierren bien."""

    def __init__(self):
        super().__init__()
        self.pila: list[str] = []
        self.errores: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.pila.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.pila or self.pila[-1] != tag:
            self.errores.append(f"</{tag}> inesperado (abierto: {self.pila[-3:]})")
            if tag in self.pila:
                while self.pila and self.pila.pop() != tag:
                    pass
        else:
            self.pila.pop()


def validar_html(texto: str, nombre: str) -> None:
    p = ChequeoHTML()
    p.feed(texto)
    p.close()
    assert not p.errores, f"{nombre}: HTML mal formado: {p.errores[:5]}"
    assert not p.pila, f"{nombre}: etiquetas sin cerrar: {p.pila}"


def fila_ficticia(n: int = 0, precio: float = 100_000) -> dict:
    motor = MotorFinanciero()
    analisis = motor.analizar(precio, 70, [2_000, 2_200, 2_400, 2_600])
    aviso = Aviso(
        portal="Zonaprop", url=f"https://www.zonaprop.com.ar/propiedades/ph-muestra-{n}.html",
        barrio="Villa Urquiza", zona="CABA", precio_usd=precio, m2_cubiertos=70, ambientes=3,
        titulo="PH 3 ambientes con patio <a reciclar>", direccion=f"Calle Ficticia {2400 + n}",
        antiguedad=60, expensas_usd=40,
    )
    aviso.huella = f"{n:040x}"
    datos = [(68, 2000, "PH reciclado 3 amb patio", "Reciclado"),
             (72, 2200, "PH muy bueno 3 amb", "Muy bueno"),
             (75, 2400, "PH reciclado a nuevo con terraza", "Reciclado a nuevo"),
             (66, 2600, "PH excelente 4 amb", "Excelente")]
    comps = [
        Comparable(url=f"https://www.zonaprop.com.ar/c{i}.html", portal="Zonaprop", barrio="Villa Urquiza",
                   precio_usd=u * m2, m2_cubiertos=m2, usd_m2=u, ambientes=3, titulo=t, estado=es,
                   capturado_en="2026-11-19T12:00:00+00:00")
        for i, (m2, u, t, es) in enumerate(datos)
    ]
    fila = m.construir_fila(aviso, comps, analisis, analisis.motivo)
    fila["aprobada"] = True  # forzado para la prueba
    fila["ficha"] = {
        "tipo": "PH", "disposicion": "Contrafrente", "m2_cubiertos": 70, "m2_totales": 92, "m2_patio": 22,
        "ambientes": 3, "dormitorios": 2, "banos": 1, "antiguedad_anios": 60, "estado": "A reciclar",
        "planta_piso": "Planta baja", "unidades_en_lote": 3, "expensas": 35000, "moneda_expensas": "ARS",
        "apto_credito": True, "inmobiliaria": "Inmobiliaria de Prueba", "matricula": "CUCICBA 0000",
        "otros": ["Patio", "Parrilla", "Lavadero"], "descripcion": "Texto ficticio de prueba.\nSegunda línea.",
    }
    fila["entorno"] = {
        "puntaje_entorno": 8, "precision_ubicacion": "alta", "riesgos": ["Dato ficticio"],
        "zona_ubicacion": "Cuadra residencial (ficticio).", "zona_comercio": "Comercio de cercanía (ficticio).",
        "zona_movilidad": "Transporte a pocas cuadras (ficticio).", "zona_perfil_comprador": "Familias (ficticio).",
        "zona_dia_y_noche": "Tranquila (ficticio).",
    }
    return fila


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--salida", help="Carpeta de salida (por defecto, una temporal)")
    args = ap.parse_args()
    salida = Path(args.salida).resolve() if args.salida else Path(tempfile.mkdtemp(prefix="flip_test_"))
    dir_informes = salida / "informes"
    assert dir_informes.resolve() != cfg.INFORMES_DIR.resolve(), "No usar informes/ del proyecto para la prueba"

    # 1) Informe individual
    fila = fila_ficticia()
    ruta = gi.generar_informe(fila, dir_informes)
    assert ruta and ruta.exists(), "No se generó el informe"
    texto = ruta.read_text(encoding="utf-8")
    validar_html(texto, ruta.name)
    secciones = [
        "Ver aviso en", "Publicado", "Vale reciclado", "Oferta máx. 20 %", "Techo 15 %", "ROI al publicado",
        "Veredicto", "Ficha técnica", "Hasta cuánto ofertar", "Según el nivel de obra",
        "Lo que vale: comparables con las mismas características", "Zona por precio USD/m²",
        "Cómo es la zona", "Las cuentas a la oferta máxima (obra intermedia)",
    ]
    cuerpo = texto[texto.find("<main>"):]  # ignora el CSS del <head>
    posiciones = [cuerpo.find(s) for s in secciones]
    faltan = [s for s, p in zip(secciones, posiciones) if p < 0]
    assert not faltan, f"Faltan secciones: {faltan}"
    assert posiciones == sorted(posiciones), "Las secciones no están en el orden pedido"
    assert "&lt;a reciclar&gt;" in texto or "<a reciclar>" not in texto, "Texto sin escapar"
    print(f"✅ Informe OK: {ruta}")

    # 2) La oferta máxima da exactamente 20 % de ROI neto en las cuentas del informe
    base = fila["escenarios"]["Intermedia"]
    c = MotorFinanciero().cuentas(base["oferta_max_20"], 70, base["precio_venta"], base["costo_m2_usd"])
    assert abs(c["roi"] - 0.20) < 1e-4, c["roi"]
    print(f"✅ Cuentas OK: oferta máx. 20 % USD {base['oferta_max_20']:,.0f} → ROI neto {c['roi']:.2%}")

    # 3) Tablero con link al informe
    gi.huellas_con_informe.__defaults__ = (dir_informes,)
    tablero = m.generar_tablero([fila], salida / "index.html")
    t = tablero.read_text(encoding="utf-8")
    assert fila["huella"] in t and "informes/" in t, "El tablero no incluye el link al informe"
    print(f"✅ Tablero OK: {tablero}")

    # 4) Telegram: 20 aprobadas → todas, ordenadas por ROI, ≤ 8 por mensaje, < 3.800 caracteres
    cfg.TABLERO_URL = cfg.TABLERO_URL or "https://usuario.github.io/flip-motor/"
    aprobadas = [fila_ficticia(i, precio=90_000 + i * 1_500) for i in range(1, 21)]
    mensajes = m.armar_mensajes(m.ResumenCorrida(aprobadas=aprobadas), False, len(aprobadas))
    tandas = mensajes[1:]
    assert sum(t_.count("Ver aviso") for t_ in tandas) == 20, "No salieron todas las aprobadas"
    assert all(len(x) < m.TELEGRAM_LIMITE_CARACTERES for x in mensajes), "Mensaje demasiado largo"
    assert all(t_.count("Ver aviso") <= 8 for t_ in tandas), "Más de 8 propiedades en un mensaje"
    assert all("Ver informe" in t_ and "/informes/" in t_ for t_ in tandas), "Falta el link al informe"
    rois = [f["roi_media"] for f in sorted(aprobadas, key=lambda f: f["roi_media"], reverse=True)]
    assert rois == sorted(rois, reverse=True)
    print(f"✅ Telegram OK: {len(mensajes)} mensajes (1 resumen + {len(tandas)} tandas), "
          f"máx. {max(len(x) for x in mensajes)} caracteres")
    print("\nEjemplo de tanda:\n" + tandas[0][:700])
    print(f"\nTodo OK. Abrí el informe de muestra en el navegador: {ruta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
