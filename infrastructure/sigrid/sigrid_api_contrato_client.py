# infrastructure/sigrid/sigrid_api_contrato_client.py
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from domain.models.contrato_models import ContratoEnrichmentResult

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[contrato-enrichment][sigrid-client]"


# Query tal cual la validamos con diagnose_sigrid_contrato.py.
# Orden de parámetros: [cif, codigo_obra]. pyodbc usa ``?`` posicional.
_SQL_QUERY = """\
SELECT
    con_ctr.cod         AS codigo_contrato,
    con_ctr.res         AS nombre_contrato,
    con_ctr.fec         AS fecha_alta_contrato,
    ctr.fecdoc          AS fecha_contrato,
    ctr.fecvig1         AS vigencia_desde,
    ctr.fecvig2         AS vigencia_hasta,
    ctr.tot             AS importe_total,
    ctr.entcif          AS cif_proveedor,
    ctr.entres          AS nombre_proveedor,
    con_obr.cod         AS codigo_obra,
    con_obr.res         AS nombre_obra
FROM ctr
JOIN con AS con_ctr  ON ctr.ide     = con_ctr.ide
JOIN con AS con_obr  ON ctr.obride  = con_obr.ide
JOIN prv             ON ctr.entide  = prv.ide
WHERE
    prv.cif     = ?
AND con_obr.cod = ?
"""


class SigridApiContratoClient:
    """Adaptador HTTP contra ``sigrid-api`` para la query de contratos.

    Mismo endpoint y credenciales que ``SigridApiObraClient``, solo cambia
    la query y los parámetros. Mantenerlos separados respeta SRP.
    """

    def __init__(
        self,
        *,
        base_url: str,
        function_key: str,
        database: str,
        timeout_s: float = 30.0,
        max_rows: int = 20,
    ) -> None:
        if not base_url:
            raise ValueError("SigridApiContratoClient requiere base_url")
        if not function_key:
            raise ValueError("SigridApiContratoClient requiere function_key")
        if not database:
            raise ValueError("SigridApiContratoClient requiere database")
        self._base_url = base_url.rstrip("/")
        self._function_key = function_key
        self._database = database
        self._timeout_s = float(timeout_s)
        self._max_rows = int(max_rows)
        key_hint = (
            f"len={len(function_key)} end=...{function_key[-4:]}"
            if function_key else "VACÍA"
        )
        logger.info(
            "%s Instanciado. base_url=%s database=%s max_rows=%s key=[%s]",
            _LOG_PREFIX,
            self._base_url,
            self._database,
            self._max_rows,
            key_hint,
        )

    def fetch_contratos(
        self,
        *,
        cif_proveedor: str,
        codigo_obra_normalizado: str,
    ) -> list[ContratoEnrichmentResult]:
        url = f"{self._base_url}/api/sql/read"
        payload = {
            "database": self._database,
            "sql": _SQL_QUERY,
            "parameters": [cif_proveedor, codigo_obra_normalizado],
            "timeout_seconds": int(self._timeout_s),
            "max_rows": self._max_rows,
        }
        headers = {
            "x-functions-key": self._function_key,
            "Content-Type": "application/json",
        }

        logger.info(
            "%s REQUEST -> POST %s cif=%s obra=%s",
            _LOG_PREFIX,
            url,
            cif_proveedor,
            codigo_obra_normalizado,
        )

        transport = httpx.HTTPTransport(retries=1)
        try:
            with httpx.Client(timeout=self._timeout_s, transport=transport) as client:
                response = client.post(url, json=payload, headers=headers)
        except Exception as exc:
            logger.exception(
                "%s FALLO de transporte. exc=%r",
                _LOG_PREFIX,
                exc,
            )
            raise

        status = response.status_code
        body_text = response.text or ""
        logger.info(
            "%s RESPONSE <- status=%s body_len=%s preview=%s",
            _LOG_PREFIX,
            status,
            len(body_text),
            body_text[:400],
        )

        if status >= 400:
            raise RuntimeError(f"sigrid-api respondió {status}: {body_text[:500]}")

        try:
            body: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"sigrid-api respuesta no JSON: {body_text[:500]}") from exc

        if not body.get("ok", False):
            raise RuntimeError(f"sigrid-api devolvió ok=false: {body!r}")

        columns: list[str] = list(body.get("columns") or [])
        rows: list[list[Any]] = list(body.get("rows") or [])
        logger.info(
            "%s Parseado: columns=%s row_count=%s",
            _LOG_PREFIX,
            columns,
            len(rows),
        )

        return [
            self._row_to_result(row=row, columns=columns)
            for row in rows
        ]

    @staticmethod
    def _row_to_result(
        *,
        row: list[Any],
        columns: list[str],
    ) -> ContratoEnrichmentResult:
        row_map = dict(zip(columns, row))
        return ContratoEnrichmentResult(
            codigo_contrato=str(row_map.get("codigo_contrato") or ""),
            nombre_contrato=_opt_str(row_map.get("nombre_contrato")),
            fecha_alta_contrato=_opt_int(row_map.get("fecha_alta_contrato")),
            fecha_contrato=_opt_int(row_map.get("fecha_contrato")),
            vigencia_desde=_opt_int(row_map.get("vigencia_desde")),
            vigencia_hasta=_opt_int(row_map.get("vigencia_hasta")),
            importe_total=_opt_float(row_map.get("importe_total")),
            cif_proveedor=_opt_str(row_map.get("cif_proveedor")),
            nombre_proveedor=_opt_str(row_map.get("nombre_proveedor")),
            codigo_obra=_opt_str(row_map.get("codigo_obra")),
            nombre_obra=_opt_str(row_map.get("nombre_obra")),
        )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
