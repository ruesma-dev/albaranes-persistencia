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
# Query 1: cabecera + líneas en un solo resultset.
#
# Cambios respecto a versiones anteriores:
#   - ``ctr.totbas`` como importe_total (SIN IVA, que es lo que el
#     usuario del ERP ve como total del contrato).
#   - LEFT JOIN a ``obrparpar`` para traer partida (cod + res) de cada
#     línea. Imputación por capítulos de obra.
#   - Filtro ``con_ctr.emp = 1`` para consistencia con el prototipo
#     validado.
#
# Parámetros: [cif, codigo_obra_normalizado].
# ==================================================================== #
_SQL_HEADER_AND_LINES = """\
SELECT
    ctr.ide             AS contrato_ide,
    con_ctr.cod         AS codigo_contrato,
    con_ctr.res         AS nombre_contrato,
    con_ctr.fec         AS fecha_alta_contrato,
    ctr.fecdoc          AS fecha_contrato,
    ctr.fecvig1         AS vigencia_desde,
    ctr.fecvig2         AS vigencia_hasta,
    ctr.totbas          AS importe_total,
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
    ctrpro.docoricod    AS doc_origen,
    obrparpar.cod       AS codigo_partida,
    obrparpar.res       AS descripcion_partida
FROM ctr
JOIN con AS con_ctr       ON ctr.ide     = con_ctr.ide
JOIN con AS con_obr       ON ctr.obride  = con_obr.ide
JOIN prv                  ON ctr.entide  = prv.ide
LEFT JOIN ctrpro          ON ctrpro.docide = ctr.ide
LEFT JOIN pro             ON ctrpro.proide = pro.ide
LEFT JOIN con AS con_pro  ON pro.ide       = con_pro.ide
LEFT JOIN obrparpar       ON ctrpro.paride = obrparpar.ide
WHERE
    prv.cif       = ?
AND con_obr.cod   = ?
AND con_ctr.emp   = 1
ORDER BY con_ctr.cod, ctrpro.pos
"""


# Query 2a (BBDD principal): ruesma.rcg + ruesma.gra por contrato_ide.
# Devuelve pos (orden) y cod (clave cruzada a ruesma_rep.gra).
_SQL_GRA_COD_BY_CONTRATO = """\
SELECT
    rcg.pos             AS rcg_pos,
    gra.cod             AS gra_cod,
    gra.nom             AS gra_nom,
    gra.nomori          AS gra_nomori
FROM rcg
JOIN gra ON rcg.gra = gra.ide
WHERE rcg.con = ?
ORDER BY rcg.pos
"""

# Query 2b (BBDD rep): ruesma_rep.gra por cod.
# Devuelve ide (el que necesitamos para descarga).
_SQL_GRA_REP_BY_COD = """\
SELECT
    ide                 AS gra_rep_ide,
    nom                 AS gra_nom,
    nomori              AS gra_nomori
FROM gra
WHERE cod = ?
"""


