# infrastructure/sigrid/sigrid_api_contrato_client.py
from __future__ import annotations

import json
import logging
from collections import OrderedDict
from typing import Any

import httpx

from domain.models.contrato_models import (
    ContratoEnrichmentResult,
    ContratoLineFromSigrid,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[contrato-enrichment][sigrid-client]"


# ==================================================================== #
# Query ampliada: cabecera + líneas de detalle en un solo resultset.
# Cambios respecto a la versión anterior:
#   - Añade JOIN a ``ctrpro`` para obtener las líneas de detalle.
#   - LEFT JOIN en vez de INNER JOIN para no perder contratos sin líneas
#     registradas en el ERP (raro pero posible al abrir el contrato).
#   - LEFT JOIN a ``pro`` y ``con AS con_pro`` para el código de
#     producto (puede no existir si la línea es manual/texto libre).
#   - ORDER BY asegura filas contiguas del mismo contrato, permitiendo
#     agrupar por ``codigo_contrato`` en streaming.
#
# Parámetros posicionales: [cif, codigo_obra_normalizado].
# ==================================================================== #
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
    con_obr.res         AS nombre_obra,
    ctrpro.pos          AS linea,
    ctrpro.numlin       AS numero_linea,
    con_pro.cod         AS codigo_producto,
    ctrpro.cod2         AS codigo_alternativo,
    ctrpro.unimed       AS unidad_medida,
    ctrpro.res          AS descripcion_linea,
    ctrpro.can          AS uds,
    ctrpro.canser       AS cantidad_servida,
    ctrpro.canfac       AS cantidad_facturada,
    (ctrpro.can - ISNULL(ctrpro.canser, 0)) AS pendiente_servir,
    ctrpro.pre          AS precio_unitario,
    ctrpro.tar          AS precio_bruto,
    ctrpro.dto          AS descuentos,
    ctrpro.tot          AS importe_linea,
    ctrpro.ivacuo       AS cuota_iva,
    ctrpro.docoricod    AS doc_origen
FROM ctr
JOIN con AS con_ctr       ON ctr.ide     = con_ctr.ide
JOIN con AS con_obr       ON ctr.obride  = con_obr.ide
JOIN prv                  ON ctr.entide  = prv.ide
LEFT JOIN ctrpro          ON ctrpro.docide = ctr.ide
LEFT JOIN pro             ON ctrpro.proide = pro.ide
LEFT JOIN con AS con_pro  ON pro.ide       = con_pro.ide
WHERE
    prv.cif     = ?
AND con_obr.cod = ?
ORDER BY con_ctr.cod, ctrpro.pos
"""


class SigridApiContratoClient:
    """Cliente HTTP que llama a la Function App ``sigrid-api``.

    La respuesta de ``/api/sql/read`` viene como JSON con ``columns`` y
    ``rows``. Este cliente:

      1. Pide cabecera + líneas en una sola query (más eficiente que
         dos llamadas: una conexión, un round-trip, un plan ejecución).
      2. Agrupa en memoria por ``codigo_contrato`` preservando el orden.
      3. Devuelve ``list[ContratoEnrichmentResult]``, cada uno con sus
         líneas dentro.

    Si una fila trae ``linea = NULL`` (porque LEFT JOIN no casó con
    ``ctrpro``), significa que el contrato existe pero no tiene líneas
    registradas. En ese caso la cabecera se devuelve con ``lines=[]``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        function_key: str,
        database: str,
        timeout_s: float = 30.0,
        max_rows: int = 1000,
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
        logger.info(
            "%s Instanciado. base_url=%s database=%s max_rows=%s key_len=%s",
            _LOG_PREFIX,
            self._base_url,
            self._database,
            self._max_rows,
            len(function_key),
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
            logger.exception("%s FALLO de transporte. exc=%r", _LOG_PREFIX, exc)
            raise

        status = response.status_code
        body_text = response.text or ""
        logger.info(
            "%s RESPONSE <- status=%s body_len=%s preview=%s",
            _LOG_PREFIX,
            status,
            len(body_text),
            body_text[:300],
        )

        if status >= 400:
            raise RuntimeError(f"sigrid-api respondió {status}: {body_text[:500]}")

        try:
            body: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"sigrid-api respuesta no JSON: {body_text[:500]}"
            ) from exc

        if not body.get("ok", False):
            raise RuntimeError(f"sigrid-api devolvió ok=false: {body!r}")

        columns: list[str] = list(body.get("columns") or [])
        rows: list[list[Any]] = list(body.get("rows") or [])
        logger.info(
            "%s Parseado: row_count=%s (incluye filas de líneas)",
            _LOG_PREFIX,
            len(rows),
        )

        results = self._group_rows_by_contrato(columns=columns, rows=rows)
        logger.info(
            "%s Agrupado: contratos=%s total_lineas=%s",
            _LOG_PREFIX,
            len(results),
            sum(len(c.lines) for c in results),
        )
        return results

    @staticmethod
    def _group_rows_by_contrato(
        *,
        columns: list[str],
        rows: list[list[Any]],
    ) -> list[ContratoEnrichmentResult]:
        """Agrupa filas (cabecera repetida + líneas) por codigo_contrato.

        Usa ``OrderedDict`` para preservar el orden de aparición de los
        contratos en el resultset (ordenado por ``con_ctr.cod`` gracias
        al ORDER BY). Dentro de cada grupo las líneas se añaden por
        orden (también ORDER BY ``ctrpro.pos``).
        """
        buckets: "OrderedDict[str, tuple[dict[str, Any], list[ContratoLineFromSigrid]]]" = (
            OrderedDict()
        )

        for row in rows:
            row_map = dict(zip(columns, row))
            codigo = _opt_str(row_map.get("codigo_contrato"))
            if codigo is None:
                # Defensivo: si Sigrid devuelve fila sin codigo_contrato
                # (no debería pasar con la query actual), saltamos.
                continue

            if codigo not in buckets:
                buckets[codigo] = (row_map, [])

            linea_value = row_map.get("linea")
            if linea_value is None:
                # Contrato sin líneas registradas (LEFT JOIN no casó):
                # se queda solo con la cabecera, lines=[].
                continue

            buckets[codigo][1].append(
                ContratoLineFromSigrid(
                    codigo_contrato=codigo,
                    linea=_opt_int(linea_value),
                    numero_linea=_opt_int(row_map.get("numero_linea")),
                    codigo_producto=_opt_str(row_map.get("codigo_producto")),
                    codigo_alternativo=_opt_str(row_map.get("codigo_alternativo")),
                    unidad_medida=_opt_str(row_map.get("unidad_medida")),
                    descripcion_linea=_opt_str(row_map.get("descripcion_linea")),
                    uds=_opt_float(row_map.get("uds")),
                    cantidad_servida=_opt_float(row_map.get("cantidad_servida")),
                    cantidad_facturada=_opt_float(row_map.get("cantidad_facturada")),
                    pendiente_servir=_opt_float(row_map.get("pendiente_servir")),
                    precio_unitario=_opt_float(row_map.get("precio_unitario")),
                    precio_bruto=_opt_float(row_map.get("precio_bruto")),
                    descuentos=_opt_float(row_map.get("descuentos")),
                    importe_linea=_opt_float(row_map.get("importe_linea")),
                    cuota_iva=_opt_float(row_map.get("cuota_iva")),
                    doc_origen=_opt_str(row_map.get("doc_origen")),
                )
            )

        results: list[ContratoEnrichmentResult] = []
        for codigo, (header_row, lines) in buckets.items():
            results.append(
                ContratoEnrichmentResult(
                    codigo_contrato=codigo,
                    nombre_contrato=_opt_str(header_row.get("nombre_contrato")),
                    fecha_alta_contrato=_opt_int(header_row.get("fecha_alta_contrato")),
                    fecha_contrato=_opt_int(header_row.get("fecha_contrato")),
                    vigencia_desde=_opt_int(header_row.get("vigencia_desde")),
                    vigencia_hasta=_opt_int(header_row.get("vigencia_hasta")),
                    importe_total=_opt_float(header_row.get("importe_total")),
                    cif_proveedor=_opt_str(header_row.get("cif_proveedor")),
                    nombre_proveedor=_opt_str(header_row.get("nombre_proveedor")),
                    codigo_obra=_opt_str(header_row.get("codigo_obra")),
                    nombre_obra=_opt_str(header_row.get("nombre_obra")),
                    lines=lines,
                )
            )
        return results


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
