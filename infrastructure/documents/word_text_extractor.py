# infrastructure/documents/word_text_extractor.py
"""Extracción de texto de documentos Word (.docx y .doc) sin dependencias.

Pensado para alimentar a un LLM con el TEXTO del contrato: prioriza
recuperar el contenido íntegro (tablas de precios incluidas como texto)
sobre conservar el formato.

- ``.docx`` (Office Open XML): es un ZIP; se lee ``word/document.xml``
  y se reconstruye el texto respetando párrafos, saltos y celdas de
  tabla (separadas con `` | ``). Fiable.
- ``.doc`` (Word 97-2003, contenedor OLE): no hay parser del formato
  binario en la stdlib, así que se aplica una heurística de "runs" de
  texto: se localizan secuencias legibles tanto en UTF-16LE como en
  cp1252 y se concatenan. Recupera el contenido textual con algo de
  ruido de metadatos, suficiente para la valoración por IA.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from xml.sax.saxutils import unescape

logger = logging.getLogger(__name__)

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"

# Caracteres "legibles" para la heurística cp1252: ASCII imprimible +
# letras acentuadas y signos típicos del español.
_CP1252_RUN_RX = re.compile(
    r"[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñºª€()%/.,;:¡!¿?\"'+\-_=<>@#&*\[\]\s]{4,}"
)
# Run UTF-16LE: pares (char ascii/latin, \x00) repetidos.
_UTF16_RUN_RX = re.compile(rb"(?:[\x20-\x7e\xc0-\xff][\x00]){4,}")


def extract_text(filename: str, data: bytes) -> str:
    """Devuelve el texto del documento, o cadena vacía si no se pudo."""
    name = (filename or "").lower()
    try:
        if data.startswith(_ZIP_MAGIC) or name.endswith(".docx"):
            text = _extract_docx(data)
            if text:
                return text
        if data.startswith(_OLE_MAGIC) or name.endswith(".doc"):
            return _extract_doc_heuristic(data)
        # Último recurso: tratarlo como texto plano cp1252.
        return _clean_lines(data.decode("cp1252", errors="ignore"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[word-extract] fallo extrayendo %r: %r", filename, exc
        )
        return ""


# --------------------------------------------------------------------- #
# .docx
# --------------------------------------------------------------------- #
def _extract_docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        try:
            xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
        except KeyError:
            return ""
    # Saltos estructurales ANTES de quitar las etiquetas:
    #  - fin de celda de tabla → separador de columna
    #  - fin de párrafo / salto de línea / fin de fila → nueva línea
    xml = re.sub(r"</w:tc>", " | ", xml)
    xml = re.sub(r"</w:p>|<w:br[^>]*/>|</w:tr>", "\n", xml)
    # El texto real vive en <w:t ...>...</w:t>; el resto de etiquetas fuera.
    parts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, flags=re.DOTALL)
    keep = set()
    # Reconstrucción: sustituimos cada w:t por su contenido y eliminamos
    # el resto de marcado.
    text = re.sub(r"<w:t[^>]*>(.*?)</w:t>", lambda m: m.group(1), xml,
                  flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    _ = parts, keep  # (parts solo se usa para validar que había texto)
    if not parts:
        return ""
    return _clean_lines(text)


# --------------------------------------------------------------------- #
# .doc (heurística)
# --------------------------------------------------------------------- #
def _extract_doc_heuristic(data: bytes) -> str:
    chunks: list[tuple[int, str]] = []

    # 1) Runs UTF-16LE (Word moderno guarda el texto así muy a menudo).
    for m in _UTF16_RUN_RX.finditer(data):
        try:
            s = m.group(0).decode("utf-16-le", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        if _looks_like_text(s):
            chunks.append((m.start(), s))

    # 2) Runs cp1252 (texto plano de la piece table sin unicode).
    decoded = data.decode("cp1252", errors="ignore")
    for m in _CP1252_RUN_RX.finditer(decoded):
        s = m.group(0)
        if _looks_like_text(s):
            chunks.append((m.start(), s))

    if not chunks:
        return ""
    chunks.sort(key=lambda t: t[0])

    # Dedup aproximado conservando orden (el mismo texto puede aparecer
    # en ambas pasadas).
    seen: set[str] = set()
    out: list[str] = []
    for _, s in chunks:
        key = re.sub(r"\s+", " ", s).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s.strip())
    return _clean_lines("\n".join(out))


def _looks_like_text(s: str) -> bool:
    letters = sum(1 for ch in s if ch.isalpha())
    return letters >= 3 and letters / max(len(s), 1) > 0.25


def _clean_lines(text: str) -> str:
    # Normaliza saltos, colapsa espacios repetidos y elimina líneas vacías
    # consecutivas.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n")
    text = re.sub(r"[\x00-\x08\x0e-\x1f\x7f]", "", text)
    lines = []
    blank = False
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            if not blank:
                lines.append("")
            blank = True
            continue
        blank = False
        lines.append(line)
    return "\n".join(lines).strip()
