# infrastructure/documents/simple_pdf_writer.py
"""Generador de PDF de texto plano SIN dependencias externas.

Construye un PDF 1.4 mínimo (Helvetica, codificación WinAnsi/cp1252,
A4, multipágina) a partir de secciones (título, cuerpo). Se usa para
materializar como UN solo PDF el contrato combinado a partir de los
Word originales de Sigrid, de modo que el resto del pipeline (subida a
SharePoint, descarga en sv5 y envío al LLM) no cambie.

No pretende ser un PDF "bonito": prioriza texto íntegro y legible.
"""
from __future__ import annotations

_PAGE_W = 595  # A4 en puntos
_PAGE_H = 842
_MARGIN = 50
_FONT_SIZE = 9
_TITLE_SIZE = 12
_LEADING = 12
_MAX_CHARS = 100  # ancho aproximado de línea con Helvetica 9pt en A4


def build_text_pdf(sections: list[tuple[str, str]]) -> bytes:
    """Genera el PDF. ``sections`` = lista de (titulo, cuerpo)."""
    pages = _layout_pages(sections)
    return _emit_pdf(pages)


# --------------------------------------------------------------------- #
# Maquetación: trocear el texto en páginas de líneas (texto, es_titulo)
# --------------------------------------------------------------------- #
def _wrap(line: str, width: int) -> list[str]:
    if len(line) <= width:
        return [line]
    out: list[str] = []
    current = ""
    for word in line.split(" "):
        # Palabra más larga que el ancho: trocear en seco.
        while len(word) > width:
            espacio = width - len(current) - (1 if current else 0)
            if espacio > 10:
                current = (current + " " + word[:espacio]).strip()
                word = word[espacio:]
            out.append(current) if current else None
            current = ""
            if len(word) <= width:
                break
            out.append(word[:width])
            word = word[width:]
        candidate = (current + " " + word).strip() if current else word
        if len(candidate) <= width:
            current = candidate
        else:
            out.append(current)
            current = word
    if current:
        out.append(current)
    return out or [""]


def _layout_pages(
    sections: list[tuple[str, str]],
) -> list[list[tuple[str, bool]]]:
    lines_per_page = (_PAGE_H - 2 * _MARGIN) // _LEADING
    pages: list[list[tuple[str, bool]]] = []
    page: list[tuple[str, bool]] = []

    def flush() -> None:
        nonlocal page
        if page:
            pages.append(page)
            page = []

    def add(text: str, title: bool = False) -> None:
        nonlocal page
        if len(page) >= lines_per_page:
            flush()
        page.append((text, title))

    for idx, (titulo, cuerpo) in enumerate(sections):
        if idx > 0:
            add("")
            add("=" * 80)
            add("")
        for chunk in _wrap(titulo, _MAX_CHARS - 10):
            add(chunk, title=True)
        add("")
        for raw_line in (cuerpo or "(sin texto extraído)").split("\n"):
            for chunk in _wrap(raw_line, _MAX_CHARS):
                add(chunk)
    flush()
    return pages or [[("(documento vacío)", False)]]


# --------------------------------------------------------------------- #
# Emisión del PDF
# --------------------------------------------------------------------- #
def _escape_pdf_text(s: str) -> bytes:
    # cp1252 (WinAnsi) con reemplazo; escapar \, ( y ).
    raw = s.encode("cp1252", errors="replace")
    raw = raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return raw


def _page_stream(lines: list[tuple[str, bool]]) -> bytes:
    parts: list[bytes] = [b"BT\n"]
    y = _PAGE_H - _MARGIN
    for text, is_title in lines:
        size = _TITLE_SIZE if is_title else _FONT_SIZE
        font = b"/F2" if is_title else b"/F1"
        parts.append(
            font + f" {size} Tf 1 0 0 1 {_MARGIN} {y} Tm (".encode("ascii")
            + _escape_pdf_text(text)
            + b") Tj\n"
        )
        y -= _LEADING
    parts.append(b"ET")
    return b"".join(parts)


def _emit_pdf(pages: list[list[tuple[str, bool]]]) -> bytes:
    # Objetos: 1 catálogo, 2 pages, 3 F1, 4 F2, luego por página
    # (page obj, content obj).
    objects: list[bytes] = []

    n_pages = len(pages)
    first_page_obj = 5
    page_obj_ids = [first_page_obj + 2 * i for i in range(n_pages)]
    kids = " ".join(f"{oid} 0 R" for oid in page_obj_ids)

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode("ascii")
    )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>"
    )

    for i, page_lines in enumerate(pages):
        content = _page_stream(page_lines)
        page_id = page_obj_ids[i]
        content_id = page_id + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_W} "
                f"{_PAGE_H}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> "
                f">> /Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
            + content
            + b"\nendstream"
        )

    # Serialización con xref.
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for idx, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_pos = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_pos}\n"
        "%%EOF\n"
    ).encode("ascii")
    return bytes(out)
