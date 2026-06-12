# infrastructure/documents/docx_pdf_renderer.py
"""Renderizado de Word (.docx) a PDF preservando la ESTRUCTURA.

Motivación (jun 2026)
---------------------
El contrato de Sigrid viene como uno o varios Word. Hasta ahora se
combinaban extrayendo solo el TEXTO (``word_text_extractor``) y
volcándolo a un PDF de texto plano (``simple_pdf_writer``): las tablas
de tarifas quedaban aplanadas a texto con separadores `` | ``, con muy
mala estructura para que el LLM de sv5 leyera los precios de cada
modificador. El resultado era un "PDF con mala estructura".

Este módulo renderiza el .docx a un PDF que CONSERVA encabezados,
párrafos, listas y —lo importante— las TABLAS de precios como tablas
reales (rejilla), que es justo lo que el LLM necesita para leer las
tarifas con precisión.

Pipeline 100 % Python, SIN dependencias nativas (ni LibreOffice, ni
cairo/pango), apto para Azure Functions Flex Consumption:

    .docx  --mammoth-->  HTML  --xhtml2pdf-->  PDF

Notas:
- Solo aplica a ``.docx`` (Office Open XML). Para ``.doc`` binario
  (Word 97-2003) mammoth no sirve; el llamador debe usar el fallback de
  texto plano (``word_text_extractor`` + ``simple_pdf_writer``).
- Todo el módulo degrada con elegancia: si las librerías no están
  instaladas o algo falla, las funciones devuelven ``None`` / ``False``
  y el llamador cae al pipeline de texto plano de siempre. Nunca rompe
  el flujo de combinación de contrato.

Dependencias (añadir a requirements.txt de sv3):
    mammoth
    xhtml2pdf
"""
from __future__ import annotations

import io
import logging
from xml.sax.saxutils import escape as _xml_escape

logger = logging.getLogger(__name__)

try:  # pragma: no cover - depende del entorno
    import mammoth  # type: ignore

    _HAS_MAMMOTH = True
except Exception:  # noqa: BLE001
    _HAS_MAMMOTH = False

try:  # pragma: no cover - depende del entorno
    from xhtml2pdf import pisa  # type: ignore

    _HAS_XHTML2PDF = True
except Exception:  # noqa: BLE001
    _HAS_XHTML2PDF = False


# CSS del PDF final. Compacto y orientado a contratos: tablas con
# rejilla fina, fuentes pequeñas para que quepan las tarifas, salto de
# página entre documentos combinados.
_CSS = (
    "@page { size: A4; margin: 1.5cm; }"
    " body { font-family: Helvetica, Arial, sans-serif; font-size: 9pt;"
    "        color: #000; }"
    " h1, h2, h3, h4, h5 { font-family: Helvetica, Arial, sans-serif;"
    "        margin: 6pt 0 3pt; }"
    " h1 { font-size: 14pt; } h2 { font-size: 12pt; } h3 { font-size: 11pt; }"
    " p { margin: 2pt 0; }"
    " ul, ol { margin: 2pt 0 2pt 14pt; }"
    " table { border-collapse: collapse; width: 100%; margin: 4pt 0; }"
    " th, td { border: 0.5pt solid #444; padding: 2pt 4pt; font-size: 8pt;"
    "          vertical-align: top; }"
    " th { background-color: #eeeeee; font-weight: bold; }"
    " .doc-sep { page-break-before: always; }"
    " .doc-title { font-size: 12pt; font-weight: bold; margin: 0 0 6pt;"
    "          border-bottom: 1pt solid #000; padding-bottom: 2pt; }"
    " pre { white-space: pre-wrap; font-family: Helvetica, Arial, sans-serif;"
    "       font-size: 8pt; margin: 2pt 0; }"
)

_HTML_SHELL = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
    "{css}"
    "</style></head><body>{body}</body></html>"
)


def renderer_available() -> bool:
    """True si las dos librerías del pipeline están disponibles."""
    return _HAS_MAMMOTH and _HAS_XHTML2PDF


def docx_to_html_fragment(data: bytes) -> str | None:
    """Convierte un ``.docx`` en un fragmento HTML (encabezados, párrafos,
    listas y tablas). Devuelve ``None`` si mammoth no está o falla.
    """
    if not _HAS_MAMMOTH:
        return None
    try:
        result = mammoth.convert_to_html(io.BytesIO(data))
        return result.value or ""
    except Exception:  # noqa: BLE001
        logger.exception("[docx-render] mammoth falló convirtiendo .docx")
        return None


def text_to_html_fragment(texto: str) -> str:
    """Envuelve texto plano (fallback de .doc / docx ilegible) en un
    bloque ``<pre>`` escapado, para incrustarlo en el PDF combinado sin
    romper el HTML.
    """
    return "<pre>" + _xml_escape(texto or "") + "</pre>"


def combine_html_to_pdf(sections: list[tuple[str, str]]) -> bytes | None:
    """Combina varias secciones (titulo, fragmento_html) en un único PDF.

    La primera sección abre el documento; cada siguiente arranca en
    página nueva. Devuelve los bytes del PDF, o ``None`` si xhtml2pdf no
    está disponible o no produjo contenido.
    """
    if not _HAS_XHTML2PDF:
        return None

    chunks: list[str] = []
    for idx, (titulo, html) in enumerate(sections):
        sep_cls = "" if idx == 0 else " class='doc-sep'"
        safe_title = _xml_escape(titulo or "")
        chunks.append(
            "<div{sep}><div class='doc-title'>{title}</div>{body}</div>".format(
                sep=sep_cls,
                title=safe_title,
                body=(html or ""),
            )
        )

    full_html = _HTML_SHELL.format(css=_CSS, body="\n".join(chunks))

    out = io.BytesIO()
    try:
        result = pisa.CreatePDF(src=full_html, dest=out, encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.exception("[docx-render] xhtml2pdf lanzó excepción")
        return None

    data = out.getvalue()
    if not data:
        logger.warning("[docx-render] xhtml2pdf no produjo bytes de PDF.")
        return None
    if getattr(result, "err", 0):
        # xhtml2pdf suele producir un PDF usable aun con avisos no
        # fatales; lo registramos pero devolvemos el PDF si tiene bytes.
        logger.warning(
            "[docx-render] xhtml2pdf terminó con %s aviso(s); "
            "se usa el PDF de todos modos (%s bytes).",
            result.err,
            len(data),
        )
    return data
