# application/pipelines/persist_albaran_pipeline.py
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict

from application.services.albaran_normalizer import AlbaranNormalizer
from application.services.contrato_enrichment_service import (
    ContratoEnrichmentService,
)
from application.services.obra_enrichment_service import ObraEnrichmentService
from domain.models.extraction_models import (
    ExtractionEnvelope,
    ProviderExtractionEnvelope,
)
from domain.ports.albaran_repository import AlbaranRepository
from domain.ports.document_storage import DocumentStorage
from domain.ports.valuation_trigger_port import ValuationTrigger

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
    """Resultado del persist + enrichment.

    Campos NUEVOS añadidos para que el orquestador (sv7) pueda decidir
    el siguiente paso (valoración o esperar al revisor) sin tener que
    consultar BBDD él mismo:

      - ``contratos_count``: nº de contratos persistidos en
        albaran_contratos_merge para este documento (después del
        enrichment).
      - ``selected_contrato_codigo``: código auto-seleccionado por el
        ContratoEnrichmentService cuando hay exactamente 1 contrato.
        ``None`` si hay 0 ó >1 contratos (o si el enrichment falló).
    """
    ok: bool
    document_id: str
    sharepoint_url: str | None
    duplicate: bool
    stored_lines: int
    contratos_count: int = 0
    selected_contrato_codigo: str | None = None


