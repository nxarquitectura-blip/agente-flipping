"""
motor_financiero.py — Motor matemático de House Flipping de PH usados (CABA / GBA Norte).

Valor de reventa (a partir de los comparables PH del mismo barrio):
    • Pesimista  percentil 25 USD/m² × m² cubiertos × 0,95
    • Base       mediana      USD/m² × m² cubiertos × 0,95
    • Optimista  percentil 75 USD/m² × m² cubiertos × 0,95
    (0,95 = −5 % de ajuste de cierre sobre el precio de publicación)

Niveles de obra (USD por m² cubierto):
    • Simple      USD 200/m²
    • Intermedia  USD 400/m²   ← ESCENARIO BASE DEL INFORME
    • Integral    USD 600/m²

Fórmulas:
    gastos de compra = precio de compra × 8,39 %   (escritura, sellos, honorarios)
    costo de obra    = m² cubiertos × USD/m² del nivel de obra
    tenencia         = USD 3.000 fijos (≈ 3 meses de obra)
    inversión total  = compra + gastos de compra + obra + tenencia
    venta neta       = valor de venta (ya con −5 %) × (1 − 5,28 %)
    ganancia bruta   = venta neta − inversión total
    ganancia neta    = ganancia bruta × 0,85 si es positiva (15 % de impuesto); si no, = bruta
    ROI              = ganancia neta / inversión total

Precio máximo de compra para un ROI neto objetivo r (> 0):
    0,85 × (VN − I) = r × I   →   I = VN / (1 + r / 0,85)
    P_max = (I − obra − tenencia) / (1 + 8,39 %)
    • oferta_max_20  → r = 20 %
    • techo_15       → r = 15 %
    • break_even     → ganancia = 0  →  I = VN
    • limite_negociacion = precio publicado × 0,85 (−15 %)

Regla de descarte automático:
    Si en el escenario Intermedia (valor Base) el techo_15 exige una rebaja MAYOR
    al 15 % sobre el precio publicado, la propiedad se descarta.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from typing import Optional, Sequence


# --------------------------------------------------------------------------- #
# Parámetros
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParametrosMercado:
    gastos_compra_pct: float = 0.0839       # escritura + sellos + honorarios
    gastos_venta_pct: float = 0.0528        # se descuenta del valor de venta estimado
    tenencia_obra_usd: float = 3_000.0      # estimado 3 meses
    meses_tenencia: int = 3                 # sólo para el ROI anualizado
    meses_comercializacion: int = 3         # sólo para el ROI anualizado
    ajuste_cierre: float = 0.95             # −5 % de cierre sobre publicación
    impuesto_ganancia_pct: float = 0.15     # sobre la ganancia bruta positiva

    # Reglas de negocio (ROI netos de impuesto)
    roi_oferta: float = 0.20                # oferta_max_20
    roi_objetivo: float = 0.15              # techo_15
    rebaja_maxima_admitida: float = 0.15    # también define limite_negociacion


@dataclass(frozen=True)
class EscenarioReforma:
    nombre: str
    costo_m2_usd: float


ESCENARIOS: tuple[EscenarioReforma, ...] = (
    EscenarioReforma("Simple", 200),
    EscenarioReforma("Intermedia", 400),
    EscenarioReforma("Integral", 600),
)
ESCENARIO_BASE = "Intermedia"
VALORES_REVENTA = ("Pesimista", "Base", "Optimista")


def cuartiles(valores: Sequence[float]) -> tuple[float, float, float]:
    """(percentil 25, mediana, percentil 75) con interpolación inclusiva."""
    v = sorted(float(x) for x in valores)
    if not v:
        raise ValueError("Se necesita al menos un valor")
    if len(v) == 1:
        return v[0], v[0], v[0]
    q1, q2, q3 = statistics.quantiles(v, n=4, method="inclusive")
    return q1, q2, q3


# --------------------------------------------------------------------------- #
# Resultados
# --------------------------------------------------------------------------- #
@dataclass
class ResultadoEscenario:
    nombre: str
    valor_reventa: str          # Pesimista / Base / Optimista
    costo_m2_usd: float
    costo_reforma: float
    gastos_compra: float
    gastos_tenencia: float
    gastos_venta: float
    inversion_total: float
    precio_venta: float         # valor de venta estimado (ya con −5 % de cierre)
    ingreso_neto: float         # venta neta de gastos
    ganancia_bruta: float
    impuesto_ganancia: float
    ganancia_neta: float        # después del 15 % de impuesto
    roi: float                  # ROI neto al precio publicado
    meses_totales: int
    roi_anualizado: float
    # Precios de compra (con su % de baja vs publicado: 0,10 = 10 % por debajo; negativo = por encima)
    oferta_max_20: float
    baja_20_pct: float
    techo_15: float
    baja_15_pct: float
    break_even: float
    limite_negociacion: float
    # Rango de ROI neto con venta Pesimista / Optimista
    roi_publicado_pesimista: float
    roi_publicado_optimista: float
    roi_oferta20_pesimista: float
    roi_oferta20_optimista: float
    # Compatibilidad con columnas de Supabase / tablero: = techo_15 y su rebaja (≥ 0)
    precio_max_compra: float
    rebaja_requerida_pct: float


@dataclass
class ResultadoAnalisis:
    precio_publicado: float
    m2_cubiertos: float
    usd_m2_reciclado: float                 # mediana USD/m² de los comparables
    valor_reciclado_100: float              # valor de venta Base (mediana × m² × 0,95)
    valores_reventa: dict[str, float]       # Pesimista / Base / Optimista
    usd_m2_reventa: dict[str, float]        # p25 / mediana / p75
    escenarios: dict[str, ResultadoEscenario]          # niveles de obra, con valor Base
    sensibilidad: dict[str, ResultadoEscenario]        # obra Intermedia × 3 valores de reventa
    escenario_base: str
    aprobada: bool
    motivo: str

    @property
    def base(self) -> ResultadoEscenario:
        return self.escenarios[self.escenario_base]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["escenarios"] = {k: asdict(v) for k, v in self.escenarios.items()}
        d["sensibilidad"] = {k: asdict(v) for k, v in self.sensibilidad.items()}
        return d


# --------------------------------------------------------------------------- #
# Motor
# --------------------------------------------------------------------------- #
class MotorFinanciero:
    def __init__(
        self,
        parametros: ParametrosMercado = ParametrosMercado(),
        escenarios: tuple[EscenarioReforma, ...] = ESCENARIOS,
        escenario_base: str = ESCENARIO_BASE,
    ):
        if escenario_base not in {e.nombre for e in escenarios}:
            raise ValueError(f"El escenario base '{escenario_base}' no existe.")
        self.p = parametros
        self.escenarios = escenarios
        self.escenario_base = escenario_base

    # ------------------------------------------------------------------ #
    def cuentas(self, precio: float, m2: float, valor_venta: float, costo_m2_obra: float) -> dict:
        """Cuentas completas a un precio de compra dado (lo usa también el informe web)."""
        p = self.p
        gastos_compra = precio * p.gastos_compra_pct
        obra = m2 * costo_m2_obra
        tenencia = p.tenencia_obra_usd
        inversion = precio + gastos_compra + obra + tenencia
        gastos_venta = valor_venta * p.gastos_venta_pct
        venta_neta = valor_venta - gastos_venta
        bruta = venta_neta - inversion
        impuesto = bruta * p.impuesto_ganancia_pct if bruta > 0 else 0.0
        neta = bruta - impuesto
        return {
            "compra": precio,
            "gastos_compra": gastos_compra,
            "obra": obra,
            "tenencia": tenencia,
            "inversion_total": inversion,
            "valor_venta": valor_venta,
            "gastos_venta": gastos_venta,
            "venta_neta": venta_neta,
            "ganancia_bruta": bruta,
            "impuesto": impuesto,
            "ganancia_neta": neta,
            "roi": neta / inversion if inversion > 0 else 0.0,
        }

    def precio_para_roi(self, valor_venta: float, m2: float, costo_m2_obra: float, roi_neto: float) -> float:
        """Precio de compra máximo para lograr `roi_neto` (0 = break-even)."""
        p = self.p
        venta_neta = valor_venta * (1 - p.gastos_venta_pct)
        if roi_neto > 0:
            inversion = venta_neta / (1 + roi_neto / (1 - p.impuesto_ganancia_pct))
        else:
            inversion = venta_neta
        precio = (inversion - m2 * costo_m2_obra - p.tenencia_obra_usd) / (1 + p.gastos_compra_pct)
        return max(precio, 0.0)

    def evaluar_escenario(
        self,
        precio: float,
        m2: float,
        valores: dict[str, float],
        esc: EscenarioReforma,
        nombre_valor: str = "Base",
    ) -> ResultadoEscenario:
        """`valores` = valor de venta Pesimista / Base / Optimista (ya con −5 % de cierre)."""
        p = self.p
        valor_venta = valores[nombre_valor]
        c = self.cuentas(precio, m2, valor_venta, esc.costo_m2_usd)

        meses = p.meses_tenencia + p.meses_comercializacion
        roi = c["roi"]
        roi_anual = (1 + roi) ** (12 / meses) - 1 if roi > -1 and meses > 0 else -1.0

        oferta20 = self.precio_para_roi(valor_venta, m2, esc.costo_m2_usd, p.roi_oferta)
        techo15 = self.precio_para_roi(valor_venta, m2, esc.costo_m2_usd, p.roi_objetivo)
        break_even = self.precio_para_roi(valor_venta, m2, esc.costo_m2_usd, 0.0)
        baja = lambda tope: 1 - tope / precio  # noqa: E731
        roi_a = lambda compra, k: self.cuentas(compra, m2, valores[k], esc.costo_m2_usd)["roi"]  # noqa: E731
        rebaja15 = 1.0 if techo15 <= 0 else max(0.0, baja(techo15))

        return ResultadoEscenario(
            nombre=esc.nombre,
            valor_reventa=nombre_valor,
            costo_m2_usd=esc.costo_m2_usd,
            costo_reforma=round(c["obra"], 2),
            gastos_compra=round(c["gastos_compra"], 2),
            gastos_tenencia=round(c["tenencia"], 2),
            gastos_venta=round(c["gastos_venta"], 2),
            inversion_total=round(c["inversion_total"], 2),
            precio_venta=round(valor_venta, 2),
            ingreso_neto=round(c["venta_neta"], 2),
            ganancia_bruta=round(c["ganancia_bruta"], 2),
            impuesto_ganancia=round(c["impuesto"], 2),
            ganancia_neta=round(c["ganancia_neta"], 2),
            roi=round(roi, 4),
            meses_totales=meses,
            roi_anualizado=round(roi_anual, 4),
            oferta_max_20=round(oferta20, 2),
            baja_20_pct=round(baja(oferta20), 4),
            techo_15=round(techo15, 2),
            baja_15_pct=round(baja(techo15), 4),
            break_even=round(break_even, 2),
            limite_negociacion=round(precio * (1 - p.rebaja_maxima_admitida), 2),
            roi_publicado_pesimista=round(roi_a(precio, "Pesimista"), 4),
            roi_publicado_optimista=round(roi_a(precio, "Optimista"), 4),
            roi_oferta20_pesimista=round(roi_a(oferta20, "Pesimista"), 4) if oferta20 > 0 else 0.0,
            roi_oferta20_optimista=round(roi_a(oferta20, "Optimista"), 4) if oferta20 > 0 else 0.0,
            precio_max_compra=round(techo15, 2),
            rebaja_requerida_pct=round(rebaja15, 4),
        )

    # ------------------------------------------------------------------ #
    def analizar(
        self,
        precio_usd: float,
        m2_cubiertos: float,
        usd_m2_comparables: Sequence[float],
    ) -> ResultadoAnalisis:
        """
        precio_usd          precio publicado
        m2_cubiertos        superficie cubierta del aviso
        usd_m2_comparables  USD/m² de los comparables PH seleccionados del mismo barrio
        """
        if precio_usd <= 0 or m2_cubiertos <= 0 or not usd_m2_comparables:
            raise ValueError("precio, m² y comparables deben ser positivos / no vacíos")
        p25, p50, p75 = cuartiles(usd_m2_comparables)
        if p25 <= 0:
            raise ValueError("USD/m² de comparables deben ser positivos")

        usd_m2 = dict(zip(VALORES_REVENTA, (p25, p50, p75)))
        valores = {k: v * m2_cubiertos * self.p.ajuste_cierre for k, v in usd_m2.items()}

        resultados = {
            e.nombre: self.evaluar_escenario(precio_usd, m2_cubiertos, valores, e, "Base")
            for e in self.escenarios
        }
        esc_base = next(e for e in self.escenarios if e.nombre == self.escenario_base)
        sensibilidad = {
            k: self.evaluar_escenario(precio_usd, m2_cubiertos, valores, esc_base, k)
            for k in VALORES_REVENTA
        }

        base = resultados[self.escenario_base]
        limite = self.p.rebaja_maxima_admitida
        if base.rebaja_requerida_pct > limite:
            aprobada = False
            motivo = (
                f"Descarte automático: en obra {self.escenario_base} el techo para ROI neto "
                f"{self.p.roi_objetivo:.0%} es USD {base.techo_15:,.0f}, "
                f"{base.rebaja_requerida_pct:.1%} bajo el publicado (máx. {limite:.0%})."
            )
        elif base.rebaja_requerida_pct > 0:
            aprobada = True
            motivo = (
                f"Aprobada negociando: oferta máx. 20 % USD {base.oferta_max_20:,.0f} · "
                f"techo 15 % USD {base.techo_15:,.0f} ({base.rebaja_requerida_pct:.1%} bajo el publicado)."
            )
        else:
            aprobada = True
            motivo = f"Aprobada a precio de lista: ROI neto {self.escenario_base} {base.roi:.1%}."

        return ResultadoAnalisis(
            precio_publicado=precio_usd,
            m2_cubiertos=m2_cubiertos,
            usd_m2_reciclado=round(p50, 2),
            valor_reciclado_100=round(valores["Base"], 2),
            valores_reventa={k: round(v, 2) for k, v in valores.items()},
            usd_m2_reventa={k: round(v, 2) for k, v in usd_m2.items()},
            escenarios=resultados,
            sensibilidad=sensibilidad,
            escenario_base=self.escenario_base,
            aprobada=aprobada,
            motivo=motivo,
        )


# --------------------------------------------------------------------------- #
# Prueba rápida:  python motor_financiero.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    motor = MotorFinanciero()
    r = motor.analizar(precio_usd=120_000, m2_cubiertos=70, usd_m2_comparables=[2_300, 2_450, 2_600, 2_700, 2_900])
    print("Valores de reventa: " + " · ".join(f"{k} USD {v:,.0f}" for k, v in r.valores_reventa.items()))
    for e in [*r.escenarios.values(), *r.sensibilidad.values()]:
        print(
            f"{e.nombre:<10} {e.valor_reventa:<9} obra {e.costo_reforma:>7,.0f} | inv. {e.inversion_total:>8,.0f} | "
            f"neta {e.ganancia_neta:>8,.0f} | ROI {e.roi:6.1%} [{e.roi_publicado_pesimista:6.1%} a {e.roi_publicado_optimista:6.1%}] | "
            f"oferta20 {e.oferta_max_20:>8,.0f} ({e.baja_20_pct:5.1%}) | techo15 {e.techo_15:>8,.0f} ({e.baja_15_pct:5.1%}) | "
            f"BE {e.break_even:>8,.0f} | −15% {e.limite_negociacion:>8,.0f}"
        )
    print(("✅ " if r.aprobada else "❌ ") + r.motivo)
