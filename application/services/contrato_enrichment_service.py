# application/services/contrato_enrichment_service.py
from __future__ import annotations

import logging

from application.services.obra_code_normalizer import normalize_obra_code
from domain.ports.contrato_enrichment_port import ContratoEnrichmentClient
from domain.ports.contrato_merge_repository_port import ContratoMergeRepository

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[contrato-enrichment]"


class ContratoEnrichmentService:
    """Orquesta la búsqueda y persistencia de contratos proveedor×obra.

    Flujo:
      1. Lee (cif, obra_codigo) del merge recién persistido.
      2. Normaliza el código de obra con la MISMA regla que obra-enrichment.
      3. Si falta el CIF o la obra no valida, omite.
      4. Llama a Sigrid (lista completa de contratos que casen).
      5. Borra los contratos previos del merge e inserta los nuevos.
      6. Si hay EXACTAMENTE 1 contrato, lo auto-selecciona. Si hay 0 o
         varios, deja selected_contrato_codigo a NULL (el usuario elige
         en el portal).

    Best-effort: cualquier fallo (red, HTTP 5xx, validación) se captura
    y loguea. No rompe el pipeline de persistencia.
    """

    def __init__(
        self,
        *,
        client: ContratoEnrichmentClient,
        repository: ContratoMergeRepository,
        enabled: bool = True,
    ) -> None:
        self._client = client
        self._repository = repository
        self._enabled = enabled
        logger.info(
            "%s ContratoEnrichmentService INSTANCIADO (enabled=%s client=%s repo=%s)",
            _LOG_PREFIX,
            enabled,
            type(client).__name__,
            type(repository).__name__,
        )

    def enrich_merge_document(self, *, merge_document_id: str) -> int:
        """Devuelve el número de contratos insertados (0 si se omitió)."""
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
            return 0

        cif, obra_raw = self._repository.get_merge_cif_and_obra(
            document_id=merge_document_id,
        )
        logger.info(
            "%s Paso 1 — leídos del merge: cif=%r obra_raw=%r",
            _LOG_PREFIX,
            cif,
            obra_raw,
        )

        cif_clean = (cif or "").strip().upper().replace(" ", "") or None
        obra_norm = normalize_obra_code(obra_raw)
        logger.info(
            "%s Paso 2 — normalizados: cif=%r obra=%r",
            _LOG_PREFIX,
            cif_clean,
            obra_norm,
        )
        if not cif_clean or not obra_norm:
            logger.warning(
                "%s Faltan datos o no validan; se OMITE consulta. cif=%r obra=%r",
                _LOG_PREFIX,
                cif_clean,
                obra_norm,
            )
            return 0

        logger.info(
            "%s Paso 3 — LLAMANDO a Sigrid (cif=%s obra=%s)...",
            _LOG_PREFIX,
            cif_clean,
            obra_norm,
        )
        try:
            contratos = self._client.fetch_contratos(
                cif_proveedor=cif_clean,
                codigo_obra_normalizado=obra_norm,
            )
        except Exception as exc:
            logger.exception(
                "%s ERROR llamando a Sigrid. exc=%r",
                _LOG_PREFIX,
                exc,
            )
            return 0

        logger.info(
            "%s Paso 3 — Sigrid devolvió %s contrato(s)",
            _LOG_PREFIX,
            len(contratos),
        )

        try:
            self._repository.replace_contratos(
                document_id=merge_document_id,
                contratos=contratos,
            )
        except Exception as exc:
            logger.exception(
                "%s ERROR guardando contratos. exc=%r",
                _LOG_PREFIX,
                exc,
            )
            return 0

        if len(contratos) == 1:
            codigo = contratos[0].codigo_contrato
            try:
                self._repository.set_selected_contrato(
                    document_id=merge_document_id,
                    codigo_contrato=codigo,
                )
                logger.info(
                    "%s Auto-seleccionado contrato único: %s",
                    _LOG_PREFIX,
                    codigo,
                )
            except Exception as exc:
                logger.exception(
                    "%s ERROR auto-seleccionando contrato. codigo=%s exc=%r",
                    _LOG_PREFIX,
                    codigo,
                    exc,
                )
        elif len(contratos) > 1:
            logger.info(
                "%s %s contratos encontrados; NO se auto-selecciona "
                "(el usuario elegirá en el portal).",
                _LOG_PREFIX,
                len(contratos),
            )
        else:
            logger.warning(
                "%s 0 contratos para (cif=%s, obra=%s). "
                "selected_contrato_codigo queda a NULL.",
                _LOG_PREFIX,
                cif_clean,
                obra_norm,
            )

        logger.info(
            "%s OK — document_id=%s contratos_guardados=%s",
            _LOG_PREFIX,
            merge_document_id,
            len(contratos),
        )
        return len(contratos)
