# application/services/header_resolver_service.py
from __future__ import annotations

import logging
import unicodedata

from application.services.familia_detector import (
    familias_de_filas_merge,
    familias_de_texto,
)
from application.services.obra_code_normalizer import normalize_obra_code
from domain.models.header_resolution_models import ProveedorObraResumen
from domain.models.obra_models import ObraEnrichmentResult
from domain.ports.header_resolver_ports import (
    HeaderMergeRepository,
    ObraReverseLookupClient,
    ProveedorReverseLookupClient,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[header-resolver]"

# Prefijo del aviso de proveedor en review_notes. Se usa tambien para
# RETIRAR el aviso obsoleto (idempotencia en re-enriquecimientos y cuando
# el CIF ya quedo resuelto por el revisor).
_NOTA_PROVEEDOR_PREFIX = "[AVISO] Proveedor"

# ------------------------------------------------------------------ #
# Scoring por obra+familia (jul 2026). Mismo espiritu que el selector
# determinista de contratos: el ganador necesita un minimo de puntos y
# un margen claro sobre el segundo; si no, decide el humano.
# ------------------------------------------------------------------ #
_PUNTOS_MINIMOS = 3
_MARGEN_MINIMO = 2
# El score de nombre (0..1) aporta hasta 4 puntos (nombre exacto = 4).
_PUNTOS_NOMBRE_MAX = 4
# Cada familia comun albaran<->contratos del proveedor aporta 3 puntos.
_PUNTOS_POR_FAMILIA = 3
# Penalizacion si el proveedor tiene familias claras y NINGUNA coincide.
_PENALIZACION_OTRA_FAMILIA = 2

# Origen del CIF cuando la deduccion vino por familia+obra (nombre por
# debajo del umbral). OJO: la columna proveedor_cif_origen es VARCHAR(24);
# el valor debe caber. sv4 lo penaliza en la confianza (factor 0.5).
ORIGEN_DET_FAMILIA_OBRA = "det_familia_obra"


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


class _CandidatoObra:
    """Candidato puntuado (proveedor con contrato en la obra)."""

    __slots__ = ("cif", "nombre", "familias", "score_nombre", "puntos")

    def __init__(
        self,
        *,
        cif: str,
        nombre: str | None,
        familias: set[str],
        score_nombre: float,
        puntos: int,
    ) -> None:
        self.cif = cif
        self.nombre = nombre
        self.familias = familias
        self.score_nombre = score_nombre
        self.puntos = puntos

    def etiqueta(self) -> str:
        fams = ",".join(sorted(self.familias)) or "-"
        return f"{self.cif} — {self.nombre or '?'} ({fams})"


class HeaderResolverService:
    """Resolucion DETERMINISTA de cabecera.

    Cuando la 1a fase IA no fijo ``obra_codigo`` y/o ``proveedor_cif``,
    los deduce:

      - OBRA: por NOMBRE + DIRECCION contra Sigrid (igual que siempre).
      - PROVEEDOR, en dos pasos (jul 2026):
          1) Proveedores CON CONTRATO en la obra efectiva (la de la IA o
             la recien deducida): se puntua NOMBRE (hasta 4 puntos) +
             FAMILIA de producto del albaran vs familia de sus contratos
             (+3 por familia comun; -2 si contradice). Con senal fuerte y
             unica se fija el CIF:
               * si el nombre supera el umbral -> origen 'deterministic'
                 (comportamiento clasico, factor 0.6 en confianza sv4);
               * si gana por familia (nombre debil) -> origen
                 'det_familia_obra' + AVISO en review_notes (sv4 lo
                 penaliza con factor 0.5).
             Si hay ambiguedad: NO se fija y se deja un AVISO con los
             candidatos de la obra para que el revisor elija rapido.
          2) Fallback global por nombre contra todos los proveedores con
             contrato (comportamiento previo, umbral ``min_score``).

    Lo resuelto se persiste en el merge marcado con su origen
    (``deterministic`` / ``det_familia_obra`` vs ``ia`` para lo que ya
    trajo la IA), para que la valoracion de confianza sepa de donde
    salio cada dato.

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
        familia_enabled: bool = True,
        nota_max_candidatos: int = 5,
    ) -> None:
        self._obra_client = obra_client
        self._proveedor_client = proveedor_client
        self._repository = repository
        self._min_score = float(min_score)
        self._enabled = enabled
        self._familia_enabled = bool(familia_enabled)
        self._nota_max_candidatos = max(1, int(nota_max_candidatos))
        logger.info(
            "%s INSTANCIADO (enabled=%s min_score=%s familia=%s)",
            _LOG_PREFIX, enabled, self._min_score, self._familia_enabled,
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
        # Obra EFECTIVA para el paso de proveedor: la recien deducida o
        # la que ya trajo la IA (si normaliza).
        obra_efectiva = (
            obra_codigo_det or normalize_obra_code(hdr.obra_codigo)
        )
        proveedor_cif_det, proveedor_origen = self._resolve_proveedor(
            proveedor_cif=hdr.proveedor_cif,
            proveedor_nombre=hdr.proveedor_nombre,
            obra_codigo_efectiva=obra_efectiva,
            merge_document_id=merge_document_id,
        )

        # Siempre llamamos al repo: ademas de escribir lo resuelto,
        # marca origen='ia' cuando el dato ya venia de la IA (origen NULL).
        try:
            self._repository.update_merge_resolved_header(
                document_id=merge_document_id,
                obra_codigo_det=obra_codigo_det,
                proveedor_cif_det=proveedor_cif_det,
                proveedor_origen=proveedor_origen,
            )
        except Exception:
            logger.exception(
                "%s error persistiendo resolucion. document_id=%s",
                _LOG_PREFIX, merge_document_id,
            )
            return

        logger.info(
            "%s OK document_id=%s obra_codigo_det=%r proveedor_cif_det=%r "
            "origen=%s",
            _LOG_PREFIX, merge_document_id,
            obra_codigo_det, proveedor_cif_det,
            proveedor_origen if proveedor_cif_det else "-",
        )

    # ----------------------------------------------------------------- #
    # OBRA
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

    # ----------------------------------------------------------------- #
    # PROVEEDOR
    # ----------------------------------------------------------------- #
    def _resolve_proveedor(
        self,
        *,
        proveedor_cif: str | None,
        proveedor_nombre: str | None,
        obra_codigo_efectiva: str | None,
        merge_document_id: str,
    ) -> tuple[str | None, str]:
        """Devuelve ``(cif_deducido | None, origen)``.

        ``origen`` solo es relevante cuando hay CIF deducido:
        'deterministic' (nombre supera umbral) o 'det_familia_obra'
        (decidio la familia con nombre debil).
        """
        if (proveedor_cif or "").strip():
            # CIF ya presente (IA o revisor): si quedo un aviso de
            # candidatos de un intento anterior, se retira para no
            # contradecir lo que ve el revisor.
            self._remove_nota_proveedor_safely(merge_document_id)
            return None, "deterministic"

        # Paso 1 (jul 2026) — candidatos con contrato en la obra,
        # puntuados por nombre + familia de producto.
        if self._familia_enabled and obra_codigo_efectiva:
            cif, origen = self._resolver_por_obra_y_familia(
                proveedor_nombre=proveedor_nombre,
                obra_codigo=obra_codigo_efectiva,
                merge_document_id=merge_document_id,
            )
            if cif:
                return cif, origen

        # Paso 2 — fallback global por nombre (comportamiento clasico).
        if not (proveedor_nombre or "").strip():
            return None, "deterministic"
        try:
            proveedores = self._proveedor_client.search_proveedores()
        except Exception:
            logger.exception("%s search_proveedores fallo.", _LOG_PREFIX)
            return None, "deterministic"
        if not proveedores:
            return None, "deterministic"

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
                "%s proveedor resuelto por texto (global): cif=%s score=%.2f",
                _LOG_PREFIX, best_cif, best_score,
            )
            self._remove_nota_proveedor_safely(merge_document_id)
            return (best_cif.strip() or None), "deterministic"
        logger.info(
            "%s proveedor NO resuelto (mejor score global=%.2f < %.2f).",
            _LOG_PREFIX, best_score, self._min_score,
        )
        return None, "deterministic"

    def _resolver_por_obra_y_familia(
        self,
        *,
        proveedor_nombre: str | None,
        obra_codigo: str,
        merge_document_id: str,
    ) -> tuple[str | None, str]:
        """Puntua los proveedores CON CONTRATO en la obra y decide.

        Devuelve ``(cif, origen)`` si hay ganador claro; ``(None, ...)``
        si no (y en ese caso deja un AVISO con los candidatos si aporta
        informacion util al revisor).
        """
        try:
            resumenes: list[ProveedorObraResumen] = (
                self._proveedor_client.fetch_contratos_resumen_por_obra(
                    codigo_obra=obra_codigo,
                )
            )
        except Exception:
            logger.exception(
                "%s fetch_contratos_resumen_por_obra fallo obra=%s.",
                _LOG_PREFIX, obra_codigo,
            )
            return None, "deterministic"
        if not resumenes:
            logger.info(
                "%s obra %s sin proveedores con contrato; paso familia "
                "omitido.", _LOG_PREFIX, obra_codigo,
            )
            return None, "deterministic"

        familias_alb: set[str] = set()
        try:
            filas = self._repository.get_merge_lines_for_scoring(
                document_id=merge_document_id,
            )
            familias_alb = familias_de_filas_merge(filas)
        except Exception:
            logger.exception(
                "%s no se pudieron leer lineas para familia; se sigue "
                "solo con nombre.", _LOG_PREFIX,
            )

        q = _norm(proveedor_nombre)
        candidatos: list[_CandidatoObra] = []
        for r in resumenes:
            fams_prov = familias_de_texto((r.texto or "").lower())
            score_nombre = _match_score(q, _norm(r.nombre)) if q else 0.0
            comunes = familias_alb & fams_prov
            puntos = (
                round(_PUNTOS_NOMBRE_MAX * score_nombre)
                + _PUNTOS_POR_FAMILIA * len(comunes)
            )
            if familias_alb and fams_prov and not comunes:
                puntos -= _PENALIZACION_OTRA_FAMILIA
            candidatos.append(_CandidatoObra(
                cif=r.cif,
                nombre=r.nombre,
                familias=fams_prov,
                score_nombre=score_nombre,
                puntos=puntos,
            ))
            logger.info(
                "%s candidato obra=%s %s -> %s puntos "
                "(nombre=%.2f familias=%s comunes=%s)",
                _LOG_PREFIX, obra_codigo, r.cif, puntos,
                score_nombre, sorted(fams_prov) or "-",
                sorted(comunes) or "-",
            )

        # Atajo por NOMBRE dentro de la obra: si algun candidato supera el
        # umbral clasico, el nombre manda (origen 'deterministic').
        por_nombre = max(candidatos, key=lambda c: c.score_nombre)
        if por_nombre.score_nombre >= self._min_score:
            logger.info(
                "%s proveedor resuelto por NOMBRE dentro de la obra %s: "
                "cif=%s score=%.2f",
                _LOG_PREFIX, obra_codigo, por_nombre.cif,
                por_nombre.score_nombre,
            )
            self._remove_nota_proveedor_safely(merge_document_id)
            return por_nombre.cif, "deterministic"

        # Decision por PUNTOS (nombre debil + familia).
        candidatos.sort(key=lambda c: c.puntos, reverse=True)
        mejor = candidatos[0]
        segundo_puntos = candidatos[1].puntos if len(candidatos) > 1 else -999

        if (
            mejor.puntos >= _PUNTOS_MINIMOS
            and (mejor.puntos - segundo_puntos) >= _MARGEN_MINIMO
        ):
            logger.warning(
                "%s proveedor %s DEDUCIDO por familia+obra (%s puntos, "
                "segundo=%s, nombre=%.2f) obra=%s -> origen=%s",
                _LOG_PREFIX, mejor.cif, mejor.puntos, segundo_puntos,
                mejor.score_nombre, obra_codigo, ORIGEN_DET_FAMILIA_OBRA,
            )
            self._nota_proveedor_safely(
                merge_document_id,
                nota=(
                    f"{_NOTA_PROVEEDOR_PREFIX} {mejor.cif} — "
                    f"{mejor.nombre or '?'} deducido AUTOMATICAMENTE por "
                    "familia de producto "
                    f"({','.join(sorted(mejor.familias & familias_alb)) or '-'}) "
                    f"entre {len(candidatos)} proveedor(es) con contrato "
                    f"en la obra {obra_codigo} "
                    f"(nombre leido: '{proveedor_nombre or '—'}'). "
                    "Verificar que es el correcto."
                ),
            )
            return mejor.cif, ORIGEN_DET_FAMILIA_OBRA

        # Ambiguo o sin senal suficiente: NO se fija, pero si hay algo que
        # contar (candidatos en la obra), se deja el AVISO con el top-N
        # para que el revisor elija rapido en el portal.
        logger.info(
            "%s proveedor NO deducido por familia+obra (mejor=%s puntos, "
            "segundo=%s, minimo=%s, margen=%s).",
            _LOG_PREFIX, mejor.puntos, segundo_puntos,
            _PUNTOS_MINIMOS, _MARGEN_MINIMO,
        )
        top = candidatos[: self._nota_max_candidatos]
        listado = "; ".join(c.etiqueta() for c in top)
        self._nota_proveedor_safely(
            merge_document_id,
            nota=(
                f"{_NOTA_PROVEEDOR_PREFIX} sin CIF: no deducible con "
                f"seguridad. Candidatos con contrato en la obra "
                f"{obra_codigo}: {listado}. Corrige el proveedor arriba "
                "y pulsa \"Guardar y volver a buscar\"."
            ),
        )
        return None, "deterministic"

    # ----------------------------------------------------------------- #
    # Notas de revision (best-effort, con dedupe por prefijo)
    # ----------------------------------------------------------------- #
    def _nota_proveedor_safely(self, merge_document_id: str, *, nota: str) -> None:
        try:
            self._repository.remove_review_note_prefix(
                document_id=merge_document_id,
                prefijo=_NOTA_PROVEEDOR_PREFIX,
            )
            self._repository.append_review_note(
                document_id=merge_document_id,
                nota=nota,
            )
        except Exception:  # noqa: BLE001 — best-effort
            logger.exception(
                "%s no se pudo dejar la nota de proveedor. document_id=%s",
                _LOG_PREFIX, merge_document_id,
            )

    def _remove_nota_proveedor_safely(self, merge_document_id: str) -> None:
        try:
            self._repository.remove_review_note_prefix(
                document_id=merge_document_id,
                prefijo=_NOTA_PROVEEDOR_PREFIX,
            )
        except Exception:  # noqa: BLE001 — best-effort
            logger.exception(
                "%s no se pudo retirar el aviso de proveedor. "
                "document_id=%s", _LOG_PREFIX, merge_document_id,
            )
