# interface_adapters/worker/local_stubs.py
"""Stubs LOCALES de los puertos del worker de sv3 (sin BBDD ni SharePoint)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from interface_adapters.worker.ports import (
    DocumentoPdf,
    FuenteDocumento,
    FuenteEnvelope,
)

logger = logging.getLogger(__name__)


class FuenteEnvelopeFichero(FuenteEnvelope):
    """Lee ``{dir}/{document_id}_{fase}.json`` (lo escribio el worker de sv2)."""

    def __init__(self, envelope_dir: str, *, fase: str = "phase_1") -> None:
        self._dir = Path(envelope_dir)
        self._fase = fase

    def obtener(self, document_id: str) -> Dict[str, Any]:
        path = self._dir / f"{document_id}_{self._fase}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"No encuentro el envelope {path}. ¿Corrio antes el worker de sv2?"
            )
        logger.info("[fuente-envelope] document_id=%s <- %s", document_id, path)
        return json.loads(path.read_text(encoding="utf-8"))


class FuenteDocumentoLocal(FuenteDocumento):
    """Devuelve SIEMPRE el mismo PDF local (ignora document_id) — piloto."""

    def __init__(self, pdf_path: str) -> None:
        self._pdf_path = Path(pdf_path)

    def obtener(self, document_id: str) -> DocumentoPdf:
        data = self._pdf_path.read_bytes()
        logger.info(
            "[fuente-pdf-local] document_id=%s -> %s (%d bytes)",
            document_id, self._pdf_path.name, len(data),
        )
        return DocumentoPdf(self._pdf_path.name, "application/pdf", data)