class SigridApiContratoClient:
    """Cliente HTTP a ``sigrid-api`` para extraer contratos completos.

    Flujo:
      1. Query principal (``_SQL_HEADER_AND_LINES``): cabecera + líneas
         (con partida) en un solo resultset, agrupadas en memoria por
         ``codigo_contrato``.
      2. Por cada contrato agrupado, se hace un fetch auxiliar del PDF
         principal del contrato:
           a. Query en BBDD principal (``ruesma``) a ``rcg`` + ``gra``
              para obtener los ``gra.cod`` vinculados al contrato.
           b. Query en BBDD réplica (``ruesma_rep``) a ``gra`` por cada
              ``cod`` para obtener el ``ide`` (usable en la descarga de
              documentos). Nos quedamos con el primer PDF por ``rcg.pos``.

    Por qué 3 queries y no una: el PDF vive en ``ruesma_rep`` (otra BBDD),
    así que no se puede JOINear con la principal. Y vincular rcg/gra en
    la query de cabecera multiplicaría filas por documento, enredando la
    agrupación por ``codigo_contrato``.

    Si algo falla al buscar PDFs, ``gra_rep_ide`` queda ``None`` para ese
    contrato pero el resto (cabecera + líneas) se devuelve correctamente.
    """

    def __init__(
        self,
        *,
        base_url: str,
        function_key: str,
        database: str,
        timeout_s: float = 30.0,
        max_rows: int = 1000,
        database_rep: str = "ruesma_rep",
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
        self._database_rep = database_rep
        self._timeout_s = float(timeout_s)
        self._max_rows = int(max_rows)
        logger.info(
            "%s Instanciado. base_url=%s database=%s database_rep=%s "
            "max_rows=%s key_len=%s",
            _LOG_PREFIX,
            self._base_url,
            self._database,
            self._database_rep,
            self._max_rows,
            len(function_key),
        )

    # --------------------------------------------------------------- #
    # API pública
    # --------------------------------------------------------------- #
    def fetch_contratos(
        self,
        *,
        cif_proveedor: str,
        codigo_obra_normalizado: str,
    ) -> list[ContratoEnrichmentResult]:
        logger.info(
            "%s fetch_contratos INICIO cif=%s obra=%s",
            _LOG_PREFIX,
            cif_proveedor,
            codigo_obra_normalizado,
        )

        # Paso 1: cabecera + líneas.
        columns, rows = self._post_sql_read(
            sql=_SQL_HEADER_AND_LINES,
            parameters=[cif_proveedor, codigo_obra_normalizado],
            database=self._database,
            label="header_and_lines",
        )
        contrato_ides, results_without_pdf = self._group_rows_by_contrato(
            columns=columns,
            rows=rows,
        )
        logger.info(
            "%s Agrupado cabecera+líneas: contratos=%s total_lineas=%s",
            _LOG_PREFIX,
            len(results_without_pdf),
            sum(len(c.lines) for c in results_without_pdf),
        )

        if not results_without_pdf:
            return []

        # Paso 2: por cada contrato, su gra_rep_ide del PDF principal.
        # Si el fetch de un contrato falla, devolvemos None para ese y
        # continuamos con los demás.
        enriched: list[ContratoEnrichmentResult] = []
        for contrato, contrato_ide in zip(results_without_pdf, contrato_ides):
            gra_rep_ide = self._safe_fetch_gra_rep_ide(contrato_ide=contrato_ide)
            enriched.append(
                ContratoEnrichmentResult(
                    codigo_contrato=contrato.codigo_contrato,
                    nombre_contrato=contrato.nombre_contrato,
                    fecha_alta_contrato=contrato.fecha_alta_contrato,
                    fecha_contrato=contrato.fecha_contrato,
                    vigencia_desde=contrato.vigencia_desde,
                    vigencia_hasta=contrato.vigencia_hasta,
                    importe_total=contrato.importe_total,
                    cif_proveedor=contrato.cif_proveedor,
                    nombre_proveedor=contrato.nombre_proveedor,
                    codigo_obra=contrato.codigo_obra,
                    nombre_obra=contrato.nombre_obra,
                    gra_rep_ide=gra_rep_ide,
                    lines=contrato.lines,
                )
            )

        logger.info(
            "%s fetch_contratos FIN contratos=%s pdfs_encontrados=%s",
            _LOG_PREFIX,
            len(enriched),
            sum(1 for c in enriched if c.gra_rep_ide is not None),
        )
        return enriched

    # --------------------------------------------------------------- #
    # HTTP primitive
    # --------------------------------------------------------------- #
    def _post_sql_read(
        self,
        *,
        sql: str,
        parameters: list[Any],
        database: str,
        label: str,
    ) -> tuple[list[str], list[list[Any]]]:
        url = f"{self._base_url}/api/sql/read"
        payload = {
            "database": database,
            "sql": sql,
            "parameters": parameters,
            "timeout_seconds": int(self._timeout_s),
            "max_rows": self._max_rows,
        }
        headers = {
            "x-functions-key": self._function_key,
            "Content-Type": "application/json",
        }

        logger.info(
            "%s REQUEST [%s] -> POST %s db=%s params=%s",
            _LOG_PREFIX,
            label,
            url,
            database,
            parameters,
        )

        transport = httpx.HTTPTransport(retries=1)
        try:
            with httpx.Client(timeout=self._timeout_s, transport=transport) as client:
                response = client.post(url, json=payload, headers=headers)
        except Exception as exc:
            logger.exception("%s FALLO de transporte [%s]. exc=%r", _LOG_PREFIX, label, exc)
            raise

        status = response.status_code
        body_text = response.text or ""
        logger.info(
            "%s RESPONSE [%s] <- status=%s body_len=%s preview=%s",
            _LOG_PREFIX,
            label,
            status,
            len(body_text),
            body_text[:200],
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
        return columns, rows

    # --------------------------------------------------------------- #
    # Agrupación cabecera + líneas
    # --------------------------------------------------------------- #
    @staticmethod
    def _group_rows_by_contrato(
        *,
        columns: list[str],
        rows: list[list[Any]],
    ) -> tuple[list[int], list[ContratoEnrichmentResult]]:
        """Agrupa filas por codigo_contrato. Devuelve (ides, resultados)
        en el mismo orden para que luego podamos asociar el PDF correcto
        a cada contrato.
        """
        buckets: "OrderedDict[str, tuple[dict[str, Any], list[ContratoLineFromSigrid]]]" = (
            OrderedDict()
        )

        for row in rows:
            row_map = dict(zip(columns, row))
            codigo = _opt_str(row_map.get("codigo_contrato"))
            if codigo is None:
                continue

            if codigo not in buckets:
                buckets[codigo] = (row_map, [])

            linea_value = row_map.get("linea")
            if linea_value is None:
                # Contrato sin líneas en el ERP (LEFT JOIN no casó).
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
                    codigo_partida=_opt_str(row_map.get("codigo_partida")),
                    descripcion_partida=_opt_str(row_map.get("descripcion_partida")),
                )
            )

        contrato_ides: list[int] = []
        results: list[ContratoEnrichmentResult] = []
        for codigo, (header_row, lines) in buckets.items():
            contrato_ide = _opt_int(header_row.get("contrato_ide"))
            contrato_ides.append(contrato_ide if contrato_ide is not None else 0)
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
                    gra_rep_ide=None,  # se rellena en fetch_contratos
                    lines=lines,
                )
            )
        return contrato_ides, results

    # --------------------------------------------------------------- #
    # PDF lookup: rcg.con → ruesma.gra.cod → ruesma_rep.gra.ide
    # --------------------------------------------------------------- #
    def _safe_fetch_gra_rep_ide(self, *, contrato_ide: int) -> int | None:
        """Igual que ``_fetch_gra_rep_ide`` pero NO propaga excepciones.

        Si Sigrid responde mal para la búsqueda de PDFs, lo registramos
        pero no rompemos la persistencia del contrato — la cabecera y
        las líneas ya valen por sí solas.
        """
        if contrato_ide == 0:
            return None
        try:
            return self._fetch_gra_rep_ide(contrato_ide=contrato_ide)
        except Exception as exc:
            logger.warning(
                "%s PDF lookup falló para contrato_ide=%s. exc=%r",
                _LOG_PREFIX,
                contrato_ide,
                exc,
            )
            return None

    def _fetch_gra_rep_ide(self, *, contrato_ide: int) -> int | None:
        """Encadena las 2 queries auxiliares para obtener ruesma_rep.gra.ide.

        Devuelve el ``ide`` del PRIMER PDF vinculado al contrato,
        ordenado por ``rcg.pos`` (orden de vinculación). Si hay varios,
        se toma el primero; si ninguno es PDF, ``None``.
        """
        cols, rows = self._post_sql_read(
            sql=_SQL_GRA_COD_BY_CONTRATO,
            parameters=[contrato_ide],
            database=self._database,
            label=f"rcg_gra_for_ctr_{contrato_ide}",
        )

        # Ya viene ordenado por rcg.pos. Filtramos a PDFs (nombre .pdf).
        pdf_cods: list[str] = []
        for row in rows:
            row_map = dict(zip(cols, row))
            cod = _opt_str(row_map.get("gra_cod"))
            if cod is None:
                continue
            name = (
                _opt_str(row_map.get("gra_nomori"))
                or _opt_str(row_map.get("gra_nom"))
                or ""
            )
            if name.lower().endswith(".pdf"):
                pdf_cods.append(cod)

        if not pdf_cods:
            logger.info(
                "%s contrato_ide=%s sin PDFs vinculados",
                _LOG_PREFIX,
                contrato_ide,
            )
            return None

        # Para cada cod, resolvemos el ide en ruesma_rep. En cuanto
        # encontremos el primero válido, devolvemos.
        for cod in pdf_cods:
            cols_rep, rows_rep = self._post_sql_read(
                sql=_SQL_GRA_REP_BY_COD,
                parameters=[cod],
                database=self._database_rep,
                label=f"gra_rep_for_cod_{cod}",
            )
            for row in rows_rep:
                row_map = dict(zip(cols_rep, row))
                gra_rep_ide = _opt_int(row_map.get("gra_rep_ide"))
                if gra_rep_ide is not None:
                    logger.info(
                        "%s contrato_ide=%s → gra_rep_ide=%s (cod=%s)",
                        _LOG_PREFIX,
                        contrato_ide,
                        gra_rep_ide,
                        cod,
                    )
                    return gra_rep_ide

        # PDFs localizados en ruesma.gra pero ninguno aparece en
        # ruesma_rep — extraño, pero posible en cierta fase de
        # sincronización. Lo registramos como warning.
        logger.warning(
            "%s contrato_ide=%s PDFs vinculados pero ningún gra_rep_ide; "
            "cods=%s",
            _LOG_PREFIX,
            contrato_ide,
            pdf_cods,
        )
        return None


# --------------------------------------------------------------------- #
# Helpers de parseo defensivo
# --------------------------------------------------------------------- #
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
