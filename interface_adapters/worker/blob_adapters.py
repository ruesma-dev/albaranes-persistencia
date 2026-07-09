# interface_adapters/worker/blob_adapters.py
"""Adaptadores de PRODUCCIÓN de los puertos del worker de sv3 sobre Blob.

Sustituyen a los stubs de disco (``local_stubs.py``):
- :class:`FuenteEnvelopeBlob`  lee ``envelopes/{document_id}_{fase}.json``
  (lo escribió el worker de sv2).
- :class:`FuenteDocumentoBlob` lee ``input/{document_id}.pdf`` (lo dejó sv1).

Los blobs son EFÍMEROS (lifecycle policy). sv3 persiste el dato durable:
PDF -> SharePoint, datos extraídos -> PostgreSQL.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from interface_adapters.worker.ports import (
    DocumentoPdf,
    FuenteDocumento,
    FuenteEnvelope,
)
from ruesma_comun.blobs import (
    CONTENEDOR_ENVELOPES,
    CONTENEDOR_INPUT,
    AlmacenBlobs,
)

logger = logging.getLogger(__name__)


class FuenteEnvelopeBlob(FuenteEnvelope):
    """Lee ``envelopes/{document_id}_{fase}.json`` (lo dejó el worker de sv2)."""

    def __init__(
        self,
        almacen: AlmacenBlobs,
        *,
        contenedor: str = CONTENEDOR_ENVELOPES,
        fase: str = "phase_1",
    ) -> None:
        self._almacen = almacen
        self._contenedor = contenedor
        self._fase = fase

    def obtener(self, document_id: str) -> Dict[str, Any]:
        nombre = f"{document_id}_{self._fase}.json"
        envelope = self._almacen.get_json(self._contenedor, nombre)
        logger.info(
            "[fuente-envelope-blob] document_id=%s <- %s/%s",
            document_id, self._contenedor, nombre,
        )
        return envelope


class FuenteDocumentoBlob(FuenteDocumento):
    """Lee el PDF de ``input/{document_id}.pdf`` (mismo que usa sv2)."""

    def __init__(
        self,
        almacen: AlmacenBlobs,
        *,
        contenedor: str = CONTENEDOR_INPUT,
        sufijo: str = ".pdf",
    ) -> None:
        self._almacen = almacen
        self._contenedor = contenedor
        self._sufijo = sufijo

    def obtener(self, document_id: str) -> DocumentoPdf:
        nombre = f"{document_id}{self._sufijo}"
        data, metadata, content_type = self._almacen.get_con_metadata(
            self._contenedor, nombre
        )
        filename = metadata.get("filename") or nombre
        mime_type = content_type or metadata.get("mime_type") or "application/pdf"
        logger.info(
            "[fuente-doc-blob] document_id=%s <- %s/%s (%d bytes, file=%s)",
            document_id, self._contenedor, nombre, len(data), filename,
        )
        return DocumentoPdf(filename, mime_type, data)
