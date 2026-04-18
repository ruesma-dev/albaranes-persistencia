# application/services/albaran_confidence_service.py
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Any

from domain.models.extraction_models import (
    CabeceraAlbaran,
    DocumentoAlbaran,
    ExtractionEnvelope,
    LineaAlbaran,
    ProviderExtractionEnvelope,
)


@dataclass(frozen=True)
class LineMergeResult:
    merged_line: LineaAlbaran
    provider_origin: str
    source_openai_index: int | None
    source_gemini_index: int | None
    source_claude_index: int | None
    raw_openai_confidence_pct: float | None
    confidence_pct_calc: float
    line_match_score: float | None
    comparison_status: dict[str, Any]
    field_scores: dict[str, Any]


@dataclass(frozen=True)
class MergeAnalysis:
    merged_envelope: ProviderExtractionEnvelope
    provider_origin: str
    document_confidence_pct: float
    openai_raw_confidence_pct: float | None
    review_required: bool
    review_reasons: list[str]
    comparison_summary: dict[str, Any]
    line_results: list[LineMergeResult]


_HEADER_CONFIGS: dict[str, dict[str, Any]] = {
    "proveedor_nombre": {"weight": 18, "required": True, "critical": True, "kind": "text"},
    "proveedor_cif": {"weight": 10, "required": False, "critical": False, "kind": "cif"},
    "fecha": {"weight": 14, "required": True, "critical": True, "kind": "date"},
    "numero_albaran": {"weight": 18, "required": True, "critical": True, "kind": "identifier"},
    "forma_pago": {"weight": 4, "required": False, "critical": False, "kind": "text"},
    "obra_codigo": {"weight": 6, "required": False, "critical": False, "kind": "identifier"},
    "obra_nombre": {"weight": 14, "required": False, "critical": False, "kind": "text"},
    "obra_direccion": {"weight": 4, "required": False, "critical": False, "kind": "text"},
}

_LINE_CONFIGS: dict[str, dict[str, Any]] = {
    "codigo": {"weight": 5, "required": False, "critical": False, "kind": "identifier"},
    "cantidad": {"weight": 15, "required": True, "critical": True, "kind": "number", "tolerance": 0.05},
    "concepto": {"weight": 25, "required": True, "critical": True, "kind": "text"},
    "precio": {"weight": 10, "required": False, "critical": False, "kind": "number", "tolerance": 0.05},
    "descuento": {"weight": 5, "required": False, "critical": False, "kind": "number", "tolerance": 0.2},
    "precio_neto": {"weight": 20, "required": True, "critical": True, "kind": "number", "tolerance": 0.15},
    "codigo_imputacion": {"weight": 20, "required": False, "critical": True, "kind": "identifier"},
}

# Puntuación por field-status. Con 3 fuentes, los casos "match" se
# refuerzan cuando hay coincidencia triple y se debilitan cuando hay
# disenso parcial.
_FIELD_SCORE_BY_STATUS: dict[str, float] = {
    "triple_match": 98.0,
    "match_exact": 96.0,
    "match_normalized": 92.0,
    "match_tolerant": 88.0,
    "majority_match": 86.0,
    "openai_only": 76.0,
    "gemini_only": 66.0,
    "claude_only": 66.0,
    "both_empty_required": 20.0,
    "both_empty_optional": math.nan,
    "conflict": 28.0,
    "invalid": 10.0,
}


