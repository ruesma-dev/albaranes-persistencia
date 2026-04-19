# infrastructure/sigrid/sigrid_api_obra_client.py
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from domain.models.obra_models import ObraEnrichmentResult

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[obra-enrichment][sigrid-client]"


_SQL_QUERY = """\
SELECT
    con.cod        AS codigo_obra,
    obr.res        AS nombre_obra,
    obr.dir1       AS direccion_linea1,
    obr.dir2       AS direccion_linea2,
    obr.dircpo     AS codigo_postal,
    mun.res        AS municipio,
    pro.res        AS provincia
FROM obr
JOIN con ON obr.ide = con.ide
LEFT JOIN auxmun mun ON obr.munide = mun.ide
LEFT JOIN auxpro pro ON obr.proide = pro.ide
WHERE con.cod = ?
"""


class SigridApiObraClient:
    """Adaptador HTTP contra la Function App ``sigrid-api``.

    Cumple el puerto ``ObraEnrichmentClient``. Realiza un POST a
    ``/api/sql/read`` con la query parametrizada de obra y cabecera
    ``x-functions-key``.

    Selección entre múltiples filas:
        Algunos ``con.cod`` aparecen varias veces en la BBDD (ver
        documentación de la estructura de Sigrid). Este cliente pide
        hasta ``max_rows`` filas y devuelve **la más rica** (la que tenga
        más campos informativos rellenos: dirección, CP, municipio,
        provincia). Si hay empate en riqueza, gana la primera devuelta
        por la BBDD. Esto evita pisar datos buenos del merge con una
        fila casi vacía.
    """

    def __init__(
        self,
        *,
        base_url: str,
        function_key: str,
        database: str,
        timeout_s: float = 30.0,
        max_rows: int = 10,
    ) -> None:
        if not base_url:
            raise ValueError("SigridApiObraClient requiere base_url no vacío")
        if not function_key:
            raise ValueError("SigridApiObraClient requiere function_key no vacío")
        if not database:
            raise ValueError("SigridApiObraClient requiere database no vacío")
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
            "%s Instanciado. base_url=%s database=%s timeout_s=%s max_rows=%s key=[%s]",
            _LOG_PREFIX,
            self._base_url,
            self._database,
            self._timeout_s,
            self._max_rows,
            key_hint,
        )

    def fetch_obra_by_codigo(
        self,
        *,
        codigo_obra_normalizado: str,
    ) -> ObraEnrichmentResult | None:
        url = f"{self._base_url}/api/sql/read"
        payload = {
            "database": self._database,
            "sql": _SQL_QUERY,
            "parameters": [codigo_obra_normalizado],
            "timeout_seconds": int(self._timeout_s),
            "max_rows": self._max_rows,
        }
        headers = {
            "x-functions-key": self._function_key,
            "Content-Type": "application/json",
        }

        logger.info(
            "%s REQUEST -> POST %s codigo=%s database=%s",
            _LOG_PREFIX,
            url,
            codigo_obra_normalizado,
            self._database,
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
        body_preview = body_text[:600]
        logger.info(
            "%s RESPONSE <- status=%s body_len=%s body_preview=%s",
            _LOG_PREFIX,
            status,
            len(body_text),
            body_preview,
        )

        if status >= 400:
            raise RuntimeError(
                f"sigrid-api respondió {status}: {body_preview}"
            )

        try:
            body: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            logger.exception(
                "%s Respuesta no es JSON válido. exc=%r preview=%s",
                _LOG_PREFIX,
                exc,
                body_preview,
            )
            raise RuntimeError(f"sigrid-api respuesta no JSON: {body_preview}") from exc

        if not body.get("ok", False):
            logger.warning(
                "%s Respuesta con ok=false. body=%r",
                _LOG_PREFIX,
                body,
            )
            raise RuntimeError(f"sigrid-api devolvió ok=false: {body!r}")

        columns: list[str] = list(body.get("columns") or [])
        rows: list[list[Any]] = list(body.get("rows") or [])
        row_count = body.get("row_count", len(rows))
        logger.info(
            "%s Parseado: columns=%s row_count=%s",
            _LOG_PREFIX,
            columns,
            row_count,
        )

        if not rows:
            logger.info(
                "%s Sin filas. La obra con cod=%s no existe en %s.",
                _LOG_PREFIX,
                codigo_obra_normalizado,
                self._database,
            )
            return None

        # Convertir cada fila a ObraEnrichmentResult y elegir la más rica.
        candidates: list[ObraEnrichmentResult] = [
            self._row_to_result(
                row=row,
                columns=columns,
                codigo_fallback=codigo_obra_normalizado,
            )
            for row in rows
        ]

        if len(candidates) == 1:
            chosen = candidates[0]
            logger.info(
                "%s Única fila. richness=%s",
                _LOG_PREFIX,
                chosen.richness_score,
            )
            return chosen

        # Varias filas: rankeamos por riqueza (más campos no vacíos primero).
        # Si empatan, conservamos el orden original de la BBDD.
        scored = [
            (idx, candidate.richness_score, candidate)
            for idx, candidate in enumerate(candidates)
        ]
        # Estabilidad: ordenamos por (-richness_score, idx).
        scored.sort(key=lambda tup: (-tup[1], tup[0]))

        # Log de todos los candidatos para depurar elecciones futuras.
        for idx, score, candidate in scored:
            logger.info(
                "%s   candidato[%s]: richness=%s nombre=%r dir1=%r cp=%r mun=%r prov=%r",
                _LOG_PREFIX,
                idx,
                score,
                candidate.nombre_obra,
                candidate.direccion_linea1,
                candidate.codigo_postal,
                candidate.municipio,
                candidate.provincia,
            )

        _, winning_score, chosen = scored[0]
        logger.info(
            "%s ELEGIDA fila con richness=%s entre %s candidatos.",
            _LOG_PREFIX,
            winning_score,
            len(candidates),
        )

        # Si todos los candidatos estaban completamente vacíos, devolvemos None
        # para que el servicio considere que no hay datos útiles (evita pisar
        # el merge con nulls).
        if winning_score == 0:
            logger.warning(
                "%s Todas las filas devueltas están vacías. Se devuelve None.",
                _LOG_PREFIX,
            )
            return None

        return chosen

    @staticmethod
    def _row_to_result(
        *,
        row: list[Any],
        columns: list[str],
        codigo_fallback: str,
    ) -> ObraEnrichmentResult:
        row_map = dict(zip(columns, row))
        return ObraEnrichmentResult(
            codigo_obra=str(row_map.get("codigo_obra") or codigo_fallback),
            nombre_obra=_optional_str(row_map.get("nombre_obra")),
            direccion_linea1=_optional_str(row_map.get("direccion_linea1")),
            direccion_linea2=_optional_str(row_map.get("direccion_linea2")),
            codigo_postal=_optional_str(row_map.get("codigo_postal")),
            municipio=_optional_str(row_map.get("municipio")),
            provincia=_optional_str(row_map.get("provincia")),
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)
