# application/pipelines/persist_albaran_pipeline.py
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict

from application.services.albaran_normalizer import AlbaranNormalizer
from application.services.obra_enrichment_service import ObraEnrichmentService
from domain.models.extraction_models import (
    ExtractionEnvelope,
    ProviderExtractionEnvelope,
)
from domain.ports.albaran_repository import AlbaranRepository
from domain.ports.document_storage import DocumentStorage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersistAlbaranRequest:
    filename: str
    mime_type: str
    file_bytes: bytes
    extraction_envelope: Dict[str, Any]
    context: Dict[str, Any]


@dataclass(frozen=True)
class PersistAlbaranResult:
    ok: bool
    document_id: str
    sharepoint_url: str | None
    duplicate: bool
    stored_lines: int


class PersistAlbaranPipeline:
    def __init__(
        self,
        *,
        repository: AlbaranRepository,
        document_storage: DocumentStorage,
        normalizer: AlbaranNormalizer,
        obra_enrichment_service: ObraEnrichmentService | None = None,
    ) -> None:
        self._repository = repository
        self._document_storage = document_storage
        self._normalizer = normalizer
        self._obra_enrichment_service = obra_enrichment_service
        logger.info(
            "[obra-enrichment][pipeline] PersistAlbaranPipeline construido; "
            "enrichment_service=%s",
            "PRESENTE" if obra_enrichment_service is not None else "None",
        )

    def run(self, request: PersistAlbaranRequest) -> PersistAlbaranResult:
        if not request.file_bytes:
            raise ValueError("Archivo vacío.")

        envelope = ExtractionEnvelope.model_validate(request.extraction_envelope)
        sha256 = hashlib.sha256(request.file_bytes).hexdigest()
        self._validate_sha256(envelope=envelope, sha256=sha256)

        existing = self._repository.get_by_sha256(sha256)
        if existing is not None:
            logger.info(
                "Documento duplicado detectado. sha256=%s document_id=%s",
                sha256,
                existing.document_id,
            )
            # Idempotencia: si ya existía y ahora Sigrid responde, también
            # enriquecemos. Evita que reintentos sobre el mismo fichero
            # sigan mostrando datos antiguos si la BBDD cambió.
            self._enrich_safely(merge_document_id=existing.document_id)
            return PersistAlbaranResult(
                ok=True,
                document_id=existing.document_id,
                sharepoint_url=existing.sharepoint_url,
                duplicate=True,
                stored_lines=existing.stored_lines,
            )

        envelope = self._normalize(envelope)
        openai_debug = (
            envelope.debug if isinstance(envelope.debug, dict) else {}
        )
        gemini_debug = (
            envelope.gemini.debug
            if envelope.gemini is not None and isinstance(envelope.gemini.debug, dict)
            else {}
        )
        claude_debug = (
            envelope.claude.debug
            if envelope.claude is not None and isinstance(envelope.claude.debug, dict)
            else {}
        )

        stored_file = self._document_storage.upload(
            filename=request.filename,
            mime_type=request.mime_type,
            file_bytes=request.file_bytes,
            source_sha256=sha256,
            ia_input_payload=self._coerce_dict(
                openai_debug.get("openai_request")
            ),
            ia_output_payload=self._coerce_dict(
                openai_debug.get("openai_response")
            ),
            gem_input_payload=self._coerce_dict(
                gemini_debug.get("gemini_request")
            ),
            gem_output_payload=self._coerce_dict(
                gemini_debug.get("gemini_response")
            ),
            cla_input_payload=self._coerce_dict(
                claude_debug.get("claude_request")
            ),
            cla_output_payload=self._coerce_dict(
                claude_debug.get("claude_response")
            ),
        )
        saved = self._repository.save(
            envelope=envelope,
            context=request.context,
            stored_file=stored_file,
        )

        # Step final: enriquecer obra_nombre / obra_direccion desde Sigrid (on-prem).
        # Esto sobrescribe los valores del merge recién persistido si la BBDD
        # Ruesma tiene la obra. Es best-effort: cualquier fallo queda en logs
        # y no propaga excepción al cliente HTTP.
        self._enrich_safely(merge_document_id=saved.document_id)

        return PersistAlbaranResult(
            ok=True,
            document_id=saved.document_id,
            sharepoint_url=saved.sharepoint_url,
            duplicate=False,
            stored_lines=saved.stored_lines,
        )

    def _enrich_safely(self, *, merge_document_id: str) -> None:
        """Llama al servicio de enriquecimiento capturando cualquier excepción."""
        logger.info(
            "[obra-enrichment][pipeline] pre-step: service_present=%s "
            "merge_document_id=%s",
            self._obra_enrichment_service is not None,
            merge_document_id,
        )
        if self._obra_enrichment_service is None:
            logger.warning(
                "[obra-enrichment][pipeline] SKIP: no hay servicio wire-ado. "
                "Revisa SIGRID_API_* en .env y obra_enrichment_service en build_app()."
            )
            return
        try:
            self._obra_enrichment_service.enrich_merge_document(
                merge_document_id=merge_document_id,
            )
        except Exception:
            logger.exception(
                "[obra-enrichment][pipeline] step falló; se continúa. document_id=%s",
                merge_document_id,
            )

    def _validate_sha256(
        self,
        *,
        envelope: ExtractionEnvelope,
        sha256: str,
    ) -> None:
        providers: list[tuple[str, ProviderExtractionEnvelope]] = [
            ("openai", envelope)
        ]
        if envelope.gemini is not None:
            providers.append(("gemini", envelope.gemini))
        if envelope.claude is not None:
            providers.append(("claude", envelope.claude))
        if envelope.google_document_ai is not None:
            providers.append(
                ("google_document_ai", envelope.google_document_ai)
            )
        if envelope.azure_document_intelligence is not None:
            providers.append(
                (
                    "azure_document_intelligence",
                    envelope.azure_document_intelligence,
                )
            )

        for provider_name, provider_envelope in providers:
            expected_sha = provider_envelope.meta.source_sha256.strip().lower()
            if expected_sha and expected_sha != sha256:
                raise ValueError(
                    "El sha256 del adjunto no coincide con el bloque "
                    f"{provider_name}."
                )

    def _normalize(self, envelope: ExtractionEnvelope) -> ExtractionEnvelope:
        normalized = envelope.model_copy(
            update={
                "data": self._normalizer.normalize_provider_document(
                    document=envelope.data,
                    provider_origin="openai",
                )
            }
        )

        provider_updates: dict[str, ProviderExtractionEnvelope] = {}
        optional_providers = {
            "gemini": normalized.gemini,
            "claude": normalized.claude,
            "google_document_ai": normalized.google_document_ai,
            "azure_document_intelligence": normalized.azure_document_intelligence,
        }
        for provider_name, provider_envelope in optional_providers.items():
            if provider_envelope is None:
                continue
            provider_updates[provider_name] = provider_envelope.model_copy(
                update={
                    "data": self._normalizer.normalize_provider_document(
                        document=provider_envelope.data,
                        provider_origin=provider_name,
                    )
                }
            )

        if provider_updates:
            normalized = normalized.model_copy(update=provider_updates)
        return normalized

    @staticmethod
    def _coerce_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}