class AlbaranConfidenceService:
    # ---------------------------------------------------------------- #
    # Entry-point                                                      #
    # ---------------------------------------------------------------- #

    def build_merge_analysis(
        self,
        *,
        openai: ExtractionEnvelope,
        gemini: ProviderExtractionEnvelope | None,
        claude: ProviderExtractionEnvelope | None = None,
    ) -> MergeAnalysis:
        gemini_has_meaningful_lines = (
            gemini is not None
            and self._has_meaningful_lines(gemini.data.lineas)
        )
        claude_has_meaningful_lines = (
            claude is not None
            and self._has_meaningful_lines(claude.data.lineas)
        )

        if gemini is not None:
            # Caso 1: Gemini presente → base = gemini (como antes),
            # con coalesce triple gemini → openai → claude en cada campo.
            header = self._merge_header_triple(
                primary=gemini.data.cabecera,
                secondary=openai.data.cabecera,
                tertiary=claude.data.cabecera if claude is not None else None,
            )
            (
                line_results,
                matched_openai_indices,
                matched_claude_indices,
            ) = self._build_merged_lines_gemini_primary(
                gemini_lines=gemini.data.lineas,
                openai_lines=openai.data.lineas,
                claude_lines=claude.data.lineas if claude is not None else [],
                append_unmatched_openai=not gemini_has_meaningful_lines,
            )
            provider_origin = "gemini_filled"
            base_envelope = gemini

        elif claude is not None and claude_has_meaningful_lines:
            # Caso 2: No hay Gemini pero sí Claude útil. Usamos OpenAI como
            # base (existe siempre) y rellenamos con Claude.
            header = self._merge_header_triple(
                primary=openai.data.cabecera,
                secondary=claude.data.cabecera,
                tertiary=None,
            )
            line_results = self._build_merged_lines_openai_primary(
                openai_lines=openai.data.lineas,
                claude_lines=claude.data.lineas,
            )
            provider_origin = "openai_filled"
            base_envelope = openai

        else:
            # Caso 3: Solo OpenAI (o claude sin líneas útiles).
            header = openai.data.cabecera
            line_results = self._build_openai_only_lines(openai.data.lineas)
            provider_origin = "openai_fallback"
            base_envelope = openai

        merged_document = DocumentoAlbaran(
            cabecera=header,
            lineas=[item.merged_line for item in line_results],
        )
        merged_envelope = ProviderExtractionEnvelope(
            meta=base_envelope.meta,
            data=merged_document,
            debug=base_envelope.debug,
        )

        header_summary = self._score_header(
            openai_header=openai.data.cabecera,
            gemini_header=gemini.data.cabecera if gemini is not None else None,
            claude_header=claude.data.cabecera if claude is not None else None,
            merged_header=merged_document.cabecera,
        )
        lines_summary = self._score_document_lines(line_results)
        openai_raw_conf = self._safe_average(
            [line.confianza_pct for line in openai.data.lineas]
        )
        coherence_score, coherence_flags = self._compute_coherence_score(
            merged_document=merged_document,
            header_summary=header_summary,
            line_results=line_results,
        )

        doc_rule_score = self._weighted_average(
            values=[
                (header_summary["score"], 0.35),
                (lines_summary["score"], 0.50),
                (coherence_score, 0.15),
            ]
        )
        raw_signal = openai_raw_conf if openai_raw_conf is not None else 75.0
        doc_conf = 0.75 * doc_rule_score + 0.25 * raw_signal

        caps: list[dict[str, Any]] = []
        doc_conf = self._apply_document_caps(
            document_confidence=doc_conf,
            provider_origin=provider_origin,
            header_summary=header_summary,
            line_results=line_results,
            coherence_flags=coherence_flags,
            caps=caps,
        )
        doc_conf = round(max(0.0, min(100.0, doc_conf)), 2)

        review_reasons = self._build_review_reasons(
            document_confidence=doc_conf,
            header_summary=header_summary,
            line_results=line_results,
            coherence_flags=coherence_flags,
            provider_origin=provider_origin,
        )
        review_required = doc_conf < 80.0 or bool(review_reasons)

        comparison_summary = {
            "version": "comparison-v2",
            "provider_origin": provider_origin,
            "providers_available": {
                "openai": True,
                "gemini": gemini is not None,
                "claude": claude is not None,
            },
            "document_confidence_pct": doc_conf,
            "openai_raw_confidence_pct": openai_raw_conf,
            "header": header_summary,
            "lines": [
                {
                    "merge_line_index": index + 1,
                    "provider_origin": result.provider_origin,
                    "source_openai_index": result.source_openai_index,
                    "source_gemini_index": result.source_gemini_index,
                    "source_claude_index": result.source_claude_index,
                    "raw_openai_confidence_pct": result.raw_openai_confidence_pct,
                    "confidence_pct_calc": result.confidence_pct_calc,
                    "line_match_score": result.line_match_score,
                    "comparison_status": result.comparison_status,
                    "field_scores": result.field_scores,
                }
                for index, result in enumerate(line_results)
            ],
            "coherence": {
                "score": coherence_score,
                "flags": coherence_flags,
            },
            "caps_applied": caps,
            "review_reasons": review_reasons,
            "review_required": review_required,
        }

        return MergeAnalysis(
            merged_envelope=merged_envelope,
            provider_origin=provider_origin,
            document_confidence_pct=doc_conf,
            openai_raw_confidence_pct=openai_raw_conf,
            review_required=review_required,
            review_reasons=review_reasons,
            comparison_summary=comparison_summary,
            line_results=line_results,
        )

    # ---------------------------------------------------------------- #
    # Header scoring                                                   #
    # ---------------------------------------------------------------- #

    def _score_header(
        self,
        *,
        openai_header: CabeceraAlbaran,
        gemini_header: CabeceraAlbaran | None,
        claude_header: CabeceraAlbaran | None,
        merged_header: CabeceraAlbaran,
    ) -> dict[str, Any]:
        field_results: dict[str, dict[str, Any]] = {}
        weighted_values: list[tuple[float, float]] = []
        critical_conflicts: list[str] = []
        required_missing: list[str] = []

        for field_name, config in _HEADER_CONFIGS.items():
            result = self._score_field(
                field_name=field_name,
                openai_value=getattr(openai_header, field_name),
                gemini_value=(
                    getattr(gemini_header, field_name)
                    if gemini_header is not None
                    else None
                ),
                claude_value=(
                    getattr(claude_header, field_name)
                    if claude_header is not None
                    else None
                ),
                merged_value=getattr(merged_header, field_name),
                config=config,
                gemini_available=gemini_header is not None,
                claude_available=claude_header is not None,
            )
            field_results[field_name] = result
            if result["score"] is not None:
                weighted_values.append((result["score"], config["weight"]))
            if result["status"] == "conflict" and config["critical"]:
                critical_conflicts.append(field_name)
            if result["status"] == "both_empty_required":
                required_missing.append(field_name)

        return {
            "score": round(self._weighted_average(weighted_values), 2),
            "fields": field_results,
            "critical_conflicts": critical_conflicts,
            "required_missing": required_missing,
        }

    def _score_document_lines(
        self,
        line_results: list[LineMergeResult],
    ) -> dict[str, Any]:
        if not line_results:
            return {
                "score": 0.0,
                "unmatched_count": 0,
                "critical_conflict_count": 0,
            }
        score = self._safe_average(
            [item.confidence_pct_calc for item in line_results]
        ) or 0.0
        unmatched_count = sum(
            1
            for item in line_results
            if item.provider_origin
            in {"openai_only", "gemini_only", "claude_only"}
        )
        critical_conflict_count = sum(
            1
            for item in line_results
            if item.comparison_status.get("has_critical_conflict") is True
        )
        return {
            "score": round(score, 2),
            "unmatched_count": unmatched_count,
            "critical_conflict_count": critical_conflict_count,
        }

    def _compute_coherence_score(
        self,
        *,
        merged_document: DocumentoAlbaran,
        header_summary: dict[str, Any],
        line_results: list[LineMergeResult],
    ) -> tuple[float, list[str]]:
        score = 100.0
        flags: list[str] = []

        for field_name in ("numero_albaran", "fecha", "proveedor_nombre"):
            field_summary = header_summary["fields"][field_name]
            if field_summary["status"] == "both_empty_required":
                score -= 20.0
                flags.append(f"missing_required_header:{field_name}")
            elif field_summary["status"] == "conflict":
                score -= 18.0
                flags.append(f"critical_header_conflict:{field_name}")

        if not merged_document.lineas:
            score -= 50.0
            flags.append("no_lines_detected")

        for result in line_results:
            if result.provider_origin == "gemini_only":
                score -= 14.0
                flags.append(
                    f"line_without_openai_match:{result.source_gemini_index}"
                )
            if result.provider_origin == "openai_only":
                score -= 10.0
                flags.append(
                    f"line_only_in_openai:{result.source_openai_index}"
                )
            if result.provider_origin == "claude_only":
                score -= 12.0
                flags.append(
                    f"line_only_in_claude:{result.source_claude_index}"
                )
            if result.comparison_status.get("has_net_mismatch"):
                score -= 12.0
                flags.append(
                    "line_net_mismatch:"
                    f"{result.source_openai_index or result.source_gemini_index or result.source_claude_index}"
                )
            if result.comparison_status.get("has_critical_conflict"):
                score -= 10.0
                flags.append(
                    "line_critical_conflict:"
                    f"{result.source_openai_index or result.source_gemini_index or result.source_claude_index}"
                )

        return round(max(0.0, min(100.0, score)), 2), flags

    def _apply_document_caps(
        self,
        *,
        document_confidence: float,
        provider_origin: str,
        header_summary: dict[str, Any],
        line_results: list[LineMergeResult],
        coherence_flags: list[str],
        caps: list[dict[str, Any]],
    ) -> float:
        final_confidence = document_confidence

        if provider_origin == "openai_fallback":
            final_confidence = min(final_confidence, 84.0)
            caps.append({"reason": "single_provider_openai", "cap": 84.0})

        if provider_origin == "openai_filled":
            final_confidence = min(final_confidence, 88.0)
            caps.append(
                {"reason": "two_providers_openai_claude", "cap": 88.0}
            )

        if header_summary["critical_conflicts"]:
            final_confidence = min(final_confidence, 79.0)
            caps.append({
                "reason": "critical_header_conflict",
                "fields": header_summary["critical_conflicts"],
                "cap": 79.0,
            })

        if header_summary["required_missing"]:
            final_confidence = min(final_confidence, 69.0)
            caps.append({
                "reason": "required_header_missing",
                "fields": header_summary["required_missing"],
                "cap": 69.0,
            })

        unmatched = [
            item
            for item in line_results
            if item.provider_origin
            in {"openai_only", "gemini_only", "claude_only"}
        ]
        if unmatched:
            final_confidence = min(final_confidence, 74.0)
            caps.append(
                {"reason": "unmatched_lines", "count": len(unmatched), "cap": 74.0}
            )

        if any(
            item.comparison_status.get("codigo_imputacion_conflict")
            for item in line_results
        ):
            final_confidence = min(final_confidence, 75.0)
            caps.append({"reason": "codigo_imputacion_conflict", "cap": 75.0})

        critical_line_conflicts = sum(
            1
            for item in line_results
            if item.comparison_status.get("has_critical_conflict")
        )
        if line_results and critical_line_conflicts / len(line_results) > 0.2:
            final_confidence = min(final_confidence, 64.0)
            caps.append({
                "reason": "critical_line_conflict_ratio",
                "ratio": round(critical_line_conflicts / len(line_results), 4),
                "cap": 64.0,
            })

        if "no_lines_detected" in coherence_flags:
            final_confidence = min(final_confidence, 49.0)
            caps.append({"reason": "no_lines_detected", "cap": 49.0})

        return final_confidence

    def _build_review_reasons(
        self,
        *,
        document_confidence: float,
        header_summary: dict[str, Any],
        line_results: list[LineMergeResult],
        coherence_flags: list[str],
        provider_origin: str,
    ) -> list[str]:
        reasons: list[str] = []
        if provider_origin == "openai_fallback":
            reasons.append("single_provider_openai")
        if provider_origin == "openai_filled":
            reasons.append("missing_provider_gemini")
        if document_confidence < 80.0:
            reasons.append("document_confidence_below_threshold")
        reasons.extend(
            f"header_conflict:{field}"
            for field in header_summary["critical_conflicts"]
        )
        reasons.extend(
            f"header_missing:{field}"
            for field in header_summary["required_missing"]
        )
        reasons.extend(coherence_flags)
        for item in line_results:
            if item.provider_origin == "gemini_only":
                reasons.append(
                    f"line_without_openai_match:{item.source_gemini_index}"
                )
            if item.provider_origin == "openai_only":
                reasons.append(
                    f"line_only_in_openai:{item.source_openai_index}"
                )
            if item.provider_origin == "claude_only":
                reasons.append(
                    f"line_only_in_claude:{item.source_claude_index}"
                )
            if item.comparison_status.get("has_critical_conflict"):
                reasons.append(
                    "line_critical_conflict:"
                    f"{item.source_openai_index or item.source_gemini_index or item.source_claude_index}"
                )
            if item.comparison_status.get("has_net_mismatch"):
                reasons.append(
                    "line_net_mismatch:"
                    f"{item.source_openai_index or item.source_gemini_index or item.source_claude_index}"
                )
        seen: set[str] = set()
        ordered: list[str] = []
        for reason in reasons:
            if reason in seen:
                continue
            seen.add(reason)
            ordered.append(reason)
        return ordered

    # ---------------------------------------------------------------- #
    # Utilities                                                        #
    # ---------------------------------------------------------------- #

    @staticmethod
    def _has_meaningful_lines(lines: list[LineaAlbaran]) -> bool:
        for line in lines:
            concepto = (line.concepto or "").strip()
            if concepto:
                return True
        return False

    # ---------------------------------------------------------------- #
    # Line merging                                                     #
    # ---------------------------------------------------------------- #

    def _build_openai_only_lines(
        self,
        openai_lines: list[LineaAlbaran],
    ) -> list[LineMergeResult]:
        results: list[LineMergeResult] = []
        for index, line in enumerate(openai_lines, start=1):
            field_scores: dict[str, Any] = {}
            weighted_values: list[tuple[float, float]] = []
            for field_name, config in _LINE_CONFIGS.items():
                result = self._score_field(
                    field_name=field_name,
                    openai_value=getattr(line, field_name),
                    gemini_value=None,
                    claude_value=None,
                    merged_value=getattr(line, field_name),
                    config=config,
                    gemini_available=False,
                    claude_available=False,
                )
                field_scores[field_name] = result
                if result["score"] is not None:
                    weighted_values.append(
                        (result["score"], config["weight"])
                    )

            line_rule_score = self._weighted_average(weighted_values)
            raw_openai_conf = line.confianza_pct
            final_score = self._combine_line_score(
                rule_score=line_rule_score,
                raw_openai_confidence=raw_openai_conf,
                provider_origin="openai_only",
            )
            comparison_status = {
                "status": "openai_only",
                "has_critical_conflict": False,
                "has_net_mismatch": not self._is_line_net_consistent(line),
                "codigo_imputacion_conflict": False,
            }
            results.append(
                LineMergeResult(
                    merged_line=line,
                    provider_origin="openai_only",
                    source_openai_index=index,
                    source_gemini_index=None,
                    source_claude_index=None,
                    raw_openai_confidence_pct=raw_openai_conf,
                    confidence_pct_calc=final_score,
                    line_match_score=None,
                    comparison_status=comparison_status,
                    field_scores=field_scores,
                )
            )
        return results

    def _build_merged_lines_gemini_primary(
        self,
        *,
        gemini_lines: list[LineaAlbaran],
        openai_lines: list[LineaAlbaran],
        claude_lines: list[LineaAlbaran],
        append_unmatched_openai: bool,
    ) -> tuple[list[LineMergeResult], set[int], set[int]]:
        matched_openai_pairs, unmatched_openai = self._match_lines(
            left_lines=openai_lines,
            right_lines=gemini_lines,
        )
        matched_claude_pairs, unmatched_claude = self._match_lines(
            left_lines=claude_lines,
            right_lines=gemini_lines,
        )
        openai_by_gemini = {
            gem_idx: (left_idx, score)
            for gem_idx, left_idx, score in matched_openai_pairs
        }
        claude_by_gemini = {
            gem_idx: (left_idx, score)
            for gem_idx, left_idx, score in matched_claude_pairs
        }

        results: list[LineMergeResult] = []
        used_openai: set[int] = set()
        used_claude: set[int] = set()

        for gemini_index, gemini_line in enumerate(gemini_lines, start=1):
            openai_info = openai_by_gemini.get(gemini_index)
            claude_info = claude_by_gemini.get(gemini_index)
            openai_index = openai_info[0] if openai_info else None
            claude_index = claude_info[0] if claude_info else None

            if openai_index is not None:
                used_openai.add(openai_index)
            if claude_index is not None:
                used_claude.add(claude_index)

            openai_line = (
                openai_lines[openai_index - 1]
                if openai_index is not None
                else None
            )
            claude_line = (
                claude_lines[claude_index - 1]
                if claude_index is not None
                else None
            )

            # Best match score (para trazabilidad): el más alto entre
            # las dos alineaciones.
            scores = [
                info[1]
                for info in (openai_info, claude_info)
                if info is not None
            ]
            line_match_score = max(scores) if scores else None

            result = self._build_line_from_triple(
                gemini_line=gemini_line,
                openai_line=openai_line,
                claude_line=claude_line,
                gemini_index=gemini_index,
                openai_index=openai_index,
                claude_index=claude_index,
                line_match_score=line_match_score,
                primary_provider="gemini",
            )
            results.append(result)

        # Unmatched openai (si gemini no tiene líneas útiles, se anexan).
        if append_unmatched_openai:
            for openai_index in sorted(unmatched_openai - used_openai):
                results.append(
                    self._build_line_openai_only(
                        openai_line=openai_lines[openai_index - 1],
                        openai_index=openai_index,
                    )
                )

        return results, used_openai, used_claude

    def _build_merged_lines_openai_primary(
        self,
        *,
        openai_lines: list[LineaAlbaran],
        claude_lines: list[LineaAlbaran],
    ) -> list[LineMergeResult]:
        matched_claude_pairs, _ = self._match_lines(
            left_lines=claude_lines,
            right_lines=openai_lines,
        )
        claude_by_openai = {
            oai_idx: (cla_idx, score)
            for oai_idx, cla_idx, score in matched_claude_pairs
        }

        results: list[LineMergeResult] = []
        for openai_index, openai_line in enumerate(openai_lines, start=1):
            claude_info = claude_by_openai.get(openai_index)
            claude_index = claude_info[0] if claude_info else None
            claude_line = (
                claude_lines[claude_index - 1]
                if claude_index is not None
                else None
            )
            line_match_score = claude_info[1] if claude_info else None
            result = self._build_line_from_triple(
                gemini_line=None,
                openai_line=openai_line,
                claude_line=claude_line,
                gemini_index=None,
                openai_index=openai_index,
                claude_index=claude_index,
                line_match_score=line_match_score,
                primary_provider="openai",
            )
            results.append(result)
        return results

    def _build_line_from_triple(
        self,
        *,
        gemini_line: LineaAlbaran | None,
        openai_line: LineaAlbaran | None,
        claude_line: LineaAlbaran | None,
        gemini_index: int | None,
        openai_index: int | None,
        claude_index: int | None,
        line_match_score: float | None,
        primary_provider: str,
    ) -> LineMergeResult:
        merged_payload: dict[str, Any] = {}
        field_scores: dict[str, Any] = {}
        weighted_values: list[tuple[float, float]] = []
        critical_conflict = False
        codigo_imputacion_conflict = False

        gemini_available = gemini_line is not None
        claude_available = claude_line is not None

        for field_name, config in _LINE_CONFIGS.items():
            openai_value = (
                getattr(openai_line, field_name)
                if openai_line is not None
                else None
            )
            gemini_value = (
                getattr(gemini_line, field_name) if gemini_available else None
            )
            claude_value = (
                getattr(claude_line, field_name) if claude_available else None
            )
            merged_value = self._coalesce_triple(
                primary_provider=primary_provider,
                gemini_value=gemini_value,
                openai_value=openai_value,
                claude_value=claude_value,
            )
            merged_payload[field_name] = merged_value

            result = self._score_field(
                field_name=field_name,
                openai_value=openai_value,
                gemini_value=gemini_value,
                claude_value=claude_value,
                merged_value=merged_value,
                config=config,
                gemini_available=gemini_available,
                claude_available=claude_available,
            )
            field_scores[field_name] = result
            if result["score"] is not None:
                weighted_values.append((result["score"], config["weight"]))
            if result["status"] == "conflict" and config["critical"]:
                critical_conflict = True
            if (
                field_name == "codigo_imputacion"
                and result["status"] == "conflict"
            ):
                codigo_imputacion_conflict = True

        merged_payload["id"] = self._coalesce(
            (gemini_line.id if gemini_available else None),
            self._coalesce(
                (openai_line.id if openai_line is not None else None),
                (claude_line.id if claude_available else None),
            ),
        )
        merged_payload["cabecera_id"] = self._coalesce(
            (gemini_line.cabecera_id if gemini_available else None),
            self._coalesce(
                (openai_line.cabecera_id if openai_line is not None else None),
                (claude_line.cabecera_id if claude_available else None),
            ),
        )
        raw_openai_conf = (
            openai_line.confianza_pct if openai_line is not None else None
        )
        merged_payload["confianza_pct"] = raw_openai_conf
        merged_line = LineaAlbaran(**merged_payload)

        provider_origin = self._resolve_line_provider_origin(
            primary_provider=primary_provider,
            openai_present=openai_line is not None,
            gemini_present=gemini_available,
            claude_present=claude_available,
        )

        line_rule_score = self._weighted_average(weighted_values)
        line_final = self._combine_line_score(
            rule_score=line_rule_score,
            raw_openai_confidence=raw_openai_conf,
            provider_origin=provider_origin,
        )
        has_net_mismatch = not self._is_line_net_consistent(merged_line)
        if has_net_mismatch:
            line_final = min(line_final, 60.0)

        comparison_status = {
            "status": provider_origin,
            "has_critical_conflict": critical_conflict,
            "has_net_mismatch": has_net_mismatch,
            "codigo_imputacion_conflict": codigo_imputacion_conflict,
        }
        return LineMergeResult(
            merged_line=merged_line,
            provider_origin=provider_origin,
            source_openai_index=openai_index,
            source_gemini_index=gemini_index,
            source_claude_index=claude_index,
            raw_openai_confidence_pct=raw_openai_conf,
            confidence_pct_calc=line_final,
            line_match_score=(
                round(line_match_score, 4)
                if line_match_score is not None
                else None
            ),
            comparison_status=comparison_status,
            field_scores=field_scores,
        )

    def _build_line_openai_only(
        self,
        *,
        openai_line: LineaAlbaran,
        openai_index: int,
    ) -> LineMergeResult:
        field_scores: dict[str, Any] = {}
        weighted_values: list[tuple[float, float]] = []
        for field_name, config in _LINE_CONFIGS.items():
            result = self._score_field(
                field_name=field_name,
                openai_value=getattr(openai_line, field_name),
                gemini_value=None,
                claude_value=None,
                merged_value=getattr(openai_line, field_name),
                config=config,
                gemini_available=False,
                claude_available=False,
            )
            field_scores[field_name] = result
            if result["score"] is not None:
                weighted_values.append((result["score"], config["weight"]))

        line_rule_score = self._weighted_average(weighted_values)
        raw_openai_conf = openai_line.confianza_pct
        line_final = self._combine_line_score(
            rule_score=line_rule_score,
            raw_openai_confidence=raw_openai_conf,
            provider_origin="openai_only",
        )
        return LineMergeResult(
            merged_line=openai_line,
            provider_origin="openai_only",
            source_openai_index=openai_index,
            source_gemini_index=None,
            source_claude_index=None,
            raw_openai_confidence_pct=raw_openai_conf,
            confidence_pct_calc=line_final,
            line_match_score=None,
            comparison_status={
                "status": "openai_only",
                "has_critical_conflict": False,
                "has_net_mismatch": not self._is_line_net_consistent(
                    openai_line
                ),
                "codigo_imputacion_conflict": False,
            },
            field_scores=field_scores,
        )

    @staticmethod
    def _resolve_line_provider_origin(
        *,
        primary_provider: str,
        openai_present: bool,
        gemini_present: bool,
        claude_present: bool,
    ) -> str:
        if primary_provider == "gemini":
            if openai_present and claude_present:
                return "gemini_filled_triple"
            if openai_present:
                return "gemini_filled"
            if claude_present:
                return "gemini_filled_claude"
            return "gemini_only"
        if primary_provider == "openai":
            if claude_present:
                return "openai_filled_claude"
            return "openai_only"
        # Should not happen in current pipeline, but kept for future.
        return "claude_only"

    # ---------------------------------------------------------------- #
    # Header merge                                                     #
    # ---------------------------------------------------------------- #

    def _merge_header_triple(
        self,
        *,
        primary: CabeceraAlbaran,
        secondary: CabeceraAlbaran,
        tertiary: CabeceraAlbaran | None,
    ) -> CabeceraAlbaran:
        payload: dict[str, Any] = {}
        for field_name in _HEADER_CONFIGS:
            primary_value = getattr(primary, field_name)
            secondary_value = getattr(secondary, field_name)
            tertiary_value = (
                getattr(tertiary, field_name) if tertiary is not None else None
            )
            payload[field_name] = self._coalesce(
                primary_value,
                self._coalesce(secondary_value, tertiary_value),
            )
        payload["id"] = self._coalesce(
            primary.id,
            self._coalesce(
                secondary.id,
                tertiary.id if tertiary is not None else None,
            ),
        )
        return CabeceraAlbaran(**payload)

    def _coalesce_triple(
        self,
        *,
        primary_provider: str,
        gemini_value: Any,
        openai_value: Any,
        claude_value: Any,
    ) -> Any:
        """
        Orden de coalesce por líneas:
        - Si el proveedor primario es gemini → gemini > openai > claude.
        - Si el proveedor primario es openai → openai > claude > gemini.
        (gemini no existe en este caso, pero lo dejamos por robustez).
        """
        if primary_provider == "gemini":
            return self._coalesce(
                gemini_value,
                self._coalesce(openai_value, claude_value),
            )
        if primary_provider == "openai":
            return self._coalesce(
                openai_value,
                self._coalesce(claude_value, gemini_value),
            )
        return self._coalesce(
            claude_value,
            self._coalesce(gemini_value, openai_value),
        )

    # ---------------------------------------------------------------- #
    # Line matching                                                    #
    # ---------------------------------------------------------------- #

    def _match_lines(
        self,
        *,
        left_lines: list[LineaAlbaran],
        right_lines: list[LineaAlbaran],
    ) -> tuple[list[tuple[int, int, float]], set[int]]:
        """
        Para cada línea right, encuentra la mejor line left (no usada aún)
        por similitud. Devuelve pares (right_idx, left_idx, score) y
        el conjunto de left indexes que no encontraron match.
        """
        unmatched_left = set(range(1, len(left_lines) + 1))
        matched_pairs: list[tuple[int, int, float]] = []

        for right_index, right_line in enumerate(right_lines, start=1):
            best_left_index: int | None = None
            best_score = 0.0
            for left_index in list(unmatched_left):
                score = self._line_similarity(
                    a_line=left_lines[left_index - 1],
                    b_line=right_line,
                )
                if score > best_score:
                    best_score = score
                    best_left_index = left_index
            if best_left_index is not None and best_score >= 0.55:
                matched_pairs.append((right_index, best_left_index, best_score))
                unmatched_left.discard(best_left_index)

        return matched_pairs, unmatched_left

    def _line_similarity(
        self,
        *,
        a_line: LineaAlbaran,
        b_line: LineaAlbaran,
    ) -> float:
        concepto = self._text_similarity(a_line.concepto, b_line.concepto)
        cantidad = self._number_similarity(a_line.cantidad, b_line.cantidad, 0.05)
        precio = self._number_similarity(a_line.precio, b_line.precio, 0.05)
        precio_neto = self._number_similarity(
            a_line.precio_neto, b_line.precio_neto, 0.15
        )
        codigo = self._text_similarity(
            a_line.codigo_imputacion, b_line.codigo_imputacion
        )
        return (
            0.55 * concepto
            + 0.15 * cantidad
            + 0.10 * precio
            + 0.10 * precio_neto
            + 0.10 * codigo
        )

    # ---------------------------------------------------------------- #
    # Field scoring (ternario)                                         #
    # ---------------------------------------------------------------- #

    def _score_field(
        self,
        *,
        field_name: str,
        openai_value: Any,
        gemini_value: Any,
        claude_value: Any,
        merged_value: Any,
        config: dict[str, Any],
        gemini_available: bool,
        claude_available: bool,
    ) -> dict[str, Any]:
        required = bool(config["required"])
        critical = bool(config["critical"])
        kind = str(config["kind"])
        tolerance = float(config.get("tolerance", 0.0))

        status = self._field_status(
            kind=kind,
            openai_value=openai_value,
            gemini_value=gemini_value,
            claude_value=claude_value,
            required=required,
            tolerance=tolerance,
            gemini_available=gemini_available,
            claude_available=claude_available,
        )
        score = _FIELD_SCORE_BY_STATUS[status]
        if isinstance(score, float) and math.isnan(score):
            score_value: float | None = None
        else:
            score_value = float(score)
            score_value += self._validation_adjustment(
                field_name=field_name,
                merged_value=merged_value,
                kind=kind,
            )
            score_value = max(0.0, min(100.0, score_value))

        return {
            "status": status,
            "score": round(score_value, 2) if score_value is not None else None,
            "critical": critical,
            "required": required,
            "openai": openai_value,
            "gemini": gemini_value,
            "claude": claude_value,
            "merged": merged_value,
        }

    def _field_status(
        self,
        *,
        kind: str,
        openai_value: Any,
        gemini_value: Any,
        claude_value: Any,
        required: bool,
        tolerance: float,
        gemini_available: bool,
        claude_available: bool,
    ) -> str:
        openai_empty = self._is_empty(openai_value)
        gemini_empty = self._is_empty(gemini_value) or not gemini_available
        claude_empty = self._is_empty(claude_value) or not claude_available

        present_count = sum(
            1 for empty in (openai_empty, gemini_empty, claude_empty) if not empty
        )

        # Todos vacíos
        if present_count == 0:
            return "both_empty_required" if required else "both_empty_optional"

        # Solo uno tiene valor → "<provider>_only"
        if present_count == 1:
            if not openai_empty:
                return "openai_only"
            if not gemini_empty:
                return "gemini_only"
            return "claude_only"

        # Dos o tres tienen valor → comparamos.
        return self._compare_present_values(
            kind=kind,
            openai_value=openai_value,
            gemini_value=gemini_value,
            claude_value=claude_value,
            openai_empty=openai_empty,
            gemini_empty=gemini_empty,
            claude_empty=claude_empty,
            tolerance=tolerance,
        )

    def _compare_present_values(
        self,
        *,
        kind: str,
        openai_value: Any,
        gemini_value: Any,
        claude_value: Any,
        openai_empty: bool,
        gemini_empty: bool,
        claude_empty: bool,
        tolerance: float,
    ) -> str:
        present_pairs: list[tuple[str, Any]] = []
        if not openai_empty:
            present_pairs.append(("openai", openai_value))
        if not gemini_empty:
            present_pairs.append(("gemini", gemini_value))
        if not claude_empty:
            present_pairs.append(("claude", claude_value))

        match_types: list[str] = []
        for i in range(len(present_pairs)):
            for j in range(i + 1, len(present_pairs)):
                _, value_a = present_pairs[i]
                _, value_b = present_pairs[j]
                match_types.append(
                    self._pairwise_match_type(
                        kind=kind,
                        value_a=value_a,
                        value_b=value_b,
                        tolerance=tolerance,
                    )
                )

        match_count = sum(
            1 for match_type in match_types if match_type != "conflict"
        )
        total_pairs = len(match_types)

        if total_pairs == 0:
            # Un único valor (caso no posible aquí).
            return "openai_only"

        # Caso con 3 proveedores: 3 pares. Si todos coinciden → triple_match.
        if total_pairs == 3 and match_count == 3:
            return "triple_match"

        # Caso con 2 proveedores: 1 par.
        if total_pairs == 1:
            match_type = match_types[0]
            if match_type == "conflict":
                return "conflict"
            return match_type

        # 3 proveedores, 2 de 3 pares coinciden → mayoría.
        if match_count >= 2:
            return "majority_match"

        # 3 proveedores, 1 o 0 coincidencias → conflicto.
        return "conflict"

    @staticmethod
    def _pairwise_match_type(
        *,
        kind: str,
        value_a: Any,
        value_b: Any,
        tolerance: float,
    ) -> str:
        service = AlbaranConfidenceService
        if kind in {"number"}:
            if value_a == value_b:
                return "match_exact"
            if service._numbers_match(value_a, value_b, tolerance):
                return "match_tolerant"
            return "conflict"
        if kind in {"date"}:
            if str(value_a) == str(value_b):
                return "match_exact"
            if service._compare_key_static(value_a) == service._compare_key_static(
                value_b
            ):
                return "match_normalized"
            return "conflict"
        if str(value_a) == str(value_b):
            return "match_exact"
        if service._compare_key_static(value_a) == service._compare_key_static(
            value_b
        ):
            return "match_normalized"
        return "conflict"

    def _validation_adjustment(
        self,
        *,
        field_name: str,
        merged_value: Any,
        kind: str,
    ) -> float:
        if self._is_empty(merged_value):
            return 0.0
        if field_name == "proveedor_cif":
            return 8.0 if self._is_valid_cif(str(merged_value)) else -20.0
        if kind == "date":
            return 6.0 if self._is_valid_iso_date(str(merged_value)) else -15.0
        return 0.0

    def _combine_line_score(
        self,
        *,
        rule_score: float,
        raw_openai_confidence: float | None,
        provider_origin: str,
    ) -> float:
        if raw_openai_confidence is None:
            combined = rule_score
        else:
            combined = 0.75 * rule_score + 0.25 * raw_openai_confidence
        if provider_origin in {"gemini_only", "claude_only"}:
            combined = min(combined, 74.0)
        if provider_origin == "openai_only":
            combined = min(combined, 84.0)
        if provider_origin == "openai_filled_claude":
            combined = min(combined, 88.0)
        return round(max(0.0, min(100.0, combined)), 2)

    def _is_line_net_consistent(self, line: LineaAlbaran) -> bool:
        if line.cantidad is None or line.precio is None or line.precio_neto is None:
            return True
        descuento = line.descuento or 0.0
        expected = (
            float(line.cantidad)
            * float(line.precio)
            * (1 - (float(descuento) / 100.0))
        )
        tolerance = max(0.15, abs(expected) * 0.02)
        return abs(expected - float(line.precio_neto)) <= tolerance

    # ---------------------------------------------------------------- #
    # Helpers                                                          #
    # ---------------------------------------------------------------- #

    @staticmethod
    def _coalesce(primary: Any, fallback: Any) -> Any:
        if primary is None:
            return fallback
        if isinstance(primary, str) and not primary.strip():
            return fallback
        return primary

    @staticmethod
    def _weighted_average(values: list[tuple[float, float]]) -> float:
        valid = [
            (value, weight)
            for value, weight in values
            if value is not None and weight > 0
        ]
        if not valid:
            return 0.0
        numerator = sum(value * weight for value, weight in valid)
        denominator = sum(weight for _, weight in valid)
        return numerator / denominator if denominator else 0.0

    @staticmethod
    def _safe_average(values: list[float | None]) -> float | None:
        valid = [float(value) for value in values if value is not None]
        if not valid:
            return None
        return sum(valid) / len(valid)

    @staticmethod
    def _is_empty(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    @staticmethod
    def _strip_accents(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        return "".join(char for char in normalized if not unicodedata.combining(char))

    @classmethod
    def _compare_key_static(cls, value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip().upper()
        text = cls._strip_accents(text)
        text = re.sub(r"\s+", " ", text)
        return text

    def _compare_key(self, value: Any) -> str:
        return self._compare_key_static(value)

    def _text_similarity(self, left: Any, right: Any) -> float:
        if self._is_empty(left) and self._is_empty(right):
            return 0.3
        if self._is_empty(left) or self._is_empty(right):
            return 0.0
        return SequenceMatcher(
            None, self._compare_key(left), self._compare_key(right)
        ).ratio()

    def _number_similarity(
        self,
        left: float | None,
        right: float | None,
        tolerance: float,
    ) -> float:
        if left is None and right is None:
            return 0.3
        if left is None or right is None:
            return 0.0
        if self._numbers_match(left, right, tolerance):
            return 1.0
        denominator = max(abs(float(left)), abs(float(right)), 1.0)
        return max(
            0.0,
            1.0 - (abs(float(left) - float(right)) / denominator),
        )

    @staticmethod
    def _numbers_match(left: Any, right: Any, tolerance: float) -> bool:
        if left is None or right is None:
            return False
        try:
            left_num = float(left)
            right_num = float(right)
        except Exception:
            return False
        effective_tolerance = max(
            tolerance, max(abs(left_num), abs(right_num)) * 0.01
        )
        return abs(left_num - right_num) <= effective_tolerance

    @staticmethod
    def _is_valid_iso_date(value: str) -> bool:
        try:
            date.fromisoformat(value)
            return True
        except Exception:
            return False

    @staticmethod
    def _is_valid_cif(value: str) -> bool:
        compact = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
        patterns = (
            r"^[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]$",
            r"^[XYZ]\d{7}[A-Z]$",
            r"^\d{8}[A-Z]$",
        )
        return any(re.fullmatch(pattern, compact) for pattern in patterns)
