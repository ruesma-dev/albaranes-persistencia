# infrastructure/documents/pdf_merger.py
"""Fusión de varios PDFs en uno solo, página a página.

Se usa para combinar los PDFs del contrato (original + ampliaciones,
excluidos los audit-trail) en UN único documento manteniendo el formato
ORIGINAL de cada página — tablas de precios incluidas — sin conversión
intermedia alguna.

Requiere ``pypdf`` (pure-Python):  pip install pypdf

El import es perezoso y todo fallo degrada con ``None`` para que el
caller pueda aplicar su plan B (usar solo el PDF más antiguo) sin que
el pipeline se caiga por un PDF corrupto o por falta de la librería.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


def merge_pdfs(pdf_blobs: list[bytes]) -> bytes | None:
    """Fusiona los PDFs en el orden recibido. ``None`` si no se pudo.

    Los PDFs ilegibles individualmente se saltan (mejor un combinado
    parcial que nada); si ninguno es legible, ``None``.
    """
    if not pdf_blobs:
        return None
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        logger.warning(
            "[pdf-merge] pypdf no está instalado (pip install pypdf); "
            "no se pueden combinar los PDFs."
        )
        return None

    writer = PdfWriter()
    added_docs = 0
    for idx, blob in enumerate(pdf_blobs):
        try:
            reader = PdfReader(io.BytesIO(blob))
            for page in reader.pages:
                writer.add_page(page)
            added_docs += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[pdf-merge] PDF %s ilegible, se omite: %r", idx, exc
            )

    if added_docs == 0:
        return None

    try:
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[pdf-merge] fallo serializando el combinado: %r", exc)
        return None
