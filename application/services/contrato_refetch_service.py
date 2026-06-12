# application/services/contrato_refetch_service.py
from __future__ import annotations

import logging

from application.services.contrato_enrichment_service import (
    ContratoEnrichmentService,
)
from application.services.obra_code_normalizer import normalize_obra_code
from domain.models.contrato_refetch_models import ContratoRefetchOutcome
from domain.ports.contrato_merge_repository_port import ContratoMergeRepository

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[contrato-refetch]"


class ContratoRefetchService:
    """Wrapper fino sobre :class:`ContratoEnrichmentService` para el caso
    de uso "el usuario pulsó 'Volver a buscar' (o eligió contrato en el
    combo) en el portal".

    El sv3 ya tiene la lógica completa de búsqueda + persistencia
    (``ContratoEnrichmentService``) que se ejecuta automáticamente en
    el pipeline al persistir un albarán. Cuando el usuario edita CIF
    u obra en el portal y pulsa "Volver a buscar", el sv4 invoca al
    sv3 vía HTTP, y el sv3 reutiliza esa misma lógica forzando
    ``force_refetch=True`` (saltarse la caché de contratos, ir
    directos a Sigrid).

    AMPLIACIÓN (jun 2026) — canonización de cabecera en el refetch:

    El bug reportado era que, al elegir en el portal un proveedor o una
    obra que SÍ existen en Sigrid, el nombre del proveedor y el
    nombre/dirección de la obra no se actualizaban con los datos del
    ERP. La causa: este servicio solo ejecutaba el enrichment de
    CONTRATOS; el de OBRA (que escribe ``obra_nombre`` +
    ``obra_direccion``) solo corría en el pipeline automático, y la
    canonización del nombre de proveedor no corría en ningún sitio.

    Ahora, ANTES de buscar contratos:

      0a. Si hay ``obra_enrichment`` inyectado y el código de obra es
          válido → refresca ``obra_nombre`` y ``obra_direccion`` desde
          Sigrid (fuente de verdad del maestro de obras).
      0b. Si hay ``proveedor_client`` inyectado y el CIF es válido →
          lookup determinista en el maestro ``prv``; si el CIF existe,
          sobrescribe ``proveedor_nombre`` con la razón social
          canónica (``prv.raz``) AUNQUE luego no haya contratos para
          esa combinación CIF+obra.

    Ambos pasos son best-effort: un fallo se loguea y el refetch de
    contratos continúa igual que antes. Si los colaboradores no se
    inyectan (Sigrid no cableado), el comportamiento es el histórico.

    La diferencia con respecto a llamar directamente al enrichment es
    que aquí necesitamos devolver un ``ContratoRefetchOutcome`` con
    semántica rica (``status``, ``message``, ``selected_contrato_codigo``)
    para que el front pinte feedback útil al usuario. Este servicio
    ÚNICAMENTE se encarga de esa traducción int → outcome; no duplica
    la lógica de negocio.
    """

    def __init__(
        self,
        *,
        enrichment: ContratoEnrichmentService,
        repository: ContratoMergeRepository,
        obra_enrichment=None,
        proveedor_client=None,
    ) -> None:
        self._enrichment = enrichment
        self._repository = repository
        self._obra_enrichment = obra_enrichment
        self._proveedor_client = proveedor_client
        logger.info(
            "%s INSTANCIADO (obra_enrichment=%s proveedor_client=%s)",
            _LOG_PREFIX,
            "PRESENTE" if obra_enrichment is not None else "None",
            "PRESENTE" if proveedor_client is not None else "None",
        )

    def refetch(self, *, document_id: str) -> ContratoRefetchOutcome:
        """Re-busca contratos para un documento ya persistido.

        Idempotente. El usuario puede pulsar "Volver a buscar" tantas
        veces como quiera; cada pulsación llama a Sigrid (force_refetch)
        y hace UPSERT por ``sigrid_ide`` en BBDD.
        """
        logger.info(
            "%s refetch() INVOCADO document_id=%s",
            _LOG_PREFIX, document_id,
        )

        # Lectura previa para feedback al usuario (CIF + obra normalizada).
        try:
            cif, obra_raw = self._repository.get_merge_cif_and_obra(
                document_id=document_id,
            )
        except KeyError:
            # Documento inexistente → propagamos para que el endpoint
            # devuelva 404. NO devolvemos outcome porque no es un caso
            # de "Sigrid no encontró", es un error de input.
            raise

        cif_clean = (cif or "").strip().upper().replace(" ", "") or None
        obra_norm = normalize_obra_code(obra_raw)

        # ---------------------------------------------------------- #
        # Paso 0a (jun 2026) — refrescar OBRA desde el maestro.
        # Escribe obra_nombre + obra_direccion canónicos en el merge.
        # ---------------------------------------------------------- #
        if self._obra_enrichment is not None and obra_norm:
            try:
                self._obra_enrichment.enrich_merge_document(
                    merge_document_id=document_id,
                )
            except Exception:
                logger.exception(
                    "%s FALLO refrescando obra (best-effort). doc=%s",
                    _LOG_PREFIX, document_id,
                )

        # ---------------------------------------------------------- #
        # Paso 0b (jun 2026) — canonizar PROVEEDOR por CIF exacto.
        # Independiente de que existan contratos: si el CIF está en el
        # maestro ``prv``, el nombre del merge pasa a ser ``prv.raz``.
        # ---------------------------------------------------------- #
        if self._proveedor_client is not None and cif_clean:
            self._canonize_proveedor_por_cif(
                document_id=document_id,
                cif_clean=cif_clean,
            )

        if not cif_clean or not obra_norm:
            logger.info(
                "%s SKIP missing data document_id=%s cif=%r obra=%r",
                _LOG_PREFIX, document_id, cif_clean, obra_norm,
            )
            return ContratoRefetchOutcome(
                status="skipped_missing_data",
                count=0,
                selected_contrato_codigo=None,
                message=(
                    "Faltan CIF u obra (o no son válidos). "
                    "Comprueba ambos campos y vuelve a intentarlo."
                ),
                cif=cif_clean,
                obra_codigo=obra_norm,
            )

        # Delegación al enrichment del sv3 con force_refetch=True para
        # saltarse la caché de contratos y consultar Sigrid en vivo.
        # ``enrich_merge_document`` ya hace UPSERT por sigrid_ide,
        # auto-selección si exactamente 1, best-effort para PDFs y
        # canonización del nombre de proveedor cuando hay contratos.
        try:
            count = self._enrichment.enrich_merge_document(
                merge_document_id=document_id,
                force_refetch=True,
            )
        except Exception as exc:
            # No queremos propagar excepciones internas al front. La
            # traducimos a un outcome con status sigrid_error para que
            # el usuario sepa que algo falló en Sigrid (y los contratos
            # previos siguen intactos en BBDD).
            logger.exception(
                "%s ERROR en enrichment document_id=%s",
                _LOG_PREFIX, document_id,
            )
            return ContratoRefetchOutcome(
                status="sigrid_error",
                count=0,
                selected_contrato_codigo=None,
                message=(
                    "Error consultando Sigrid. Inténtalo de nuevo en "
                    f"unos minutos. Detalle técnico: {exc.__class__.__name__}"
                ),
                cif=cif_clean,
                obra_codigo=obra_norm,
            )

        # Tras el enrichment, leemos el estado final del merge para
        # detectar si hubo auto-selección.
        selected_codigo: str | None = None
        try:
            selected_codigo = self._repository.get_selected_contrato_codigo(
                document_id=document_id,
            )
        except Exception:
            logger.exception(
                "%s No pudimos leer selected_contrato_codigo tras refetch "
                "document_id=%s. Continuamos con outcome sin selección.",
                _LOG_PREFIX, document_id,
            )

        if count == 0:
            return ContratoRefetchOutcome(
                status="no_results",
                count=0,
                selected_contrato_codigo=None,
                message=(
                    f"No se encontró ningún contrato en el ERP para la "
                    f"combinación CIF {cif_clean} + obra {obra_norm}. "
                    "Revisa que ambos valores sean correctos."
                ),
                cif=cif_clean,
                obra_codigo=obra_norm,
            )

        if count == 1:
            return ContratoRefetchOutcome(
                status="found_single",
                count=1,
                selected_contrato_codigo=selected_codigo,
                message=(
                    f"Encontrado 1 contrato ({selected_codigo or '?'}); "
                    "se ha seleccionado automáticamente."
                ),
                cif=cif_clean,
                obra_codigo=obra_norm,
            )

        return ContratoRefetchOutcome(
            status="found_multiple",
            count=count,
            selected_contrato_codigo=selected_codigo,
            message=(
                f"Se encontraron {count} contratos. Selecciona el "
                "correcto en el portal."
            ),
            cif=cif_clean,
            obra_codigo=obra_norm,
        )

    # ----------------------------------------------------------------- #
    # Helpers privados
    # ----------------------------------------------------------------- #
    def _canonize_proveedor_por_cif(
        self,
        *,
        document_id: str,
        cif_clean: str,
    ) -> None:
        """Lookup determinista en ``prv`` por CIF y escritura del nombre
        canónico en el merge. Best-effort y tolerante a clientes/repos
        antiguos sin los métodos nuevos.
        """
        try:
            fetcher = getattr(
                self._proveedor_client, "fetch_proveedor_by_cif", None
            )
            if fetcher is None:
                logger.info(
                    "%s proveedor_client sin fetch_proveedor_by_cif; "
                    "canonización omitida. doc=%s",
                    _LOG_PREFIX, document_id,
                )
                return
            found = fetcher(cif=cif_clean)
            if found is None:
                logger.info(
                    "%s CIF %s no existe en prv; nombre sin tocar. doc=%s",
                    _LOG_PREFIX, cif_clean, document_id,
                )
                return
            _cif_canon, nombre_canon = found
            updater = getattr(
                self._repository, "update_merge_proveedor_nombre", None
            )
            if updater is None or not (nombre_canon or "").strip():
                return
            updater(
                document_id=document_id,
                nombre_proveedor=nombre_canon,
            )
        except Exception:
            logger.exception(
                "%s FALLO canonizando proveedor por CIF (best-effort). "
                "doc=%s cif=%s",
                _LOG_PREFIX, document_id, cif_clean,
            )
