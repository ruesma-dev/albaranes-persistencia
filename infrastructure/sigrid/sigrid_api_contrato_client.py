# infrastructure/sigrid/sigrid_api_contrato_client.py
from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from typing import Any

import httpx

from domain.models.contrato_models import (
    ContratoEnrichmentResult,
    ContratoLineFromSigrid,
)
from domain.ports.contrato_enrichment_port import ContratoPdfPayload
from infrastructure.documents import docx_pdf_renderer as docx_renderer
from infrastructure.documents.simple_pdf_writer import build_text_pdf
from infrastructure.documents.word_text_extractor import extract_text
from ruesma_comun.markdown import a_markdown
from ruesma_comun.office import (
    LibreOfficeWordConverter,
    combinar_pdfs,
    es_word,
    pdf_a_markdown,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[contrato-enrichment][sigrid-client]"


# Query cabecera + líneas (ctr.totbas, partida via obrparpar, emp=1).
#
# IMPORTANTE: la SELECT incluye DOS identificadores únicos del ERP:
#
#   * ``ctr.ide AS contrato_ide`` — INDICE PRIMARIO de la cabecera del
#     contrato en Sigrid. Inmutable. Se persiste en
#     ``albaran_contratos_merge.sigrid_ide`` y se usa como clave de
#     UPSERT (un mismo contrato en BBDD se actualiza en lugar de
#     duplicarse cuando llegan más albaranes que lo referencien).
#
#   * ``ctrpro.ide AS line_ide`` — INDICE PRIMARIO de la línea de
#     contrato en Sigrid. Inmutable. Se persiste en
#     ``albaran_contrato_lines_merge.sigrid_ide`` y se usa como clave
#     de UPSERT por línea.
#
# Antes el sv3 hacía DELETE+INSERT por document_id en cada albarán, lo
# que duplicaba contratos cuando el mismo contrato salía en varios
# albaranes. Ahora con estas dos columnas hacemos UPSERT por la
# identidad real del ERP, y la BBDD es la "vista actualizada" del
# estado en Sigrid (no una colección de copias por albarán).
_SQL_HEADER_AND_LINES = """\
SELECT
    ctr.ide             AS contrato_ide,
    con_ctr.cod         AS codigo_contrato,
    con_ctr.res         AS nombre_contrato,
    con_ctr.fec         AS fecha_alta_contrato,
    ctr.fecdoc          AS fecha_contrato,
    ctr.fecvig1         AS vigencia_desde,
    ctr.fecvig2         AS vigencia_hasta,
    ctr.totbas          AS importe_total,
    ctr.entcif          AS cif_proveedor,
    prv.raz             AS nombre_proveedor,
    con_obr.cod         AS codigo_obra,
    con_obr.res         AS nombre_obra,
    ctrpro.ide          AS line_ide,
    ctrpro.pos          AS linea,
    ctrpro.numlin       AS numero_linea,
    con_pro.cod         AS codigo_producto,
    ctrpro.cod2         AS codigo_alternativo,
    ctrpro.unimed       AS unidad_medida,
    ctrpro.res          AS descripcion_linea,
    ctrpro.can          AS uds,
    ctrpro.canser       AS cantidad_servida,
    ctrpro.canfac       AS cantidad_facturada,
    (ctrpro.can - ISNULL(ctrpro.canser, 0)) AS pendiente_servir,
    ctrpro.pre          AS precio_unitario,
    ctrpro.tar          AS precio_bruto,
    ctrpro.dto          AS descuentos,
    ctrpro.tot          AS importe_linea,
    ctrpro.ivacuo       AS cuota_iva,
    ctrpro.docoricod    AS doc_origen,
    obrparpar.cod       AS codigo_partida,
    obrparpar.res       AS descripcion_partida
FROM ctr
JOIN con AS con_ctr       ON ctr.ide     = con_ctr.ide
JOIN con AS con_obr       ON ctr.obride  = con_obr.ide
JOIN prv                  ON ctr.entide  = prv.ide
LEFT JOIN ctrpro          ON ctrpro.docide = ctr.ide
LEFT JOIN pro             ON ctrpro.proide = pro.ide
LEFT JOIN con AS con_pro  ON pro.ide       = con_pro.ide
LEFT JOIN obrparpar       ON ctrpro.paride = obrparpar.ide
WHERE
    prv.cif       = ?
AND con_obr.cod   = ?
AND con_ctr.emp   = 1
ORDER BY con_ctr.cod, ctrpro.pos
"""

_SQL_GRA_COD_BY_CONTRATO = """\
SELECT
    rcg.pos             AS rcg_pos,
    gra.cod             AS gra_cod,
    gra.nom             AS gra_nom,
    gra.nomori          AS gra_nomori,
    gra.fec             AS gra_fec
FROM rcg
JOIN gra ON rcg.gra = gra.ide
WHERE rcg.con = ?
ORDER BY rcg.pos
"""

# Nombres de PDF que NO son el contrato (certificados de firma y
# evidencias de Signaturit y similares).
_AUDIT_NAME_RX = re.compile(r"audit|trail", re.IGNORECASE)

_WORD_EXTS = (".doc", ".docx")


def _select_contract_docs(
    docs: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Selecciona los documentos que componen el contrato.

    ``docs``: dicts con claves ``cod``, ``name``, ``fec`` (entero
    YYYYMMDD, 0 = sin fecha) y ``pos``.

    Regla (jun 2026, acordada con el cliente tras analizar casos reales
    donde se descargaba el audit-trail de Signaturit en lugar del
    contrato). Los nombres con audit/trail se excluyen SIEMPRE:

    1. Si hay PDF (sin audit/trail): se cogen TODOS, ordenados del más
       antiguo al más moderno, y se FUSIONAN página a página en un único
       PDF (formato original intacto, mejor para tablas/precios).
       → ("pdfs", [todos los pdf ordenados])
    2. Si NO hay PDF: se cogen TODOS los Word (.doc/.docx), ordenados, y
       se combinan (texto extraído) en un único PDF.
       → ("words", [todos los word ordenados])
    3. Si no queda nada (p.e. solo audit-trails): → ("none", []).
    """

    def sort_key(d: dict[str, Any]) -> tuple[int, int]:
        fec = d.get("fec") or 0
        # Sin fecha (0) al final; el resto ascendente (más antiguo antes).
        return (fec if fec > 0 else 99999999, d.get("pos") or 0)

    def is_audit(d: dict[str, Any]) -> bool:
        return bool(_AUDIT_NAME_RX.search(str(d.get("name") or "")))

    # PRIORIDAD PDF sobre Word (jul 2026, cambio pedido): el PDF final
    # del contrato conserva mejor tablas/formato (líneas de contenedor,
    # precios) que el texto extraído del Word fuente. Los audit/trail
    # (firmas) se excluyen SIEMPRE. En ambas ramas se genera el markdown
    # (pdf_a_markdown / word_a_markdown) que consume la IA de sv5.
    pdfs = [
        d for d in docs
        if str(d.get("name") or "").lower().endswith(".pdf")
        and not is_audit(d)
    ]
    if pdfs:
        return "pdfs", sorted(pdfs, key=sort_key)

    words = [
        d for d in docs
        if str(d.get("name") or "").lower().endswith(_WORD_EXTS)
        and not is_audit(d)
    ]
    if words:
        return "words", sorted(words, key=sort_key)

    return "none", []

_SQL_GRA_REP_BY_COD = """\
SELECT
    ide                 AS gra_rep_ide,
    nom                 AS gra_nom,
    nomori              AS gra_nomori
FROM gra
WHERE cod = ?
"""


class SigridApiContratoClient:
    """Cliente HTTP a ``sigrid-api`` para extraer contratos + PDFs."""

    def __init__(
        self,
        *,
        base_url: str,
        function_key: str,
        database: str,
        timeout_s: float = 30.0,
        max_rows: int = 1000,
        database_rep: str = "ruesma_rep",
        pdf_timeout_s: float = 120.0,
        word_converter=None,
    ) -> None:
        if not base_url:
            raise ValueError("SigridApiContratoClient requiere base_url")
        if not function_key:
            raise ValueError("SigridApiContratoClient requiere function_key")
        if not database:
            raise ValueError("SigridApiContratoClient requiere database")
        self._base_url = base_url.rstrip("/")
        self._function_key = function_key
        self._database = database
        self._database_rep = database_rep
        # Plan de descarga/combinación por gra_rep_ide "principal" (jun
        # 2026): cuando la selección de documentos del contrato implica
        # VARIOS Word a combinar, _fetch_gra_rep_ide guarda aquí la lista
        # completa y download_contrato_pdf la consume. Si un ide no está
        # (p.e. proceso reiniciado y reuso desde contratos_cache), la
        # descarga cae al comportamiento clásico de un solo documento.
        self._combine_plan_by_primary: dict[int, dict[str, Any]] = {}
        self._timeout_s = float(timeout_s)
        self._max_rows = int(max_rows)
        self._pdf_timeout_s = float(pdf_timeout_s)
        # Conversor Word→PDF/MD inyectable (LibreOffice o Graph). Por
        # defecto LibreOffice para no cambiar el comportamiento si el
        # composition root no inyecta nada.
        self._word_converter = word_converter or LibreOfficeWordConverter()
        logger.info(
            "%s Instanciado. base_url=%s database=%s database_rep=%s "
            "max_rows=%s pdf_timeout_s=%s key_len=%s",
            _LOG_PREFIX,
            self._base_url,
            self._database,
            self._database_rep,
            self._max_rows,
            self._pdf_timeout_s,
            len(function_key),
        )

    # --------------------------------------------------------------- #
    # API pública: fetch de contratos
    # --------------------------------------------------------------- #
    def fetch_contratos(
        self,
        *,
        cif_proveedor: str,
        codigo_obra_normalizado: str,
    ) -> list[ContratoEnrichmentResult]:
        logger.info(
            "%s fetch_contratos INICIO cif=%s obra=%s",
            _LOG_PREFIX,
            cif_proveedor,
            codigo_obra_normalizado,
        )

        columns, rows = self._post_sql_read(
            sql=_SQL_HEADER_AND_LINES,
            parameters=[cif_proveedor, codigo_obra_normalizado],
            database=self._database,
            label="header_and_lines",
        )
        contrato_ides, results_without_pdf = self._group_rows_by_contrato(
            columns=columns,
            rows=rows,
        )
        logger.info(
            "%s Agrupado cabecera+líneas: contratos=%s total_lineas=%s",
            _LOG_PREFIX,
            len(results_without_pdf),
            sum(len(c.lines) for c in results_without_pdf),
        )

        if not results_without_pdf:
            return []

        enriched: list[ContratoEnrichmentResult] = []
        for contrato, contrato_ide in zip(results_without_pdf, contrato_ides):
            gra_rep_ide = self._safe_fetch_gra_rep_ide(contrato_ide=contrato_ide)
            enriched.append(
                ContratoEnrichmentResult(
                    codigo_contrato=contrato.codigo_contrato,
                    nombre_contrato=contrato.nombre_contrato,
                    fecha_alta_contrato=contrato.fecha_alta_contrato,
                    fecha_contrato=contrato.fecha_contrato,
                    vigencia_desde=contrato.vigencia_desde,
                    vigencia_hasta=contrato.vigencia_hasta,
                    importe_total=contrato.importe_total,
                    cif_proveedor=contrato.cif_proveedor,
                    nombre_proveedor=contrato.nombre_proveedor,
                    codigo_obra=contrato.codigo_obra,
                    nombre_obra=contrato.nombre_obra,
                    gra_rep_ide=gra_rep_ide,
                    pdf_sharepoint_relative_path=None,
                    pdf_sharepoint_web_url=None,
                    sigrid_ide=contrato.sigrid_ide,
                    lines=contrato.lines,
                )
            )

        logger.info(
            "%s fetch_contratos FIN contratos=%s pdfs_encontrados=%s",
            _LOG_PREFIX,
            len(enriched),
            sum(1 for c in enriched if c.gra_rep_ide is not None),
        )
        return enriched

    # --------------------------------------------------------------- #
    # API pública: descarga del binario PDF
    # --------------------------------------------------------------- #
    def download_contrato_pdf(
        self,
        *,
        gra_rep_ide: int,
    ) -> ContratoPdfPayload | None:
        """Devuelve el PDF del contrato (jun 2026).

        - Si ``gra_rep_ide`` tiene un plan de combinación registrado por
          :meth:`_fetch_gra_rep_ide` (hay documentos Word: original +
          ampliaciones), descarga TODOS, extrae su texto y los combina
          (del más antiguo al más moderno) en un ÚNICO PDF generado.
        - En cualquier otro caso (PDF único elegido, o reuso desde caché
          sin plan en memoria) descarga ese documento tal cual, como
          siempre.
        """
        plan = self._combine_plan_by_primary.get(int(gra_rep_ide))
        if plan and plan.get("kind") == "words":
            return self._download_and_combine_words(
                primary_ide=int(gra_rep_ide), docs=plan["docs"]
            )
        if plan and plan.get("kind") == "pdfs":
            return self._download_and_merge_pdfs(
                primary_ide=int(gra_rep_ide), docs=plan["docs"]
            )

        raw = self._download_document_raw(gra_rep_ide=gra_rep_ide)
        if raw is None:
            return None
        filename, content, content_type = raw
        nombre = filename or f"contrato_{gra_rep_ide}.pdf"
        if es_word(nombre):
            ext = ".docx" if nombre.lower().endswith(".docx") else ".doc"
            md = self._word_converter.word_a_markdown(content, ext)
            # Renderizamos el Word a PDF (estructura conservada) para el
            # revisor; si el conversor no puede, dejamos el Word tal cual.
            pdf = self._word_converter.word_a_pdf(content, ext)
            if pdf:
                stem = nombre.rsplit(".", 1)[0]
                content = pdf
                content_type = "application/pdf"
                nombre = f"{stem}.pdf"
        else:
            md = pdf_a_markdown(content)
        return ContratoPdfPayload(
            filename=nombre,
            content=content,
            content_type=content_type,
            markdown=md,
        )

    def _download_and_merge_pdfs(
        self,
        *,
        primary_ide: int,
        docs: list[dict[str, Any]],
    ) -> ContratoPdfPayload | None:
        """Descarga los PDFs del plan y los FUSIONA página a página.

        El orden de ``docs`` ya viene del más antiguo al más moderno.
        Las páginas originales se conservan intactas (tablas de precios
        incluidas), sin conversión intermedia. Si la fusión no es
        posible (pypdf ausente, PDFs ilegibles), degrada al PDF más
        antiguo descargable, que era el comportamiento anterior.
        """
        from infrastructure.documents.pdf_merger import merge_pdfs

        blobs: list[bytes] = []
        first_payload: ContratoPdfPayload | None = None
        for doc in docs:
            rep_ide = int(doc["rep_ide"])
            try:
                raw = self._download_document_raw(gra_rep_ide=rep_ide)
            except Exception:
                logger.exception(
                    "%s MERGE: fallo descargando pdf rep_ide=%s",
                    _LOG_PREFIX,
                    rep_ide,
                )
                continue
            if raw is None:
                logger.warning(
                    "%s MERGE: pdf rep_ide=%s sin contenido.",
                    _LOG_PREFIX,
                    rep_ide,
                )
                continue
            filename, content, content_type = raw
            blobs.append(content)
            if first_payload is None:
                first_payload = ContratoPdfPayload(
                    filename=filename or f"contrato_{rep_ide}.pdf",
                    content=content,
                    content_type=content_type,
                    markdown=pdf_a_markdown(content),
                )

        if not blobs:
            logger.warning(
                "%s MERGE: ningún pdf descargable para primary=%s.",
                _LOG_PREFIX,
                primary_ide,
            )
            return None

        if len(blobs) == 1:
            return first_payload

        merged = merge_pdfs(blobs)
        if merged is None:
            logger.warning(
                "%s MERGE: fusión no disponible; se usa el PDF más "
                "antiguo (primary=%s).",
                _LOG_PREFIX,
                primary_ide,
            )
            return first_payload

        base = str(docs[0].get("name") or f"contrato_{primary_ide}")
        stem = base.rsplit(".", 1)[0]
        filename = f"{stem}_COMBINADO.pdf"
        logger.info(
            "%s MERGE: %s pdf(s) → %s (%s bytes)",
            _LOG_PREFIX,
            len(blobs),
            filename,
            len(merged),
        )
        return ContratoPdfPayload(
            filename=filename,
            content=merged,
            content_type="application/pdf",
            markdown=pdf_a_markdown(merged),
        )

    def _download_and_combine_words(
        self,
        *,
        primary_ide: int,
        docs: list[dict[str, Any]],
    ) -> ContratoPdfPayload | None:
        """Descarga los Word del plan y los combina en UN PDF + Markdown.

        Estrategia (jun 2026) — **LibreOffice** (headless), que lee `.doc`
        y `.docx` reales conservando la estructura (tablas de tarifas
        incluidas). Al contrario que la extracción de texto, que sobre un
        `.doc` binario volcaba la estructura OLE (themes, fuentes, XML)
        como basura, tanto al PDF del revisor como a la IA.

          - Cada Word → PDF (LibreOffice); se fusionan página a página →
            PDF que consulta el revisor, bien estructurado.
          - Cada Word → HTML (LibreOffice) → Markdown (markitdown); las
            tablas salen como tablas Markdown. Es lo que consume la IA.

        Si LibreOffice no está disponible, degrada al render anterior
        (mammoth/xhtml2pdf para .docx; texto plano para .doc).

        El orden de ``docs`` ya viene del más antiguo al más moderno.
        """
        pdf_parts: list[bytes] = []
        md_sections: list[str] = []
        # Material del render ANTIGUO (solo se usa si LibreOffice no está).
        html_sections: list[tuple[str, str]] = []
        text_sections: list[tuple[str, str]] = []
        use_renderer = docx_renderer.renderer_available()
        any_html = False

        for doc in docs:
            rep_ide = int(doc["rep_ide"])
            try:
                raw = self._download_document_raw(gra_rep_ide=rep_ide)
            except Exception:
                logger.exception(
                    "%s COMBINE: fallo descargando word rep_ide=%s",
                    _LOG_PREFIX,
                    rep_ide,
                )
                continue
            if raw is None:
                logger.warning(
                    "%s COMBINE: word rep_ide=%s sin contenido.",
                    _LOG_PREFIX,
                    rep_ide,
                )
                continue
            filename, content, _ctype = raw
            fec = doc.get("fec") or 0
            fec_str = (
                f"{str(fec)[6:8]}/{str(fec)[4:6]}/{str(fec)[0:4]}"
                if fec
                else "sin fecha"
            )
            titulo = f"DOCUMENTO: {doc.get('name') or filename} ({fec_str})"
            ext = ".docx" if (filename or "").lower().endswith(".docx") else ".doc"

            # --- Vía preferente: LibreOffice (PDF con estructura + MD) ---
            pdf_part = self._word_converter.word_a_pdf(content, ext)
            if pdf_part:
                pdf_parts.append(pdf_part)
            md_part = self._word_converter.word_a_markdown(content, ext)
            if md_part:
                md_sections.append(f"## {titulo}\n\n{md_part}")

            # --- Material de fallback (por si LibreOffice no estuviese) ---
            texto = extract_text(filename, content) or "(sin texto)"
            text_sections.append((titulo, texto))
            html_frag = None
            if use_renderer and ext == ".docx":
                html_frag = docx_renderer.docx_to_html_fragment(content)
            if html_frag:
                any_html = True
                html_sections.append((titulo, html_frag))
            else:
                html_sections.append(
                    (titulo, docx_renderer.text_to_html_fragment(texto))
                )

        if not text_sections:
            logger.warning(
                "%s COMBINE: ningún word descargable para primary=%s.",
                _LOG_PREFIX,
                primary_ide,
            )
            return None

        base = str(docs[0].get("name") or f"contrato_{primary_ide}")
        stem = base.rsplit(".", 1)[0]
        filename = f"{stem}_COMBINADO.pdf"

        # PDF: preferimos la fusión de los PDF de LibreOffice (estructura).
        pdf_bytes = combinar_pdfs(pdf_parts) if pdf_parts else None
        if pdf_bytes:
            logger.info(
                "%s COMBINE (libreoffice): %s doc(s) → %s (%s bytes), "
                "estructura conservada.",
                _LOG_PREFIX,
                len(pdf_parts),
                filename,
                len(pdf_bytes),
            )
        else:
            if use_renderer and any_html:
                pdf_bytes = docx_renderer.combine_html_to_pdf(html_sections)
            if not pdf_bytes:
                pdf_bytes = build_text_pdf(text_sections)
            logger.info(
                "%s COMBINE (fallback sin libreoffice): %s sección(es) → %s.",
                _LOG_PREFIX,
                len(text_sections),
                filename,
            )

        markdown = "\n\n---\n\n".join(md_sections).strip() or None
        if markdown:
            logger.info(
                "%s COMBINE: markdown generado (%s chars) para la IA.",
                _LOG_PREFIX,
                len(markdown),
            )

        return ContratoPdfPayload(
            filename=filename,
            content=pdf_bytes,
            content_type="application/pdf",
            markdown=markdown,
        )

    def _download_document_raw(
        self,
        *,
        gra_rep_ide: int,
    ) -> tuple[str, bytes, str | None] | None:
        """Descarga un documento desde ``ruesma_rep.gra`` vía
        /api/documents/read.

        La respuesta es binaria (no JSON). El nombre de fichero viene
        en el header ``X-Document-Filename`` (si el endpoint lo envía;
        en la Function App actual sí lo hace).

        Devuelve ``None`` si:
          - El endpoint responde con body vacío.
          - El ide no existe (404) — se interpreta como "no hay documento".

        Lanza ``RuntimeError`` si hay un error de transporte o 5xx —
        el orquestador decide si continuar con otros contratos o abortar.
        """
        url = f"{self._base_url}/api/documents/read"
        payload = {
            "database": self._database_rep,
            "schema": "dbo",
            "table": "gra",
            "id_column": "ide",
            "id_value": int(gra_rep_ide),
            "blob_column": "ima",
            "filename_columns": ["nomori", "nom"],
            "disposition": "attachment",
        }
        headers = {
            "x-functions-key": self._function_key,
            "Content-Type": "application/json",
        }

        logger.info(
            "%s DOWNLOAD REQUEST -> POST %s gra_rep_ide=%s",
            _LOG_PREFIX,
            url,
            gra_rep_ide,
        )

        try:
            with httpx.Client(timeout=self._pdf_timeout_s) as client:
                response = client.post(url, json=payload, headers=headers)
        except Exception as exc:
            logger.exception(
                "%s DOWNLOAD FALLO de transporte. gra_rep_ide=%s exc=%r",
                _LOG_PREFIX,
                gra_rep_ide,
                exc,
            )
            raise

        status = response.status_code
        content = response.content or b""
        content_type = response.headers.get("Content-Type", "") or None
        filename_header = response.headers.get("X-Document-Filename", "") or ""

        logger.info(
            "%s DOWNLOAD RESPONSE <- status=%s bytes=%s content_type=%s filename=%r",
            _LOG_PREFIX,
            status,
            len(content),
            content_type,
            filename_header,
        )

        if status == 404:
            return None
        if status >= 400:
            # Trunca el body si viniera como error JSON para el log.
            preview = content[:300].decode("utf-8", errors="replace")
            raise RuntimeError(
                f"sigrid-api /documents/read respondió {status}: {preview}"
            )
        if not content:
            return None

        filename = filename_header.strip() or f"documento_{gra_rep_ide}"
        return filename, content, content_type

    # --------------------------------------------------------------- #
    # HTTP primitive para queries SQL
    # --------------------------------------------------------------- #
    def search_proveedores(
        self,
        *,
        max_rows: int = 5000,
    ) -> list[tuple[str | None, str | None]]:
        """Devuelve (cif, nombre) de los proveedores con contrato en la
        empresa Ruesma (emp=1), para que el HeaderResolverService deduzca
        el CIF por nombre cuando la IA no lo fijo. Best-effort.

        jun 2026: el nombre es ahora la razon social CANONICA del
        maestro de proveedores (``prv.raz``), no el snapshot
        desnormalizado ``ctr.entres`` que arrastraba nombres antiguos.
        """
        sql = (
            "SELECT DISTINCT prv.cif AS cif, prv.raz AS nombre "
            "FROM ctr "
            "JOIN con ON ctr.ide = con.ide "
            "JOIN prv ON ctr.entide = prv.ide "
            "WHERE prv.cif IS NOT NULL AND con.emp = 1"
        )
        columns, rows = self._post_sql_read(
            sql=sql,
            parameters=[],
            database=self._database,
            label="search_proveedores",
        )
        out: list[tuple[str | None, str | None]] = []
        for row in rows:
            row_map = dict(zip(columns, row))
            cif = _opt_str(row_map.get("cif"))
            nombre = _opt_str(row_map.get("nombre"))
            if cif:
                out.append((cif, nombre))
        return out

    def fetch_proveedor_by_cif(
        self,
        *,
        cif: str,
    ) -> tuple[str, str | None] | None:
        """Lookup DETERMINISTA por CIF exacto en el maestro ``prv``.

        Normaliza el CIF (mayusculas, sin espacios) en AMBOS lados de la
        comparacion para tolerar variantes tipo "B 12345678".

        Returns
        -------
        tuple[str, str | None] | None
            ``(cif_canonico, razon_social_canonica)`` si el CIF existe
            en Sigrid; ``None`` si no existe. La razon social es
            ``prv.raz`` (fuente de verdad del maestro de proveedores).

        Uso (jun 2026):
          - Grounding de fase 2 (sv7→sv3): si el CIF leido por la 1a IA
            existe, el proveedor queda VALIDADO y NO pasa a revision IA.
          - Refetch del portal (sv4): al confirmar un proveedor que
            existe, su nombre se sobrescribe con el canonico aunque
            Sigrid no devuelva contratos para esa obra.
        """
        cif_clean = (cif or "").strip().upper().replace(" ", "")
        if not cif_clean:
            return None
        sql = (
            "SELECT TOP 1 prv.cif AS cif, prv.raz AS nombre "
            "FROM prv "
            "WHERE REPLACE(UPPER(prv.cif), ' ', '') = ?"
        )
        columns, rows = self._post_sql_read(
            sql=sql,
            parameters=[cif_clean],
            database=self._database,
            label="fetch_proveedor_by_cif",
        )
        if not rows:
            logger.info(
                "%s fetch_proveedor_by_cif: CIF %s NO existe en prv.",
                _LOG_PREFIX,
                cif_clean,
            )
            return None
        row_map = dict(zip(columns, rows[0]))
        cif_canon = _opt_str(row_map.get("cif")) or cif_clean
        nombre = _opt_str(row_map.get("nombre"))
        logger.info(
            "%s fetch_proveedor_by_cif: CIF %s VALIDADO -> %r",
            _LOG_PREFIX,
            cif_clean,
            nombre,
        )
        return cif_canon, nombre

    def fetch_proveedores_por_obra(
        self,
        *,
        codigo_obra: str,
    ) -> list[tuple[str | None, str | None]]:
        """Proveedores (cif, razon social canonica) con contrato en una
        obra concreta. emp=1 (Construcciones Ruesma).

        Usado por el HeaderGroundingService como lista de candidatos
        para la IA de fase 2 cuando el CIF leido no valida pero la obra
        si: el conjunto es corto y de maxima relevancia.
        """
        codigo = (codigo_obra or "").strip()
        if not codigo:
            return []
        sql = (
            "SELECT DISTINCT prv.cif AS cif, prv.raz AS nombre "
            "FROM ctr "
            "JOIN con AS con_ctr ON ctr.ide    = con_ctr.ide "
            "JOIN con AS con_obr ON ctr.obride = con_obr.ide "
            "JOIN prv            ON ctr.entide = prv.ide "
            "WHERE con_obr.cod = ? "
            "  AND con_ctr.emp = 1"
        )
        columns, rows = self._post_sql_read(
            sql=sql,
            parameters=[codigo],
            database=self._database,
            label=f"proveedores_por_obra_{codigo}",
        )
        out: list[tuple[str | None, str | None]] = []
        seen: set[str] = set()
        for row in rows:
            row_map = dict(zip(columns, row))
            cif = _opt_str(row_map.get("cif"))
            if not cif or cif in seen:
                continue
            seen.add(cif)
            out.append((cif, _opt_str(row_map.get("nombre"))))
        return out

    def fetch_proveedores_por_obra(
        self,
        *,
        codigo_obra: str,
    ) -> list[tuple[str | None, str | None]]:
        """Proveedores con contrato en una obra (emp=1), con razon
        social CANONICA (``prv.raz``).

        Usado por el HeaderGroundingService como lista de candidatos
        para que la 2ª IA case el nombre leido cuando el CIF no valido.
        Devuelve tuplas ``(cif, nombre)`` deduplicadas por CIF.
        """
        codigo = (codigo_obra or "").strip()
        if not codigo:
            return []
        sql = (
            "SELECT DISTINCT prv.cif AS cif, prv.raz AS nombre "
            "FROM ctr "
            "JOIN con AS con_ctr ON ctr.ide    = con_ctr.ide "
            "JOIN con AS con_obr ON ctr.obride = con_obr.ide "
            "JOIN prv            ON ctr.entide = prv.ide "
            "WHERE con_obr.cod = ? "
            "  AND con_ctr.emp = 1"
        )
        columns, rows = self._post_sql_read(
            sql=sql,
            parameters=[codigo],
            database=self._database,
            label=f"proveedores_obra_{codigo}",
        )
        seen: set[str] = set()
        out: list[tuple[str | None, str | None]] = []
        for row in rows:
            row_map = dict(zip(columns, row))
            cif = _opt_str(row_map.get("cif"))
            if not cif or cif in seen:
                continue
            seen.add(cif)
            out.append((cif, _opt_str(row_map.get("nombre"))))
        logger.info(
            "%s proveedores_por_obra obra=%s -> %s proveedores",
            _LOG_PREFIX,
            codigo,
            len(out),
        )
        return out

    def _post_sql_read(
        self,
        *,
        sql: str,
        parameters: list[Any],
        database: str,
        label: str,
    ) -> tuple[list[str], list[list[Any]]]:
        url = f"{self._base_url}/api/sql/read"
        payload = {
            "database": database,
            "sql": sql,
            "parameters": parameters,
            "timeout_seconds": int(self._timeout_s),
            "max_rows": self._max_rows,
        }
        headers = {
            "x-functions-key": self._function_key,
            "Content-Type": "application/json",
        }

        logger.info(
            "%s REQUEST [%s] -> POST %s db=%s params=%s",
            _LOG_PREFIX,
            label,
            url,
            database,
            parameters,
        )

        transport = httpx.HTTPTransport(retries=1)
        try:
            with httpx.Client(timeout=self._timeout_s, transport=transport) as client:
                response = client.post(url, json=payload, headers=headers)
        except Exception as exc:
            logger.exception("%s FALLO de transporte [%s]. exc=%r", _LOG_PREFIX, label, exc)
            raise

        status = response.status_code
        body_text = response.text or ""
        logger.info(
            "%s RESPONSE [%s] <- status=%s body_len=%s preview=%s",
            _LOG_PREFIX,
            label,
            status,
            len(body_text),
            body_text[:200],
        )

        if status >= 400:
            raise RuntimeError(f"sigrid-api respondió {status}: {body_text[:500]}")

        try:
            body: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"sigrid-api respuesta no JSON: {body_text[:500]}"
            ) from exc

        if not body.get("ok", False):
            raise RuntimeError(f"sigrid-api devolvió ok=false: {body!r}")

        columns: list[str] = list(body.get("columns") or [])
        rows: list[list[Any]] = list(body.get("rows") or [])
        return columns, rows

    @staticmethod
    def _group_rows_by_contrato(
        *,
        columns: list[str],
        rows: list[list[Any]],
    ) -> tuple[list[int], list[ContratoEnrichmentResult]]:
        buckets: "OrderedDict[str, tuple[dict[str, Any], list[ContratoLineFromSigrid]]]" = (
            OrderedDict()
        )

        for row in rows:
            row_map = dict(zip(columns, row))
            codigo = _opt_str(row_map.get("codigo_contrato"))
            if codigo is None:
                continue

            if codigo not in buckets:
                buckets[codigo] = (row_map, [])

            linea_value = row_map.get("linea")
            if linea_value is None:
                continue

            buckets[codigo][1].append(
                ContratoLineFromSigrid(
                    codigo_contrato=codigo,
                    linea=_opt_int(linea_value),
                    numero_linea=_opt_int(row_map.get("numero_linea")),
                    codigo_producto=_opt_str(row_map.get("codigo_producto")),
                    codigo_alternativo=_opt_str(row_map.get("codigo_alternativo")),
                    unidad_medida=_opt_str(row_map.get("unidad_medida")),
                    descripcion_linea=_opt_str(row_map.get("descripcion_linea")),
                    uds=_opt_float(row_map.get("uds")),
                    cantidad_servida=_opt_float(row_map.get("cantidad_servida")),
                    cantidad_facturada=_opt_float(row_map.get("cantidad_facturada")),
                    pendiente_servir=_opt_float(row_map.get("pendiente_servir")),
                    precio_unitario=_opt_float(row_map.get("precio_unitario")),
                    precio_bruto=_opt_float(row_map.get("precio_bruto")),
                    descuentos=_opt_float(row_map.get("descuentos")),
                    importe_linea=_opt_float(row_map.get("importe_linea")),
                    cuota_iva=_opt_float(row_map.get("cuota_iva")),
                    doc_origen=_opt_str(row_map.get("doc_origen")),
                    codigo_partida=_opt_str(row_map.get("codigo_partida")),
                    descripcion_partida=_opt_str(row_map.get("descripcion_partida")),
                    sigrid_ide=_opt_int(row_map.get("line_ide")),
                )
            )

        contrato_ides: list[int] = []
        results: list[ContratoEnrichmentResult] = []
        for codigo, (header_row, lines) in buckets.items():
            contrato_ide = _opt_int(header_row.get("contrato_ide"))
            contrato_ides.append(contrato_ide if contrato_ide is not None else 0)
            results.append(
                ContratoEnrichmentResult(
                    codigo_contrato=codigo,
                    nombre_contrato=_opt_str(header_row.get("nombre_contrato")),
                    fecha_alta_contrato=_opt_int(header_row.get("fecha_alta_contrato")),
                    fecha_contrato=_opt_int(header_row.get("fecha_contrato")),
                    vigencia_desde=_opt_int(header_row.get("vigencia_desde")),
                    vigencia_hasta=_opt_int(header_row.get("vigencia_hasta")),
                    importe_total=_opt_float(header_row.get("importe_total")),
                    cif_proveedor=_opt_str(header_row.get("cif_proveedor")),
                    nombre_proveedor=_opt_str(header_row.get("nombre_proveedor")),
                    codigo_obra=_opt_str(header_row.get("codigo_obra")),
                    nombre_obra=_opt_str(header_row.get("nombre_obra")),
                    gra_rep_ide=None,
                    pdf_sharepoint_relative_path=None,
                    pdf_sharepoint_web_url=None,
                    sigrid_ide=contrato_ide,
                    lines=lines,
                )
            )
        return contrato_ides, results

    def _safe_fetch_gra_rep_ide(self, *, contrato_ide: int) -> int | None:
        if contrato_ide == 0:
            return None
        try:
            return self._fetch_gra_rep_ide(contrato_ide=contrato_ide)
        except Exception as exc:
            logger.warning(
                "%s PDF lookup falló para contrato_ide=%s. exc=%r",
                _LOG_PREFIX,
                contrato_ide,
                exc,
            )
            return None

    def _fetch_gra_rep_ide(self, *, contrato_ide: int) -> int | None:
        """Selecciona y resuelve los documentos del contrato (jun 2026).

        Aplica :func:`_select_contract_docs` sobre los documentos
        relacionados (rcg→gra) y resuelve cada uno en ``ruesma_rep``.
        Devuelve el ``gra_rep_ide`` PRINCIPAL (el Word más antiguo o el
        PDF elegido) y, si hay varios Word, registra el plan completo en
        ``self._combine_plan_by_primary`` para que
        :meth:`download_contrato_pdf` los combine en un único PDF.
        """
        cols, rows = self._post_sql_read(
            sql=_SQL_GRA_COD_BY_CONTRATO,
            parameters=[contrato_ide],
            database=self._database,
            label=f"rcg_gra_for_ctr_{contrato_ide}",
        )

        docs: list[dict[str, Any]] = []
        for row in rows:
            row_map = dict(zip(cols, row))
            cod = _opt_str(row_map.get("gra_cod"))
            if cod is None:
                continue
            name = (
                _opt_str(row_map.get("gra_nomori"))
                or _opt_str(row_map.get("gra_nom"))
                or ""
            )
            docs.append(
                {
                    "cod": cod,
                    "name": name,
                    "fec": _opt_int(row_map.get("gra_fec")) or 0,
                    "pos": _opt_int(row_map.get("rcg_pos")) or 0,
                }
            )

        kind, selected = _select_contract_docs(docs)
        logger.info(
            "%s contrato_ide=%s selección documentos: kind=%s -> %s",
            _LOG_PREFIX,
            contrato_ide,
            kind,
            [d["name"] for d in selected],
        )
        if not selected:
            logger.warning(
                "%s contrato_ide=%s SIN documento de contrato válido "
                "(docs=%s). No se descargará PDF.",
                _LOG_PREFIX,
                contrato_ide,
                [d["name"] for d in docs],
            )
            return None

        # Resolver cada cod en ruesma_rep (donde vive el binario).
        resolved: list[dict[str, Any]] = []
        for doc in selected:
            rep_ide = self._resolve_rep_ide(cod=doc["cod"])
            if rep_ide is None:
                logger.warning(
                    "%s contrato_ide=%s doc %r (cod=%s) sin fila en rep.",
                    _LOG_PREFIX,
                    contrato_ide,
                    doc["name"],
                    doc["cod"],
                )
                continue
            resolved.append({**doc, "rep_ide": rep_ide})

        if not resolved:
            return None

        primary = int(resolved[0]["rep_ide"])
        if kind == "words" or (kind == "pdfs" and len(resolved) > 1):
            self._combine_plan_by_primary[primary] = {
                "kind": kind,
                "docs": resolved,
            }
        logger.info(
            "%s contrato_ide=%s → gra_rep_ide=%s (kind=%s, %s doc/s)",
            _LOG_PREFIX,
            contrato_ide,
            primary,
            kind,
            len(resolved),
        )
        return primary

    def _resolve_rep_ide(self, *, cod: str) -> int | None:
        """Resuelve un ``gra.cod`` de ruesma en su ide de ruesma_rep."""
        cols_rep, rows_rep = self._post_sql_read(
            sql=_SQL_GRA_REP_BY_COD,
            parameters=[cod],
            database=self._database_rep,
            label=f"gra_rep_for_cod_{cod}",
        )
        for row in rows_rep:
            row_map = dict(zip(cols_rep, row))
            gra_rep_ide = _opt_int(row_map.get("gra_rep_ide"))
            if gra_rep_ide is not None:
                return gra_rep_ide
        return None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
