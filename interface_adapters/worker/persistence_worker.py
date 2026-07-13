# interface_adapters/worker/persistence_worker.py
"""Handler del worker de persistencia: consume q-persistencia.

Recupera el PDF (FuenteDocumento) y el envelope (FuenteEnvelope) por
document_id, ejecuta el pipeline REAL de sv3 (persiste + sube PDF + enriquece
Sigrid). El disparo de ``q-valoracion`` lo hace el propio pipeline via el
``ValuationTrigger`` inyectado (``ColaValuationTrigger``) cuando hay contrato
unico; si no, queda pendiente de seleccion (lo retoma sv4).
"""
from __future__ import annotations

import logging

from ruesma_comun.blobs.almacen import BlobNoEncontradoError
from typing import Callable

from application.pipelines.persist_albaran_pipeline import (
    PersistAlbaranPipeline,
    PersistAlbaranRequest,
)
from domain.models.extraction_models import ExtractionMeta
from interface_adapters.worker.ports import FuenteDocumento, FuenteEnvelope
from ruesma_comun.colas import MensajeBase

logger = logging.getLogger(__name__)


# Claves que el ExtractionMeta de sv3 admite (StrictSchemaModel prohibe extras).
# Se derivan del propio modelo para no quedar desactualizadas: el envelope de
# sv2 trae ademas "phase"/"provider", que aqui se descartan (igual que hacia
# sv7 antes de mandar el envelope a sv3).
_META_PERMITIDA = {
    (f.alias or n) for n, f in ExtractionMeta.model_fields.items()
}


def _sanear_envelope(envelope: dict) -> dict:
    """Filtra ``meta`` a las claves que admite sv3; deja data/debug igual."""
    meta = envelope.get("meta")
    if not isinstance(meta, dict):
        return envelope
    meta_filtrada = {k: v for k, v in meta.items() if k in _META_PERMITIDA}
    if meta_filtrada.keys() == meta.keys():
        return envelope
    descartadas = sorted(set(meta) - set(meta_filtrada))
    logger.info("[sv3-worker] meta saneada; descartadas: %s", descartadas)
    return {**envelope, "meta": meta_filtrada}


def construir_handler_persistencia(
    *,
    pipeline: PersistAlbaranPipeline,
    fuente_documento: FuenteDocumento,
    fuente_envelope: FuenteEnvelope,
) -> Callable[[MensajeBase], None]:
    def handler(mensaje: MensajeBase) -> None:
        document_id = mensaje.document_id
        # ``force`` solo lo trae MensajePersistencia (re-fetch manual desde
        # el portal sv4). El consumidor deserializa a MensajePersistencia,
        # pero el tipo estático aquí es MensajeBase → lectura defensiva.
        force_refetch = bool(getattr(mensaje, "force", False))
        logger.info(
            "[sv3-worker] document_id=%s START force_refetch=%s",
            document_id,
            force_refetch,
        )

        # Los mensajes de q-persistencia llegan con DOS espacios de id:
        #   - document_id de ORIGEN (sv1/sv2): existe blob input/{id}.pdf
        #     -> persist completo (camino normal).
        #   - MERGE id (sv4: seleccion de contrato / re-fetch): NO hay
        #     blob -> re-enrichment del merge (baja PDF/MD del contrato
        #     seleccionado y encadena la valoracion).
        try:
            doc = fuente_documento.obtener(document_id)
        except BlobNoEncontradoError:
            logger.info(
                "[sv3-worker] sin blob para %s; se intenta como MERGE id "
                "(mensaje de sv4).",
                document_id,
            )
            ok = pipeline.reenrich_by_merge_id(
                merge_document_id=document_id,
                force_refetch=force_refetch,
            )
            if ok:
                logger.info(
                    "[sv3-worker] document_id=%s OK -> re-enriquecido "
                    "(merge)",
                    document_id,
                )
            else:
                logger.error(
                    "[sv3-worker] document_id=%s NO es blob NI merge; "
                    "mensaje descartado (reintentar no ayuda).",
                    document_id,
                )
            return
        envelope = _sanear_envelope(fuente_envelope.obtener(document_id))
        context = (
            {"correlation_key": mensaje.correlation_key}
            if mensaje.correlation_key
            else {}
        )

        result = pipeline.run(
            PersistAlbaranRequest(
                filename=doc.filename,
                mime_type=doc.mime_type,
                file_bytes=doc.file_bytes,
                extraction_envelope=envelope,
                context=context,
                force_refetch=force_refetch,
            )
        )
        # El pipeline ya disparo q-valoracion (via ColaValuationTrigger) si
        # selected_contrato_codigo != None. Aqui solo trazamos.
        logger.info(
            "[sv3-worker] document_id=%s OK -> persistido "
            "(doc=%s contratos=%s contrato=%s)",
            document_id,
            getattr(result, "document_id", "?"),
            getattr(result, "contratos_count", "?"),
            getattr(result, "selected_contrato_codigo", None),
        )

    return handler
