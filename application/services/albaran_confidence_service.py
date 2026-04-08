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

_FIELD_SCORE_BY_STATUS: dict[str, float] = {
    "match_exact": 96.0,
    "match_normalized": 92.0,
    "match_tolerant": 88.0,
    "openai_only": 76.0,
    "gemini_only": 66.0,
    "both_empty_required": 20.0,
    "both_empty_optional": math.nan,
    "conflict": 28.0,
    "invalid": 10.0,
}


class AlbaranConfidenceService:
    def build_merge_analysis(
        self,
        *,
        openai: ExtractionEnvelope,
        gemini: ProviderExtractionEnvelope | None,
    ) -> MergeAnalysis:
        if gemini is None:
            merged_document = openai.data
            line_results = self._build_openai_only_lines(openai.data.lineas)
            provider_origin = "openai_fallback"
        else:
            header = self._merge_header(gemini.data.cabecera, openai.data.cabecera)
            matched_pairs, unmatched_openai = self._match_lines(
                openai_lines=openai.data.lineas,
                gemini_lines=gemini.data.lineas,
            )
            gemini_has_meaningful_lines = self._has_meaningful_lines(
                gemini.data.lineas
            )
            line_results = self._build_merged_lines(
                openai_lines=openai.data.lineas,
                gemini_lines=gemini.data.lineas,
                matched_pairs=matched_pairs,
                unmatched_openai=unmatched_openai,
                append_unmatched_openai=not gemini_has_meaningful_lines,
            )
            merged_document = DocumentoAlbaran(
                cabecera=header,
                lineas=[item.merged_line for item in line_results],
            )
            provider_origin = "gemini_filled"

        merged_envelope = ProviderExtractionEnvelope(
            meta=(gemini.meta if gemini is not None else openai.meta),
            data=merged_document,
            debug=(gemini.debug if gemini is not None else openai.debug),
        )

        header_summary = self._score_header(
            openai_header=openai.data.cabecera,
            gemini_header=gemini.data.cabecera if gemini is not None else None,
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
            "version": "comparison-v1",
            "provider_origin": provider_origin,
            "document_confidence_pct": doc_conf,
            "openai_raw_confidence_pct": openai_raw_conf,
            "header": header_summary,
            "lines": [
                {
                    "merge_line_index": index + 1,
                    "provider_origin": result.provider_origin,
                    "source_openai_index": result.source_openai_index,
                    "source_gemini_index": result.source_gemini_index,
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

    def _score_header(
        self,
        *,
        openai_header: CabeceraAlbaran,
        gemini_header: CabeceraAlbaran | None,
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
                    getattr(gemini_header, field_name) if gemini_header is not None else None
                ),
                merged_value=getattr(merged_header, field_name),
                config=config,
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
            if item.provider_origin in {"openai_only", "gemini_only"}
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
            if result.comparison_status.get("has_net_mismatch"):
                score -= 12.0
                flags.append(
                    f"line_net_mismatch:{result.source_openai_index or result.source_gemini_index}"
                )
            if result.comparison_status.get("has_critical_conflict"):
                score -= 10.0
                flags.append(
                    f"line_critical_conflict:{result.source_openai_index or result.source_gemini_index}"
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
            item for item in line_results if item.provider_origin in {"openai_only", "gemini_only"}
        ]
        if unmatched:
            final_confidence = min(final_confidence, 74.0)
            caps.append({"reason": "unmatched_lines", "count": len(unmatched), "cap": 74.0})

        if any(item.comparison_status.get("codigo_imputacion_conflict") for item in line_results):
            final_confidence = min(final_confidence, 75.0)
            caps.append({"reason": "codigo_imputacion_conflict", "cap": 75.0})

        critical_line_conflicts = sum(
            1 for item in line_results if item.comparison_status.get("has_critical_conflict")
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
        if document_confidence < 80.0:
            reasons.append("document_confidence_below_threshold")
        reasons.extend(f"header_conflict:{field}" for field in header_summary["critical_conflicts"])
        reasons.extend(f"header_missing:{field}" for field in header_summary["required_missing"])
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
            if item.comparison_status.get("has_critical_conflict"):
                reasons.append(
                    f"line_critical_conflict:{item.source_openai_index or item.source_gemini_index}"
                )
            if item.comparison_status.get("has_net_mismatch"):
                reasons.append(
                    f"line_net_mismatch:{item.source_openai_index or item.source_gemini_index}"
                )
        seen: set[str] = set()
        ordered: list[str] = []
        for reason in reasons:
            if reason in seen:
                continue
            seen.add(reason)
            ordered.append(reason)
        return ordered

    @staticmethod
    def _has_meaningful_lines(lines: list[LineaAlbaran]) -> bool:
        for line in lines:
            concepto = (line.concepto or "").strip()
            if concepto:
                return True
        return False

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
                    merged_value=getattr(line, field_name),
                    config=config,
                )
                field_scores[field_name] = result
                if result["score"] is not None:
                    weighted_values.append((result["score"], config["weight"]))

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
                    raw_openai_confidence_pct=raw_openai_conf,
                    confidence_pct_calc=final_score,
                    line_match_score=None,
                    comparison_status=comparison_status,
                    field_scores=field_scores,
                )
            )
        return results

    def _build_merged_lines(
        self,
        *,
        openai_lines: list[LineaAlbaran],
        gemini_lines: list[LineaAlbaran],
        matched_pairs: list[tuple[int, int, float]],
        unmatched_openai: set[int],
        append_unmatched_openai: bool,
    ) -> list[LineMergeResult]:
        openai_by_gemini = {gem_idx: (openai_idx, score) for gem_idx, openai_idx, score in matched_pairs}
        results: list[LineMergeResult] = []

        for gemini_index, gemini_line in enumerate(gemini_lines, start=1):
            openai_info = openai_by_gemini.get(gemini_index)
            openai_index = openai_info[0] if openai_info else None
            line_match_score = openai_info[1] if openai_info else None
            openai_line = openai_lines[openai_index - 1] if openai_index is not None else None

            merged_payload: dict[str, Any] = {}
            field_scores: dict[str, Any] = {}
            weighted_values: list[tuple[float, float]] = []
            critical_conflict = False
            codigo_imputacion_conflict = False

            for field_name, config in _LINE_CONFIGS.items():
                openai_value = getattr(openai_line, field_name) if openai_line is not None else None
                gemini_value = getattr(gemini_line, field_name)
                merged_value = self._coalesce(gemini_value, openai_value)
                merged_payload[field_name] = merged_value

                result = self._score_field(
                    field_name=field_name,
                    openai_value=openai_value,
                    gemini_value=gemini_value,
                    merged_value=merged_value,
                    config=config,
                )
                field_scores[field_name] = result
                if result["score"] is not None:
                    weighted_values.append((result["score"], config["weight"]))
                if result["status"] == "conflict" and config["critical"]:
                    critical_conflict = True
                if field_name == "codigo_imputacion" and result["status"] == "conflict":
                    codigo_imputacion_conflict = True

            merged_payload["id"] = self._coalesce(gemini_line.id, openai_line.id if openai_line else None)
            merged_payload["cabecera_id"] = self._coalesce(
                gemini_line.cabecera_id,
                openai_line.cabecera_id if openai_line else None,
            )
            raw_openai_conf = openai_line.confianza_pct if openai_line is not None else None
            merged_payload["confianza_pct"] = raw_openai_conf
            merged_line = LineaAlbaran(**merged_payload)

            if openai_line is None:
                provider_origin = "gemini_only"
            else:
                provider_origin = "gemini_filled"

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
            results.append(
                LineMergeResult(
                    merged_line=merged_line,
                    provider_origin=provider_origin,
                    source_openai_index=openai_index,
                    source_gemini_index=gemini_index,
                    raw_openai_confidence_pct=raw_openai_conf,
                    confidence_pct_calc=line_final,
                    line_match_score=(round(line_match_score, 4) if line_match_score is not None else None),
                    comparison_status=comparison_status,
                    field_scores=field_scores,
                )
            )

        if append_unmatched_openai:
            for openai_index in sorted(unmatched_openai):
                openai_line = openai_lines[openai_index - 1]
                field_scores: dict[str, Any] = {}
                weighted_values: list[tuple[float, float]] = []
                for field_name, config in _LINE_CONFIGS.items():
                    result = self._score_field(
                        field_name=field_name,
                        openai_value=getattr(openai_line, field_name),
                        gemini_value=None,
                        merged_value=getattr(openai_line, field_name),
                        config=config,
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
                results.append(
                    LineMergeResult(
                        merged_line=openai_line,
                        provider_origin="openai_only",
                        source_openai_index=openai_index,
                        source_gemini_index=None,
                        raw_openai_confidence_pct=raw_openai_conf,
                        confidence_pct_calc=line_final,
                        line_match_score=None,
                        comparison_status={
                            "status": "openai_only",
                            "has_critical_conflict": False,
                            "has_net_mismatch": not self._is_line_net_consistent(openai_line),
                            "codigo_imputacion_conflict": False,
                        },
                        field_scores=field_scores,
                    )
                )

        return results

    def _merge_header(
        self,
        gemini_header: CabeceraAlbaran,
        openai_header: CabeceraAlbaran,
    ) -> CabeceraAlbaran:
        payload: dict[str, Any] = {}
        for field_name in _HEADER_CONFIGS:
            payload[field_name] = self._coalesce(
                getattr(gemini_header, field_name),
                getattr(openai_header, field_name),
            )
        payload["id"] = self._coalesce(gemini_header.id, openai_header.id)
        return CabeceraAlbaran(**payload)

    def _match_lines(
        self,
        *,
        openai_lines: list[LineaAlbaran],
        gemini_lines: list[LineaAlbaran],
    ) -> tuple[list[tuple[int, int, float]], set[int]]:
        unmatched_openai = set(range(1, len(openai_lines) + 1))
        matched_pairs: list[tuple[int, int, float]] = []

        for gemini_index, gemini_line in enumerate(gemini_lines, start=1):
            best_openai_index: int | None = None
            best_score = 0.0
            for openai_index in list(unmatched_openai):
                score = self._line_similarity(
                    openai_line=openai_lines[openai_index - 1],
                    gemini_line=gemini_line,
                )
                if score > best_score:
                    best_score = score
                    best_openai_index = openai_index
            if best_openai_index is not None and best_score >= 0.55:
                matched_pairs.append((gemini_index, best_openai_index, best_score))
                unmatched_openai.discard(best_openai_index)

        return matched_pairs, unmatched_openai

    def _line_similarity(
        self,
        *,
        openai_line: LineaAlbaran,
        gemini_line: LineaAlbaran,
    ) -> float:
        concepto = self._text_similarity(openai_line.concepto, gemini_line.concepto)
        cantidad = self._number_similarity(openai_line.cantidad, gemini_line.cantidad, 0.05)
        precio = self._number_similarity(openai_line.precio, gemini_line.precio, 0.05)
        precio_neto = self._number_similarity(openai_line.precio_neto, gemini_line.precio_neto, 0.15)
        codigo = self._text_similarity(openai_line.codigo_imputacion, gemini_line.codigo_imputacion)
        return (
            0.55 * concepto
            + 0.15 * cantidad
            + 0.10 * precio
            + 0.10 * precio_neto
            + 0.10 * codigo
        )

    def _score_field(
        self,
        *,
        field_name: str,
        openai_value: Any,
        gemini_value: Any,
        merged_value: Any,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        required = bool(config["required"])
        critical = bool(config["critical"])
        kind = str(config["kind"])
        tolerance = float(config.get("tolerance", 0.0))

        status = self._field_status(
            kind=kind,
            openai_value=openai_value,
            gemini_value=gemini_value,
            required=required,
            tolerance=tolerance,
        )
        score = _FIELD_SCORE_BY_STATUS[status]
        if math.isnan(score):
            score_value: float | None = None
        else:
            score_value = score
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
            "merged": merged_value,
        }

    def _field_status(
        self,
        *,
        kind: str,
        openai_value: Any,
        gemini_value: Any,
        required: bool,
        tolerance: float,
    ) -> str:
        openai_empty = self._is_empty(openai_value)
        gemini_empty = self._is_empty(gemini_value)

        if openai_empty and gemini_empty:
            return "both_empty_required" if required else "both_empty_optional"
        if not openai_empty and gemini_empty:
            return "openai_only"
        if openai_empty and not gemini_empty:
            return "gemini_only"

        if kind in {"number"}:
            if openai_value == gemini_value:
                return "match_exact"
            if self._numbers_match(openai_value, gemini_value, tolerance):
                return "match_tolerant"
            return "conflict"

        if kind in {"date"}:
            if str(openai_value) == str(gemini_value):
                return "match_exact"
            if self._compare_key(openai_value) == self._compare_key(gemini_value):
                return "match_normalized"
            return "conflict"

        if str(openai_value) == str(gemini_value):
            return "match_exact"
        if self._compare_key(openai_value) == self._compare_key(gemini_value):
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
        if provider_origin == "gemini_only":
            combined = min(combined, 74.0)
        if provider_origin == "openai_only":
            combined = min(combined, 84.0)
        return round(max(0.0, min(100.0, combined)), 2)

    def _is_line_net_consistent(self, line: LineaAlbaran) -> bool:
        if line.cantidad is None or line.precio is None or line.precio_neto is None:
            return True
        descuento = line.descuento or 0.0
        expected = float(line.cantidad) * float(line.precio) * (1 - (float(descuento) / 100.0))
        tolerance = max(0.15, abs(expected) * 0.02)
        return abs(expected - float(line.precio_neto)) <= tolerance

    @staticmethod
    def _coalesce(primary: Any, fallback: Any) -> Any:
        if primary is None:
            return fallback
        if isinstance(primary, str) and not primary.strip():
            return fallback
        return primary

    @staticmethod
    def _weighted_average(values: list[tuple[float, float]]) -> float:
        valid = [(value, weight) for value, weight in values if value is not None and weight > 0]
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

    def _compare_key(self, value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip().upper()
        text = self._strip_accents(text)
        text = re.sub(r"\s+", " ", text)
        return text

    def _text_similarity(self, left: Any, right: Any) -> float:
        if self._is_empty(left) and self._is_empty(right):
            return 0.3
        if self._is_empty(left) or self._is_empty(right):
            return 0.0
        return SequenceMatcher(None, self._compare_key(left), self._compare_key(right)).ratio()

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
        return max(0.0, 1.0 - (abs(float(left) - float(right)) / denominator))

    @staticmethod
    def _numbers_match(left: Any, right: Any, tolerance: float) -> bool:
        if left is None or right is None:
            return False
        try:
            left_num = float(left)
            right_num = float(right)
        except Exception:
            return False
        effective_tolerance = max(tolerance, max(abs(left_num), abs(right_num)) * 0.01)
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
