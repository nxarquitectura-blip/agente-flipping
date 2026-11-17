"""
motor_financiero.py — Motor matemático de House Flipping (CABA / GBA Norte).

Para cada propiedad evalúa 3 escenarios de reforma:
    • Mínima   USD 200/m²
    • Media    USD 400/m²   ← ESCENARIO BASE
    • Integral USD 600/m²

Incluye gastos de compra, tenencia (obra + comercialización) y venta.

Regla de descarte automático:
    Si en el escenario Media hace falta negociar una rebaja MAYOR al 15 % sobre
    el precio publicado para alcanzar un ROI del 15 %, la propiedad se descarta.

Definiciones:
    inversión total = precio compra + gastos compra + reforma + tenencia
    ingreso neto    = precio de venta − gastos de venta
    ganancia neta   = ingreso neto − inversión total
    ROI             = ganancia neta / inversión total
    precio máximo de compra (ROI objetivo):
        P_max = (ingreso_neto / (1 + ROI_obj) − reforma − tenencia) / (1 + %gastos_compra)
    rebaja requerida = (precio publicado − P_max) / precio publicado
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


# --------------------------------------------------------------------------- #
# Parámetros
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParametrosMercado:
    # Compra (porcentajes sobre el precio de compra)
    comision_compra_pct: float = 0.04       # honorarios inmobiliaria comprador (tope CABA 4 %)
    escribania_compra_pct: float = 0.02     # escribano + certificados + IVA
    sellos_compra_pct: float = 0.0175       # 50 % del Impuesto de Sellos (3,5 %)
    otros_compra_pct: float = 0.005         # gestoría, transferencias, varios

    # Venta (porcentajes sobre el precio de venta)
    comision_venta_pct: float = 0.03        # honorarios inmobiliaria vendedor
    sellos_venta_pct: float = 0.0175        # 50 % del Impuesto de Sellos
    impuesto_venta_pct: float = 0.015       # ITI 1,5 %
    otros_venta_pct: float = 0.005          # certificados, planos, fotos, home staging

    # Tenencia (USD por mes)
    expensas_default_usd: float = 90.0      # si el aviso no informa expensas
    abl_servicios_mensual_usd: float = 45.0 # ABL/ARBA + luz/gas/agua mínimos
    meses_comercializacion: int = 4

    # Obra
    imprevistos_pct: float = 0.10           # colchón sobre el costo de reforma

    # Reglas de negocio
    roi_objetivo: float = 0.15
    rebaja_maxima_admitida: float = 0.15

    @property
    def gastos_compra_pct(self) -> float:
        return self.comision_compra_pct + self.escribania_compra_pct + self.sellos_compra_pct + self.otros_compra_pct

    @property
    def gastos_venta_pct(self) -> float:
        return self.comision_venta_pct + self.sellos_venta_pct + self.impuesto_venta_pct + self.otros_venta_pct


@dataclass(frozen=True)
class EscenarioReforma:
    nombre: str
    costo_m2_usd: float
    meses_obra: int
    # Qué fracción del valor "reciclado a nuevo" (comparables) se alcanza al vender.
    factor_valor_venta: float


ESCENARIOS: tuple[EscenarioReforma, ...] = (
    EscenarioReforma("Mínima", 200, 2, 0.90),
    EscenarioReforma("Media", 400, 4, 0.97),
    EscenarioReforma("Integral", 600, 6, 1.00),
)
ESCENARIO_BASE = "Media"


# --------------------------------------------------------------------------- #
# Resultados
# --------------------------------------------------------------------------- #
@dataclass
class ResultadoEscenario:
    nombre: str
    costo_m2_usd: float
    costo_reforma: float
    gastos_compra: float
    gastos_tenencia: float
    gastos_venta: float
    inversion_total: float
    precio_venta: float
    ingreso_neto: float
    ganancia_neta: float
    roi: float
    meses_totales: int
    roi_anualizado: float
    precio_max_compra: float
    rebaja_requerida_pct: float


@dataclass
class ResultadoAnalisis:
    precio_publicado: float
    m2_cubiertos: float
    usd_m2_reciclado: float
    valor_reciclado_100: float
    escenarios: dict[str, ResultadoEscenario]
    escenario_base: str
    aprobada: bool
    motivo: str

    @property
    def base(self) -> ResultadoEscenario:
        return self.escenarios[self.escenario_base]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["escenarios"] = {k: asdict(v) for k, v in self.escenarios.items()}
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
    def evaluar_escenario(
        self,
        precio: float,
        m2: float,
        valor_reciclado_100: float,
        expensas_usd: Optional[float],
        esc: EscenarioReforma,
    ) -> ResultadoEscenario:
        p = self.p
        expensas = expensas_usd if expensas_usd and expensas_usd > 0 else p.expensas_default_usd

        precio_venta = valor_reciclado_100 * esc.factor_valor_venta
        costo_reforma = m2 * esc.costo_m2_usd * (1 + p.imprevistos_pct)
        meses = esc.meses_obra + p.meses_comercializacion
        gastos_tenencia = meses * (expensas + p.abl_servicios_mensual_usd)
        gastos_compra = precio * p.gastos_compra_pct
        gastos_venta = precio_venta * p.gastos_venta_pct

        inversion = precio + gastos_compra + costo_reforma + gastos_tenencia
        ingreso_neto = precio_venta - gastos_venta
        ganancia = ingreso_neto - inversion
        roi = ganancia / inversion if inversion > 0 else 0.0
        roi_anual = (1 + roi) ** (12 / meses) - 1 if roi > -1 and meses > 0 else -1.0

        precio_max = (ingreso_neto / (1 + p.roi_objetivo) - costo_reforma - gastos_tenencia) / (1 + p.gastos_compra_pct)
        precio_max = max(precio_max, 0.0)
        if precio_max <= 0:
            rebaja = 1.0
        else:
            rebaja = max(0.0, (precio - precio_max) / precio)

        return ResultadoEscenario(
            nombre=esc.nombre,
            costo_m2_usd=esc.costo_m2_usd,
            costo_reforma=round(costo_reforma, 2),
            gastos_compra=round(gastos_compra, 2),
            gastos_tenencia=round(gastos_tenencia, 2),
            gastos_venta=round(gastos_venta, 2),
            inversion_total=round(inversion, 2),
            precio_venta=round(precio_venta, 2),
            ingreso_neto=round(ingreso_neto, 2),
            ganancia_neta=round(ganancia, 2),
            roi=round(roi, 4),
            meses_totales=meses,
            roi_anualizado=round(roi_anual, 4),
            precio_max_compra=round(precio_max, 2),
            rebaja_requerida_pct=round(rebaja, 4),
        )

    # ------------------------------------------------------------------ #
    def analizar(
        self,
        precio_usd: float,
        m2_cubiertos: float,
        usd_m2_reciclado: float,
        expensas_usd: Optional[float] = None,
    ) -> ResultadoAnalisis:
        """
        precio_usd        precio publicado
        m2_cubiertos      superficie cubierta
        usd_m2_reciclado  mediana USD/m² de comparables reciclados a nuevo
        expensas_usd      expensas mensuales en USD (opcional)
        """
        if precio_usd <= 0 or m2_cubiertos <= 0 or usd_m2_reciclado <= 0:
            raise ValueError("precio, m² y USD/m² de comparables deben ser positivos")

        valor_100 = usd_m2_reciclado * m2_cubiertos
        resultados = {
            e.nombre: self.evaluar_escenario(precio_usd, m2_cubiertos, valor_100, expensas_usd, e)
            for e in self.escenarios
        }
        base = resultados[self.escenario_base]
        limite = self.p.rebaja_maxima_admitida

        if base.rebaja_requerida_pct > limite:
            aprobada = False
            motivo = (
                f"Descarte automático: en Reforma {self.escenario_base} requiere "
                f"{base.rebaja_requerida_pct:.1%} de rebaja (máx. {limite:.0%}) para un ROI del "
                f"{self.p.roi_objetivo:.0%}. Precio máx. de compra USD {base.precio_max_compra:,.0f}."
            )
        elif base.rebaja_requerida_pct > 0:
            aprobada = True
            motivo = (
                f"Aprobada negociando {base.rebaja_requerida_pct:.1%}: ofertar hasta "
                f"USD {base.precio_max_compra:,.0f} para ROI {self.p.roi_objetivo:.0%}."
            )
        else:
            aprobada = True
            motivo = f"Aprobada a precio de lista: ROI {self.escenario_base} {base.roi:.1%}."

        return ResultadoAnalisis(
            precio_publicado=precio_usd,
            m2_cubiertos=m2_cubiertos,
            usd_m2_reciclado=round(usd_m2_reciclado, 2),
            valor_reciclado_100=round(valor_100, 2),
            escenarios=resultados,
            escenario_base=self.escenario_base,
            aprobada=aprobada,
            motivo=motivo,
        )


# --------------------------------------------------------------------------- #
# Prueba rápida:  python motor_financiero.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    motor = MotorFinanciero()
    r = motor.analizar(precio_usd=120_000, m2_cubiertos=70, usd_m2_reciclado=2_600, expensas_usd=100)
    print(f"Valor reciclado a nuevo: USD {r.valor_reciclado_100:,.0f}")
    for e in r.escenarios.values():
        print(
            f"{e.nombre:<9} reforma USD {e.costo_reforma:>9,.0f} | inversión USD {e.inversion_total:>9,.0f} | "
            f"venta USD {e.precio_venta:>9,.0f} | ganancia USD {e.ganancia_neta:>9,.0f} | ROI {e.roi:6.1%} | "
            f"precio máx USD {e.precio_max_compra:>9,.0f} | rebaja req. {e.rebaja_requerida_pct:5.1%}"
        )
    print(("✅ " if r.aprobada else "❌ ") + r.motivo)
