# application/services/header_resolver_service.py
from __future__ import annotations

import logging
import unicodedata

from application.services.obra_code_normalizer import normalize_obra_code
from domain.models.obra_models import ObraEnrichmentResult
from domain.ports.header_resolver_ports import (
    HeaderMergeRepository,
    ObraReverseLookupClient,
    ProveedorReverseLookupClient,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[header-resolver]"


def _norm(value: str | None) -> str:
    """minusculas, sin acentos, espacios colapsados."""
    s = (value or "").lower()
    nfkd = unicodedata.normalize("NFD", s)
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_.split())


def _match_score(q_norm: str, cand_norm: str) -> float:
    """Mismo criterio que el desplegable de sv4:
    1.0 si uno contiene al otro; si no, fraccion de tokens (>=3 letras)
    de la consulta presentes en el candidato.
    """
    if not q_norm or not cand_norm:
        return 0.0
    if cand_norm.find(q_norm) != -1 or q_norm.find(cand_norm) != -1:
        return 1.0
    toks = [t for t in q_norm.split() if len(t) >= 3]
    if not toks:
        return 0.0
    hits = sum(1 for t in toks if t in cand_norm)
    return hits / len(toks)


class HeaderResolverService:
    """Resolucion DETERMINISTA de cabecera por coincidencia de texto.

    Cuando la 1a fase IA no fijo ``obra_codigo`` y/o ``proveedor_cif``,
    los deduce buscando en Sigrid por NOMBRE de obra + DIRECCION de obra
    (obra) y por NOMBRE de proveedor (CIF), con el mismo scoring que el
    desplegable de sv4 (umbral >= ``min_score``).

    Lo resuelto se persiste en el merge marcado como ``deterministic``
    (vs ``ia`` para lo que ya trajo la IA), para que la futura
    "valoracion de confianza" sepa el origen de cada dato.

    Best-effort: cualquier fallo se loguea y NO rompe el pipeline.
    """

    def __init__(
        self,
        *,
        obra_client: ObraReverseLookupClient,
        proveedor_client: ProveedorReverseLookupClient,
        repository: HeaderMergeRepository,
        min_score: float = 0.5,
        enabled: bool = True,
    ) -> None:
        self._obra_client = obra_client
        self._proveedor_client = proveedor_client
        self._repository = repository
        self._min_score = float(min_score)
        self._enabled = enabled
        logger.info(
            "%s INSTANCIADO (enabled=%s min_score=%s)",
            _LOG_PREFIX, enabled, self._min_score,
        )

    def resolve_merge_document(self, *, merge_document_id: str) -> None:
        if not self._enabled:
            logger.info("%s DESHABILITADO. document_id=%s",
                        _LOG_PREFIX, merge_document_id)
            return

        hdr = self._repository.get_merge_header_for_resolution(
            document_id=merge_document_id,
        )
        if hdr is None:
            logger.warning("%s sin cabecera. document_id=%s",
                           _LOG_PREFIX, merge_document_id)
            return

        obra_codigo_det = self._resolve_obra(hdr.obra_codigo,
                                             hdr.obra_nombre,
                                             hdr.obra_direccion)
        proveedor_cif_det = self._resolve_proveedor(hdr.proveedor_cif,
                                                    hdr.proveedor_nombre)

        # Siempre llamamos al repo: ademas de escribir lo resuelto,
        # marca origen='ia' cuando el dato ya venia de la IA (origen NULL).
        try:
            self._repository.update_merge_resolved_header(
                document_id=merge_document_id,
                obra_codigo_det=obra_codigo_det,
                proveedor_cif_det=proveedor_cif_det,
            )
        except Exception:
            logger.exception(
                "%s error persistiendo resolucion. document_id=%s",
                _LOG_PREFIX, merge_document_id,
            )
            return

        logger.info(
            "%s OK document_id=%s obra_codigo_det=%r proveedor_cif_det=%r",
            _LOG_PREFIX, merge_document_id,
            obra_codigo_det, proveedor_cif_det,
        )

    # ----------------------------------------------------------------- #
    def _resolve_obra(
        self,
        obra_codigo: str | None,
        obra_nombre: str | None,
        obra_direccion: str | None,
    ) -> str | None:
        """Devuelve el codigo de obra deducido por texto, o None si ya
        habia codigo valido / no hay texto / no llega al umbral."""
        if obra_codigo and normalize_obra_code(obra_codigo) is not None:
            return None  # la IA ya trajo un codigo valido
        textos = [t for t in (obra_nombre, obra_direccion) if (t or "").strip()]
        if not textos:
            return None
        try:
            candidatos = self._obra_client.search_obras()
        except Exception:
            logger.exception("%s search_obras fallo.", _LOG_PREFIX)
            return None
        if not candidatos:
            return None

        queries = [_norm(t) for t in textos]
        best: ObraEnrichmentResult | None = None
        best_score = 0.0
        for cand in candidatos:
            cand_text = _norm(
                " ".join(
                    p for p in (cand.nombre_obra, cand.direccion_completa) if p
                )
            )
            score = max((_match_score(q, cand_text) for q in queries),
                        default=0.0)
            if score > best_score:
                best_score = score
                best = cand
        if best is not None and best_score >= self._min_score:
            logger.info(
                "%s obra resuelta por texto: codigo=%s score=%.2f",
                _LOG_PREFIX, best.codigo_obra, best_score,
            )
            return (best.codigo_obra or "").strip() or None
        logger.info(
            "%s obra NO resuelta (mejor score=%.2f < %.2f).",
            _LOG_PREFIX, best_score, self._min_score,
        )
        return None

    def _resolve_proveedor(
        self,
        proveedor_cif: str | None,
        proveedor_nombre: str | None,
    ) -> str | None:
        if (proveedor_cif or "").strip():
            return None  # la IA ya trajo CIF
        if not (proveedor_nombre or "").strip():
            return None
        try:
            proveedores = self._proveedor_client.search_proveedores()
        except Exception:
            logger.exception("%s search_proveedores fallo.", _LOG_PREFIX)
            return None
        if not proveedores:
            return None

        q = _norm(proveedor_nombre)
        best_cif: str | None = None
        best_score = 0.0
        for cif, nombre in proveedores:
            score = _match_score(q, _norm(nombre))
            if score > best_score:
                best_score = score
                best_cif = cif
        if best_cif and best_score >= self._min_score:
            logger.info(
                "%s proveedor resuelto por texto: cif=%s score=%.2f",
                _LOG_PREFIX, best_cif, best_score,
            )
            return best_cif.strip() or None
        logger.info(
            "%s proveedor NO resuelto (mejor score=%.2f < %.2f).",
            _LOG_PREFIX, best_score, self._min_score,
        )
        return None
