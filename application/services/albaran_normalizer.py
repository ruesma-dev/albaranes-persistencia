# application/services/albaran_normalizer.py
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from domain.models.extraction_models import (
    CabeceraAlbaran,
    DocumentoAlbaran,
    LineaAlbaran,
)


class AlbaranNormalizer:
    _DATE_FORMATS: tuple[str, ...] = (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%d.%m.%y",
        "%Y/%m/%d",
        "%Y.%m.%d",
    )

    def normalize_provider_document(
        self,
        *,
        document: DocumentoAlbaran,
        provider_origin: str,
    ) -> DocumentoAlbaran:
        normalized_header = self.normalize_header(document.cabecera)
        normalized_lines = [
            self.normalize_line(line=line, provider_origin=provider_origin)
            for line in document.lineas
        ]
        return document.model_copy(
            update={
                "cabecera": normalized_header,
                "lineas": normalized_lines,
            }
        )

    def normalize_header(self, header: CabeceraAlbaran) -> CabeceraAlbaran:
        return header.model_copy(
            update={
                "proveedor_nombre": self.normalize_text(header.proveedor_nombre),
                "proveedor_cif": self.normalize_cif(header.proveedor_cif),
                "fecha": self.normalize_date(header.fecha),
                "numero_albaran": self.normalize_identifier(
                    header.numero_albaran,
                ),
                "forma_pago": self.normalize_text(header.forma_pago),
                "obra_codigo": self.normalize_identifier(header.obra_codigo),
                "obra_nombre": self.normalize_text(header.obra_nombre),
                "obra_direccion": self.normalize_text(header.obra_direccion),
                "id": self.normalize_identifier(header.id),
            }
        )

    def normalize_line(
        self,
        *,
        line: LineaAlbaran,
        provider_origin: str,
    ) -> LineaAlbaran:
        confidence = self.normalize_confidence(line.confianza_pct)
        if provider_origin == "gemini":
            confidence = None
        return line.model_copy(
            update={
                "id": self.normalize_identifier(line.id),
                "cabecera_id": self.normalize_identifier(line.cabecera_id),
                "codigo": self.normalize_identifier(line.codigo),
                "cantidad": self.normalize_number(line.cantidad),
                "concepto": self.normalize_text(line.concepto),
                "precio": self.normalize_number(line.precio),
                "descuento": self.normalize_number(line.descuento),
                "precio_neto": self.normalize_number(line.precio_neto),
                "codigo_imputacion": self.normalize_codigo_imputacion(
                    line.codigo_imputacion,
                ),
                "confianza_pct": confidence,
            }
        )

    @staticmethod
    def normalize_text(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = re.sub(r"\s+", " ", str(value)).strip()
        return cleaned or None

    @staticmethod
    def normalize_identifier(value: str | None) -> str | None:
        cleaned = AlbaranNormalizer.normalize_text(value)
        if cleaned is None:
            return None
        return cleaned.upper()


    @classmethod
    def normalize_codigo_imputacion(cls, value: str | None) -> str | None:
        cleaned = cls.normalize_text(value)
        if cleaned is None:
            return None

        candidate = cleaned.upper()
        candidate = re.sub(r"[;,/_\-]+", ".", candidate)
        candidate = re.sub(r"\s*\.\s*", ".", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip()

        if "." not in candidate and " " in candidate:
            tokens = [token for token in candidate.split(" ") if token]
            if tokens and all(token == "CI" or re.fullmatch(r"\d{1,2}", token) for token in tokens):
                candidate = ".".join(tokens)

        candidate = candidate.replace(" ", "")
        candidate = re.sub(r"\.{2,}", ".", candidate).strip(".")
        if not candidate:
            return None

        segments = [segment for segment in candidate.split(".") if segment]
        normalized_segments: list[str] = []
        for segment in segments:
            if segment == "CI":
                normalized_segments.append(segment)
                continue
            if segment.isdigit() and len(segment) == 1:
                normalized_segments.append(segment.zfill(2))
                continue
            normalized_segments.append(segment)

        normalized = ".".join(normalized_segments)
        return normalized or None

    @staticmethod
    def normalize_cif(value: str | None) -> str | None:
        cleaned = AlbaranNormalizer.normalize_text(value)
        if cleaned is None:
            return None
        compact = re.sub(r"[^A-Za-z0-9]", "", cleaned).upper()
        return compact or None

    def normalize_date(self, value: str | None) -> str | None:
        cleaned = self.normalize_text(value)
        if cleaned is None:
            return None
        for fmt in self._DATE_FORMATS:
            try:
                parsed = datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
            return parsed.strftime("%Y-%m-%d")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
            return cleaned
        return cleaned

    @staticmethod
    def normalize_number(value: float | int | str | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text_value = str(value).strip()
        if not text_value:
            return None
        candidate = text_value.replace(" ", "")
        if "," in candidate and "." in candidate:
            if candidate.rfind(",") > candidate.rfind("."):
                candidate = candidate.replace(".", "").replace(",", ".")
            else:
                candidate = candidate.replace(",", "")
        elif "," in candidate:
            candidate = candidate.replace(",", ".")
        try:
            return float(candidate)
        except ValueError:
            return None

    @staticmethod
    def normalize_confidence(value: float | int | str | None) -> float | None:
        normalized = AlbaranNormalizer.normalize_number(value)
        if normalized is None:
            return None
        if 0.0 <= normalized <= 1.0:
            normalized *= 100.0
        return max(0.0, min(100.0, normalized))

    @staticmethod
    def mean(values: Iterable[float | None]) -> float | None:
        valid = [float(value) for value in values if value is not None]
        if not valid:
            return None
        return sum(valid) / len(valid)
