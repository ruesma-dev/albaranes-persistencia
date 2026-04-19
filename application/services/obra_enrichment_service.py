# application/services/obra_enrichment_service.py
from __future__ import annotations

import logging

from application.services.obra_code_normalizer import normalize_obra_code
from domain.ports.obra_enrichment_port import ObraEnrichmentClient
from domain.ports.obra_merge_repository_port import ObraMergeRepository

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[obra-enrichment]"


class ObraEnrichmentService:
    """Orquesta el enriquecimiento del merge con datos de obra on-prem.

    Flujo:
      1. Lee obra_codigo del registro merge recién persistido.
      2. Normaliza a 4 dígitos con 0 inicial.
      3. Si el código no valida, omite la llamada HTTP.
      4. Pregunta al puerto ObraEnrichmentClient (Sigrid).
      5. Si hay resultado, sobrescribe obra_nombre y obra_direccion
         en albaran_documents_merge.

    Best-effort: ningún fallo aquí debe romper el pipeline. Todas las
    excepciones se capturan y se loguean.
    """

    def __init__(
        self,
        *,
        client: ObraEnrichmentClient,
        repository: ObraMergeRepository,
        enabled: bool = True,
    ) -> None:
        self._client = client
        self._repository = repository
        self._enabled = enabled
        logger.info(
            "%s ObraEnrichmentService INSTANCIADO (enabled=%s client=%s repo=%s)",
            _LOG_PREFIX,
            enabled,
            type(client).__name__,
            type(repository).__name__,
        )

    def enrich_merge_document(self, *, merge_document_id: str) -> bool:
        logger.info(
            "%s enrich_merge_document() INVOCADO document_id=%s",
            _LOG_PREFIX,
            merge_document_id,
        )

        if not self._enabled:
            logger.info(
                "%s DESHABILITADO por configuración. document_id=%s",
                _LOG_PREFIX,
                merge_document_id,
            )
            return False

        raw_codigo = self._repository.get_merge_obra_codigo(
            document_id=merge_document_id,
        )
        logger.info(
            "%s Paso 1 — obra_codigo leído de merge: raw=%r",
            _LOG_PREFIX,
            raw_codigo,
        )

        normalized = normalize_obra_code(raw_codigo)
        logger.info(
            "%s Paso 2 — normalización: raw=%r -> normalized=%r",
            _LOG_PREFIX,
            raw_codigo,
            normalized,
        )
        if normalized is None:
            logger.warning(
                "%s Código inválido o vacío; se OMITE llamada a Sigrid. raw=%r",
                _LOG_PREFIX,
                raw_codigo,
            )
            return False

        logger.info(
            "%s Paso 3 — LLAMANDO a Sigrid (codigo=%s)...",
            _LOG_PREFIX,
            normalized,
        )
        try:
            result = self._client.fetch_obra_by_codigo(
                codigo_obra_normalizado=normalized,
            )
        except Exception as exc:
            logger.exception(
                "%s ERROR llamando a Sigrid. codigo=%s exc=%r",
                _LOG_PREFIX,
                normalized,
                exc,
            )
            return False
        logger.info(
            "%s Paso 3 — respuesta de Sigrid: result=%r",
            _LOG_PREFIX,
            result,
        )

        if result is None:
            logger.warning(
                "%s Sigrid devolvió 0 filas útiles para codigo=%s",
                _LOG_PREFIX,
                normalized,
            )
            return False

        nombre = (result.nombre_obra or "").strip() or None
        direccion = result.direccion_completa
        logger.info(
            "%s Paso 4 — valores compuestos: nombre=%r direccion=%r",
            _LOG_PREFIX,
            nombre,
            direccion,
        )

        try:
            self._repository.update_merge_obra_fields(
                document_id=merge_document_id,
                obra_nombre=nombre,
                obra_direccion=direccion,
            )
        except Exception as exc:
            logger.exception(
                "%s ERROR actualizando merge. document_id=%s exc=%r",
                _LOG_PREFIX,
                merge_document_id,
                exc,
            )
            return False

        logger.info(
            "%s OK — merge actualizado. document_id=%s codigo=%s nombre=%r direccion=%r",
            _LOG_PREFIX,
            merge_document_id,
            normalized,
            nombre,
            direccion,
        )
        return True