class PersistAlbaranPipeline:
    def __init__(
        self,
        *,
        repository: AlbaranRepository,
        document_storage: DocumentStorage,
        normalizer: AlbaranNormalizer,
        obra_enrichment_service: ObraEnrichmentService | None = None,
        contrato_enrichment_service: ContratoEnrichmentService | None = None,
        valuation_trigger: ValuationTrigger | None = None,
    ) -> None:
        self._repository = repository
        self._document_storage = document_storage
        self._normalizer = normalizer
        self._obra_enrichment_service = obra_enrichment_service
        self._contrato_enrichment_service = contrato_enrichment_service
        self._valuation_trigger = valuation_trigger
        logger.info(
            "[pipeline] PersistAlbaranPipeline construido; "
            "obra_enrichment=%s contrato_enrichment=%s "
            "valuation_trigger=%s",
            "PRESENTE" if obra_enrichment_service is not None else "None",
            "PRESENTE" if contrato_enrichment_service is not None else "None",
            "PRESENTE" if valuation_trigger is not None else "None",
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
            # Re-enriquecer para idempotencia: si la BBDD on-prem cambió,
            # el merge se actualiza. No duplica contratos (replace).
            self._enrich_obra_safely(merge_document_id=existing.document_id)
            contratos_count = self._enrich_contratos_safely(
                merge_document_id=existing.document_id,
            )
            self._trigger_valuation_safely(
                merge_document_id=existing.document_id,
            )
            selected_codigo = self._read_selected_contrato_safely(
                merge_document_id=existing.document_id,
            )
            return PersistAlbaranResult(
                ok=True,
                document_id=existing.document_id,
                sharepoint_url=existing.sharepoint_url,
                duplicate=True,
                stored_lines=existing.stored_lines,
                contratos_count=contratos_count,
                selected_contrato_codigo=selected_codigo,
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

        # Pipeline de enriquecimiento. ORDEN IMPORTA:
        #  1) Obra: sobrescribe nombre_obra y obra_direccion en el merge.
        #  2) Contratos: busca por (cif, obra) e inserta en
        #     albaran_contratos_merge. Si hay 1 solo contrato lo auto-
        #     selecciona en selected_contrato_codigo.
        #  3) Valoración: si hay selected_contrato_codigo con líneas,
        #     dispara /run-async del servicio 6 (fire-and-forget).
        # Todos los pasos son best-effort y no rompen la persistencia.
        self._enrich_obra_safely(merge_document_id=saved.document_id)
        contratos_count = self._enrich_contratos_safely(
            merge_document_id=saved.document_id,
        )
        self._trigger_valuation_safely(merge_document_id=saved.document_id)
        selected_codigo = self._read_selected_contrato_safely(
            merge_document_id=saved.document_id,
        )

        logger.info(
            "[pipeline] FIN persist document_id=%s duplicate=False "
            "contratos_count=%d selected_contrato_codigo=%s",
            saved.document_id,
            contratos_count,
            selected_codigo,
        )

        return PersistAlbaranResult(
            ok=True,
            document_id=saved.document_id,
            sharepoint_url=saved.sharepoint_url,
            duplicate=False,
            stored_lines=saved.stored_lines,
            contratos_count=contratos_count,
            selected_contrato_codigo=selected_codigo,
        )

    def _enrich_obra_safely(self, *, merge_document_id: str) -> None:
        logger.info(
            "[obra-enrichment][pipeline] pre-step: service_present=%s "
            "merge_document_id=%s",
            self._obra_enrichment_service is not None,
            merge_document_id,
        )
        if self._obra_enrichment_service is None:
            logger.warning(
                "[obra-enrichment][pipeline] SKIP: no hay servicio wire-ado."
            )
            return
        try:
            self._obra_enrichment_service.enrich_merge_document(
                merge_document_id=merge_document_id,
            )
        except Exception:
            logger.exception(
                "[obra-enrichment][pipeline] step falló; se continúa. "
                "document_id=%s",
                merge_document_id,
            )

    def _enrich_contratos_safely(self, *, merge_document_id: str) -> int:
        """Devuelve el nº de contratos persistidos (0 si falló o no hay servicio)."""
        logger.info(
            "[contrato-enrichment][pipeline] pre-step: service_present=%s "
            "merge_document_id=%s",
            self._contrato_enrichment_service is not None,
            merge_document_id,
        )
        if self._contrato_enrichment_service is None:
            logger.warning(
                "[contrato-enrichment][pipeline] SKIP: no hay servicio wire-ado."
            )
            return 0
        try:
            count = self._contrato_enrichment_service.enrich_merge_document(
                merge_document_id=merge_document_id,
            )
            return int(count) if count is not None else 0
        except Exception:
            logger.exception(
                "[contrato-enrichment][pipeline] step falló; se continúa. "
                "document_id=%s",
                merge_document_id,
            )
            return 0

    def _read_selected_contrato_safely(
        self,
        *,
        merge_document_id: str,
    ) -> str | None:
        """Lee selected_contrato_codigo del merge tras el enrichment.

        ``ContratoEnrichmentService`` auto-selecciona el código en BBDD
        cuando hay exactamente 1 contrato. Aquí lo leemos para incluirlo
        en la respuesta del API y que el orquestador decida.
        Best-effort: si falla, devolvemos None (sv7 entonces irá a
        awaiting_contract_selection).
        """
        try:
            return self._repository.get_selected_contrato_codigo(
                document_id=merge_document_id,
            )
        except KeyError:
            return None
        except Exception:
            logger.exception(
                "[pipeline] no se pudo leer selected_contrato_codigo "
                "tras el enrichment; se devuelve None. document_id=%s",
                merge_document_id,
            )
            return None

    def _trigger_valuation_safely(self, *, merge_document_id: str) -> None:
        """Dispara el servicio 6 SOLO si hay contrato seleccionado con líneas.

        SIEMPRE best-effort: cualquier fallo se loguea y no rompe el pipeline
        de persistencia. El front puede pulsar 'Valorar' manualmente como
        fallback si aquí falla algo.
        """
        logger.info(
            "[valuation-trigger][pipeline] pre-step: trigger_present=%s "
            "merge_document_id=%s",
            self._valuation_trigger is not None,
            merge_document_id,
        )
        if self._valuation_trigger is None:
            logger.info(
                "[valuation-trigger][pipeline] SKIP: trigger no cableado."
            )
            return

        try:
            has_lines, codigo = self._repository.has_selected_contrato_with_lines(
                document_id=merge_document_id,
            )
        except Exception:
            logger.exception(
                "[valuation-trigger][pipeline] Error comprobando líneas "
                "contrato; se omite trigger. document_id=%s",
                merge_document_id,
            )
            return

        if not has_lines:
            logger.info(
                "[valuation-trigger][pipeline] SKIP: no hay contrato "
                "seleccionado con líneas. document_id=%s codigo=%s",
                merge_document_id, codigo,
            )
            return

        try:
            accepted = self._valuation_trigger.trigger_async(
                document_id=merge_document_id,
                codigo_contrato=codigo,
                force=False,
            )
            logger.info(
                "[valuation-trigger][pipeline] trigger resultado=%s "
                "document_id=%s codigo=%s",
                "ACCEPTED" if accepted else "REJECTED",
                merge_document_id, codigo,
            )
        except Exception:
            logger.exception(
                "[valuation-trigger][pipeline] fallo inesperado al "
                "disparar trigger. document_id=%s",
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