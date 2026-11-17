"""
supabase_db.py — Cliente REST mínimo para Supabase (PostgREST).

Se usa REST directo (requests) en lugar del SDK para soportar tanto las claves
JWT clásicas (anon / service_role) como las nuevas (sb_publishable_ / sb_secret_).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Optional

import requests

log = logging.getLogger("supabase")


class ErrorSupabase(RuntimeError):
    pass


def headers_supabase(clave: str) -> dict:
    headers = {"apikey": clave, "Content-Type": "application/json"}
    # Las claves nuevas (sb_...) no son JWT: van sólo en "apikey".
    if not clave.startswith("sb_"):
        headers["Authorization"] = f"Bearer {clave}"
    return headers


class SupabaseREST:
    def __init__(self, url: str, clave: str, timeout: int = 30):
        if not url or not clave:
            raise ErrorSupabase("Faltan SUPABASE_URL o la clave de Supabase.")
        self.base = url.rstrip("/") + "/rest/v1"
        self.timeout = timeout
        self.sesion = requests.Session()
        self.sesion.headers.update(headers_supabase(clave))

    # ------------------------------------------------------------------ #
    def _request(
        self,
        metodo: str,
        tabla: str,
        *,
        params: Optional[dict] = None,
        json: Any = None,
        prefer: Optional[str] = None,
    ) -> Any:
        headers = {"Prefer": prefer} if prefer else {}
        url = f"{self.base}/{tabla}"
        respuesta = None
        for intento in range(4):
            try:
                respuesta = self.sesion.request(
                    metodo, url, params=params, json=json, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                log.warning("Supabase %s %s falló (%s). Reintento %d", metodo, tabla, exc, intento + 1)
                time.sleep(2 ** intento)
                continue
            if respuesta.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** intento)
                continue
            break
        if respuesta is None:
            raise ErrorSupabase(f"{metodo} {tabla}: sin respuesta de Supabase")
        if respuesta.status_code >= 400:
            raise ErrorSupabase(f"{metodo} {tabla} -> HTTP {respuesta.status_code}: {respuesta.text[:600]}")
        return respuesta.json() if respuesta.content else None

    # ------------------------------------------------------------------ #
    def select(
        self,
        tabla: str,
        columnas: str = "*",
        filtros: Optional[dict] = None,
        orden: Optional[str] = None,
        limite: Optional[int] = None,
    ) -> list[dict]:
        """SELECT paginado. `filtros` usa sintaxis PostgREST: {"estado": "eq.nueva"}."""
        params = {"select": columnas, **(filtros or {})}
        if orden:
            params["order"] = orden
        resultados: list[dict] = []
        tam_pagina = 1000
        offset = 0
        while True:
            tam = tam_pagina if limite is None else min(tam_pagina, limite - len(resultados))
            if tam <= 0:
                break
            lote = self._request("GET", tabla, params={**params, "limit": tam, "offset": offset}) or []
            resultados.extend(lote)
            if len(lote) < tam:
                break
            offset += tam
        return resultados

    def insert(
        self,
        tabla: str,
        filas: Iterable[dict],
        on_conflict: Optional[str] = None,
        actualizar_si_existe: bool = False,
        tam_lote: int = 200,
    ) -> list[dict]:
        filas = list(filas)
        if not filas:
            return []
        prefer = ["return=representation"]
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
            prefer.append("resolution=merge-duplicates" if actualizar_si_existe else "resolution=ignore-duplicates")
        insertadas: list[dict] = []
        for i in range(0, len(filas), tam_lote):
            lote = filas[i : i + tam_lote]
            res = self._request("POST", tabla, params=params, json=lote, prefer=",".join(prefer))
            insertadas.extend(res or [])
        return insertadas

    def update(self, tabla: str, filtros: dict, datos: dict) -> list[dict]:
        if not filtros:
            raise ErrorSupabase("update() sin filtros: operación bloqueada por seguridad.")
        return self._request("PATCH", tabla, params=filtros, json=datos, prefer="return=representation") or []


def filtro_in(valores: Iterable[str]) -> str:
    """Arma un filtro PostgREST `in.(...)` con comillas."""
    escapados = ['"' + str(v).replace('"', '\\"') + '"' for v in valores]
    return "in.(" + ",".join(escapados) + ")"
