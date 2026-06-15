# application/services/contrato_enrichment_service.py
from __future__ import annotations

import dataclasses
import logging

from application.services.obra_code_normalizer import normalize_obra_code
from domain.models.contrato_models import ContratoEnrichmentResult
from domain.ports.contrato_cache_port import ContratoCachePort
from domain.ports.contrato_enrichment_port import ContratoEnrichmentClient
from domain.ports.contrato_merge_repository_port import ContratoMergeRepository
from domain.ports.contrato_pdf_storage_port import ContratoPdfStorage

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[contrato-enrichment]"


def _fecha_iso_a_yyyymmdd(fecha_iso: str | None) -> int | None:
    """Convierte una fecha en formato ISO ``YYYY-MM-DD`` al entero
    ``YYYYMMDD`` que usa Sigrid en sus columnas de vigencia.

    Tolera entradas con espacios y otros separadores raros
    (``"2026/03/11"`` por ejemplo). Devuelve ``None`` si la fecha
    no es interpretable como una fecha de 8 dígitos.
    """
    if not fecha_iso:
        return None
    cleaned = (
        fecha_iso.strip()
        .replace("-", "")
        .replace("/", "")
        .replace(".", "")
        .replace(" ", "")
    )
    if len(cleaned) != 8 or not cleaned.isdigit():
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


class ContratoEnrichmentService:
    """Orquesta la búsqueda y persistencia de contratos proveedor×obra.

    Flujo:
      1. Lee (cif, obra) del merge.
      2. Normaliza y valida.
      3. Llama a Sigrid → lista de contratos con ``gra_rep_ide``.
      4. Lee el MAPA de PDFs existentes ANTES del replace (para reutilizar
         los que no hayan cambiado de versión).
      5. Para cada contrato: si el ``gra_rep_ide`` coincide con el
         previamente guardado, inyecta los paths en el DTO para que
         ``replace_contratos`` los persista directamente.
      6. ``replace_contratos`` (borra + inserta todo).
      7. Para los contratos donde el ``gra_rep_ide`` cambió o es nuevo,
         descarga el PDF de Sigrid y lo sube a SharePoint. Tras cada
         upload, actualiza los paths en BBDD vía
         ``update_contrato_pdf_paths``.
      8. Si hay EXACTAMENTE 1 contrato, lo auto-selecciona.

    Best-effort a nivel de PDF: un fallo de descarga/subida no rompe
    el enrichment global. El contrato queda persistido sin PDF y la
    próxima ejecución reintenta.

    El storage es OPCIONAL: si no se inyecta, toda la lógica de PDF se
    omite y el servicio se comporta como la versión anterior.
    """

    def __init__(
        self,
        *,
        client: ContratoEnrichmentClient,
        repository: ContratoMergeRepository,
        cache: ContratoCachePort | None = None,
        pdf_storage: ContratoPdfStorage | None = None,
        enabled: bool = True,
    ) -> None:
        self._client = client
        self._repository = repository
        self._cache = cache
        self._pdf_storage = pdf_storage
        self._enabled = enabled
        logger.info(
            "%s ContratoEnrichmentService INSTANCIADO "
            "(enabled=%s client=%s repo=%s cache=%s pdf_storage=%s)",
            _LOG_PREFIX,
            enabled,
            type(client).__name__,
            type(repository).__name__,
            type(cache).__name__ if cache is not None else "None",
            type(pdf_storage).__name__ if pdf_storage is not None else "None",
        )

    def enrich_merge_document(
        self,
        *,
        merge_document_id: str,
        force_refetch: bool = False,
    ) -> int:
        """Asegura que el merge document tiene contratos asociados.

        Si ``force_refetch=False`` (por defecto): primero intenta hit
        en la caché por ``(codigo_obra, cif_proveedor, fecha_albaran)``
        usando vigencia. Si lo encuentra, copia el contrato cacheado
        directamente a ``albaran_contratos_merge`` y termina sin tocar
        Sigrid.

        Si ``force_refetch=True`` (botón "refrescar contrato" del
        front), siempre llama a Sigrid e ignora la caché para LECTURA,
        pero igualmente actualiza la caché con los datos frescos.
        """
        logger.info(
            "%s enrich_merge_document() INVOCADO document_id=%s force_refetch=%s",
            _LOG_PREFIX,
            merge_document_id,
            force_refetch,
        )

        if not self._enabled:
            return 0

        cif, obra_raw = self._repository.get_merge_cif_and_obra(
            document_id=merge_document_id,
        )
        cif_clean = (cif or "").strip().upper().replace(" ", "") or None
        obra_norm = normalize_obra_code(obra_raw)
        if not cif_clean or not obra_norm:
            logger.warning(
                "%s Faltan datos o no validan; se OMITE. cif=%r obra=%r",
                _LOG_PREFIX,
                cif_clean,
                obra_norm,
            )
            return 0

        # ----------------------------------------------------------------
        # Paso A — Cache lookup (si la caché está disponible y no se
        # fuerza refetch).
        # ----------------------------------------------------------------
        if self._cache is not None and not force_refetch:
            cached = self._try_cache_hit(
                merge_document_id=merge_document_id,
                codigo_obra=obra_norm,
                cif_proveedor=cif_clean,
            )
            if cached is not None:
                # Hit válido: el contrato ya está en albaran_contratos_merge.
                # Auto-select si lo recuperado es exactamente uno (siempre
                # lo es en cache hit: solo elegimos uno).
                try:
                    self._repository.set_selected_contrato(
                        document_id=merge_document_id,
                        codigo_contrato=cached.codigo_contrato,
                    )
                except Exception:
                    logger.exception(
                        "%s ERROR auto-seleccionando contrato cacheado.",
                        _LOG_PREFIX,
                    )
                # Canonización del nombre del proveedor TAMBIÉN en hit de
                # caché (jun 2026). Antes el nombre canónico solo llegaba
                # en el camino Sigrid; los hits de caché del pipeline
                # automático dejaban la cabecera con el nombre leído por
                # la IA (limitación conocida, ahora cerrada).
                self._canonize_proveedor_nombre_safely(
                    merge_document_id=merge_document_id,
                    nombre_proveedor=cached.nombre_proveedor,
                )
                return 1
            # Cache miss → continúa al flujo original (Sigrid).

        # ----------------------------------------------------------------
        # Paso B — Llamada a Sigrid (flujo original).
        # ----------------------------------------------------------------
        try:
            contratos = self._client.fetch_contratos(
                cif_proveedor=cif_clean,
                codigo_obra_normalizado=obra_norm,
            )
        except Exception:
            logger.exception("%s ERROR llamando a Sigrid.", _LOG_PREFIX)
            return 0

        logger.info(
            "%s Sigrid devolvió %s contrato(s)", _LOG_PREFIX, len(contratos)
        )

        # ------------------------------------------------------------ #
        # Canonización del nombre del proveedor (jun 2026).
        #
        # El SQL de contratos devuelve ahora la razón social CANÓNICA
        # del maestro (``prv.raz``). Al confirmarse que el CIF del merge
        # existe en Sigrid (hay contratos), sobrescribimos
        # ``proveedor_nombre`` de la cabecera con ese canónico — es la
        # pieza que el puerto ``update_merge_proveedor_nombre``
        # declaraba pero que nunca llegó a cablearse.
        # ------------------------------------------------------------ #
        if contratos:
            self._canonize_proveedor_nombre_safely(
                merge_document_id=merge_document_id,
                nombre_proveedor=contratos[0].nombre_proveedor,
            )

        # Paso 4: mapa de PDFs ya guardados ANTES del replace.
        # Si el repo no implementa get_existing_pdf_paths (caso de
        # compatibilidad hacia atrás con mocks), se queda vacío.
        existing_pdfs: dict[str, tuple[int | None, str | None, str | None]] = {}
        try:
            existing_pdfs = self._repository.get_existing_pdf_paths(
                document_id=merge_document_id,
            )
        except Exception:
            logger.exception(
                "%s No se pudo leer mapa de PDFs existentes; se ignora.",
                _LOG_PREFIX,
            )

        # Paso 4-bis (jun 2026): segundo nivel de reutilizacion — la
        # CACHE GLOBAL (contratos_cache). Cubre el caso "cambie de
        # contrato/obra y volvi": el replace de contratos del documento
        # pudo borrar las filas locales (y sus paths), pero la cache
        # global conserva el PDF por (obra, cif, codigo_contrato). Si el
        # gra_rep_ide sigue siendo el mismo, se reutiliza sin descargar.
        cache_pdfs: dict[str, tuple[int | None, str | None, str | None]] = {}
        if self._cache is not None:
            try:
                cache_pdfs = self._cache.get_pdf_paths_for_codigos(
                    codigo_obra=obra_norm,
                    cif_proveedor=cif_clean,
                    codigos=[c.codigo_contrato for c in contratos],
                )
            except AttributeError:
                # Implementaciones/mocks antiguos sin el metodo: se ignora.
                cache_pdfs = {}
            except Exception:
                logger.exception(
                    "%s No se pudo leer PDFs de la cache global; se ignora.",
                    _LOG_PREFIX,
                )

        # Paso 5: si el gra_rep_ide del contrato nuevo coincide con el
        # previo, reutilizamos los paths directamente en el DTO para
        # que el replace los persista sin tener que volver a subir.
        reused_count = 0
        contratos_with_maybe_reused: list[ContratoEnrichmentResult] = []
        pending_pdf_indices: list[int] = []  # índices en contratos_with_maybe_reused
        for idx, contrato in enumerate(contratos):
            # Nivel 1: PDFs ya guardados en ESTE documento. Nivel 2: la
            # cache global (otro albaran o una seleccion anterior). En
            # ambos casos la identidad es (codigo_contrato, gra_rep_ide):
            # si Sigrid cambio el documento del contrato (gra_rep_ide
            # distinto), NO se reutiliza y se vuelve a descargar.
            prev = existing_pdfs.get(contrato.codigo_contrato)
            if not (
                prev is not None
                and prev[0] is not None
                and contrato.gra_rep_ide is not None
                and prev[0] == contrato.gra_rep_ide
                and prev[1] is not None
            ):
                prev = cache_pdfs.get(contrato.codigo_contrato)
            if (
                prev is not None
                and prev[0] is not None
                and contrato.gra_rep_ide is not None
                and prev[0] == contrato.gra_rep_ide
                and prev[1] is not None
            ):
                # Reutilización: inyectamos paths previos en el DTO.
                contratos_with_maybe_reused.append(
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
                        gra_rep_ide=contrato.gra_rep_ide,
                        pdf_sharepoint_relative_path=prev[1],
                        pdf_sharepoint_web_url=prev[2],
                        lines=contrato.lines,
                    )
                )
                reused_count += 1
            else:
                contratos_with_maybe_reused.append(contrato)
                if contrato.gra_rep_ide is not None and self._pdf_storage is not None:
                    pending_pdf_indices.append(idx)

        # Paso 5-bis (jun 2026): si el documento tiene un contrato
        # SELECCIONADO y esta entre los recuperados, limitamos la
        # descarga de PDFs a ESE contrato. Los demas, si no se pudieron
        # reutilizar, quedan sin PDF hasta que alguien los seleccione
        # (se descargara entonces, en su propio re-fetch). Esto hace que
        # cambiar de contrato en el combo cueste UNA descarga como
        # maximo, en lugar de re-bajar todos los del proveedor+obra.
        # Sin seleccion (pipeline automatico), comportamiento original.
        selected_codigo: str | None = None
        try:
            selected_codigo = self._repository.get_selected_contrato_codigo(
                document_id=merge_document_id,
            )
        except Exception:  # noqa: BLE001
            selected_codigo = None
        if selected_codigo and any(
            c.codigo_contrato == selected_codigo
            for c in contratos_with_maybe_reused
        ):
            before = len(pending_pdf_indices)
            pending_pdf_indices = [
                i
                for i in pending_pdf_indices
                if contratos_with_maybe_reused[i].codigo_contrato
                == selected_codigo
            ]
            logger.info(
                "%s Seleccion activa (%s): descarga de PDFs limitada "
                "%s -> %s pendiente(s).",
                _LOG_PREFIX,
                selected_codigo,
                before,
                len(pending_pdf_indices),
            )

        logger.info(
            "%s PDFs reutilizados=%s pendientes_descargar=%s",
            _LOG_PREFIX,
            reused_count,
            len(pending_pdf_indices),
        )

        # Paso 6: replace atómico (con paths ya rellenos para reutilizados).
        try:
            self._repository.replace_contratos(
                document_id=merge_document_id,
                contratos=contratos_with_maybe_reused,
            )
        except Exception:
            logger.exception("%s ERROR guardando contratos.", _LOG_PREFIX)
            return 0

        # Paso 7: descargar + subir PDFs pendientes, y actualizar paths.
        #
        # IMPORTANTE: _download_and_store_pdf devuelve los paths
        # finales (relative_path, web_url) si la operación fue OK.
        # Los usamos para RECONSTRUIR el DTO en la lista local
        # (los DTOs son frozen=True, hay que crear uno nuevo). Sin
        # esto, el paso C (caché) verá pdf_sharepoint_* = None y
        # escribirá una caché que no sirve para reutilizar.
        if self._pdf_storage is not None:
            for idx in pending_pdf_indices:
                rel_path, web_url = self._download_and_store_pdf(
                    document_id=merge_document_id,
                    contrato=contratos_with_maybe_reused[idx],
                )
                if rel_path is None and web_url is None:
                    # Fallo en algún paso (descarga, subida o UPDATE):
                    # ya está logueado por el método; aquí no propagamos
                    # nada al DTO y la caché mantendrá None para este
                    # contrato (consistente con la realidad de BBDD).
                    continue
                old = contratos_with_maybe_reused[idx]
                # Reconstruimos el DTO con solo los 2 campos que cambian.
                # Usamos ``dataclasses.replace`` para no acoplar este
                # código a la lista completa de campos del DTO (que
                # puede haber crecido: sigrid_ide, gra_rep_ide, etc.).
                contratos_with_maybe_reused[idx] = dataclasses.replace(
                    old,
                    pdf_sharepoint_relative_path=rel_path,
                    pdf_sharepoint_web_url=web_url,
                )
        elif any(c.gra_rep_ide is not None for c in contratos_with_maybe_reused):
            logger.info(
                "%s Hay %s contrato(s) con PDF pero no se ha inyectado "
                "pdf_storage; se omite descarga/subida.",
                _LOG_PREFIX,
                sum(
                    1
                    for c in contratos_with_maybe_reused
                    if c.gra_rep_ide is not None
                ),
            )

        # ----------------------------------------------------------------
        # Paso C — Persistir en caché (mejora de rendimiento futura).
        # Esta operación es best-effort: un fallo aquí no impide el
        # éxito del enrichment porque ``albaran_contratos_merge`` ya
        # tiene los datos.
        # ----------------------------------------------------------------
        if self._cache is not None and contratos:
            try:
                self._cache.upsert_contratos(contratos=contratos_with_maybe_reused)
            except Exception:
                logger.exception(
                    "%s FALLO upsertando contratos en caché (no afecta "
                    "al enrichment).",
                    _LOG_PREFIX,
                )

        # Paso 8: auto-selección si hay uno solo.
        if len(contratos) == 1:
            codigo = contratos[0].codigo_contrato
            try:
                self._repository.set_selected_contrato(
                    document_id=merge_document_id,
                    codigo_contrato=codigo,
                )
            except Exception:
                logger.exception("%s ERROR auto-seleccionando contrato.", _LOG_PREFIX)

        # ------------------------------------------------------------ #
        # Paso 9 (jun 2026) — GARANTÍA de PDF para el contrato
        # seleccionado.
        #
        # De aquí en adelante: si tras todo el enrichment el contrato
        # SELECCIONADO sigue sin PDF en BBDD pero tiene gra_rep_ide,
        # forzamos UNA descarga de su PDF. Cubre los caminos en los que
        # los pasos 5-7 no lo bajaron (p.ej. cache-hit con PDF NULL que
        # no degradó a miss, o el contrato ya estaba en la fila pero sin
        # path). Sin esto, sv5 no descarga el PDF (Fase 1B desaparece) y
        # la línea base del hormigón se queda sin precio de contrato, y
        # además el botón "Abrir contrato en SharePoint" no aparece.
        #
        # Best-effort y barato: una sola descarga, solo del seleccionado,
        # solo si falta el PDF. El COALESCE del UPSERT garantiza que el
        # path escrito aquí ya no volverá a NULL en futuros enriquecimientos.
        # ------------------------------------------------------------ #
        self._ensure_pdf_for_selected_contrato(
            merge_document_id=merge_document_id,
            contratos=contratos_with_maybe_reused,
        )

        return len(contratos)

    # ------------------------------------------------------------------ #
    # Cache lookup helper
    # ------------------------------------------------------------------ #
    def _canonize_proveedor_nombre_safely(
        self,
        *,
        merge_document_id: str,
        nombre_proveedor: str | None,
    ) -> None:
        """Sobrescribe ``proveedor_nombre`` de la cabecera con la razón
        social canónica de Sigrid (``prv.raz``). Best-effort: cualquier
        fallo se loguea y NO rompe el enrichment.

        Tolerante a repositorios/mocks antiguos sin el método
        (AttributeError → no-op).
        """
        nombre = (nombre_proveedor or "").strip()
        if not nombre:
            return
        try:
            updater = getattr(
                self._repository, "update_merge_proveedor_nombre", None
            )
            if updater is None:
                logger.info(
                    "%s repo sin update_merge_proveedor_nombre; "
                    "canonización omitida. doc=%s",
                    _LOG_PREFIX,
                    merge_document_id,
                )
                return
            updater(
                document_id=merge_document_id,
                nombre_proveedor=nombre,
            )
        except Exception:
            logger.exception(
                "%s FALLO canonizando proveedor_nombre. doc=%s",
                _LOG_PREFIX,
                merge_document_id,
            )

    # ------------------------------------------------------------------ #
    # Garantía de PDF para el contrato seleccionado (jun 2026)
    # ------------------------------------------------------------------ #
    def _ensure_pdf_for_selected_contrato(
        self,
        *,
        merge_document_id: str,
        contratos: list[ContratoEnrichmentResult],
    ) -> None:
        """Si el contrato SELECCIONADO no tiene PDF en BBDD pero sí
        ``gra_rep_ide``, fuerza una descarga + subida + UPDATE.

        Best-effort: cualquier fallo se loguea y NO rompe el enrichment.
        No-op si no hay pdf_storage, si no hay selección, si el contrato
        seleccionado no está entre los recuperados, o si ya tiene PDF.
        """
        if self._pdf_storage is None:
            return

        try:
            selected_codigo = self._repository.get_selected_contrato_codigo(
                document_id=merge_document_id,
            )
        except Exception:  # noqa: BLE001
            selected_codigo = None
        if not selected_codigo:
            return

        # ¿Ya tiene PDF COMPLETO en BBDD? Necesitamos AMBOS:
        #   - relative_path → para que sv5 pueda descargar el PDF.
        #   - web_url       → para que el portal muestre el botón
        #                     "Abrir contrato en SharePoint".
        # La caché puede tener relative_path pero web_url=None (su filtro
        # solo exige relative_path). Si reutilizamos esa fila, el merge
        # se queda sin web_url y el botón no aparece aunque la valoración
        # funcione. Por eso re-descargamos si falta CUALQUIERA de los dos.
        try:
            existing = self._repository.get_existing_pdf_paths(
                document_id=merge_document_id,
            )
        except Exception:  # noqa: BLE001
            existing = {}
        prev = existing.get(selected_codigo)
        if prev is not None and prev[1] and prev[2]:
            # prev = (gra_rep_ide, relative_path, web_url) — completo.
            return

        # Buscamos el DTO del contrato seleccionado para tener su
        # gra_rep_ide (necesario para descargar de Sigrid).
        target = next(
            (c for c in contratos if c.codigo_contrato == selected_codigo),
            None,
        )
        if target is None or target.gra_rep_ide is None:
            logger.info(
                "%s Paso 9: contrato seleccionado %s sin gra_rep_ide o no "
                "recuperado; no se puede garantizar PDF. doc=%s",
                _LOG_PREFIX, selected_codigo, merge_document_id,
            )
            return

        logger.info(
            "%s Paso 9: contrato seleccionado %s con PDF incompleto "
            "(rel=%s web_url=%s); forzando descarga (gra_rep_ide=%s). doc=%s",
            _LOG_PREFIX, selected_codigo,
            bool(prev[1]) if prev else False,
            bool(prev[2]) if prev else False,
            target.gra_rep_ide, merge_document_id,
        )
        rel_path, web_url = self._download_and_store_pdf(
            document_id=merge_document_id,
            contrato=target,
        )
        if rel_path is None and web_url is None:
            logger.warning(
                "%s Paso 9: no se pudo garantizar el PDF del contrato %s. "
                "doc=%s",
                _LOG_PREFIX, selected_codigo, merge_document_id,
            )

    def _try_cache_hit(
        self,
        *,
        merge_document_id: str,
        codigo_obra: str,
        cif_proveedor: str,
    ) -> ContratoEnrichmentResult | None:
        """Intenta servir el contrato desde caché. Si hay hit, copia el
        contrato cacheado a ``albaran_contratos_merge`` con el
        ``document_id`` actual y devuelve el contrato encontrado.

        Devuelve ``None`` en cualquier caso de miss (incluyendo errores
        de lectura, fecha del albarán no parseable o ausencia de
        ``ContratoCachePort``).
        """
        # Necesitamos la fecha del albarán para evaluar la vigencia.
        try:
            fecha_iso = self._repository.get_merge_fecha(
                document_id=merge_document_id,
            )
        except AttributeError:
            # Fallback si el repositorio no tiene get_merge_fecha
            # (compatibilidad hacia atrás): cache miss.
            logger.info(
                "%s repo.get_merge_fecha no disponible; sin cache. doc=%s",
                _LOG_PREFIX,
                merge_document_id,
            )
            return None
        except Exception:
            logger.exception(
                "%s Fallo leyendo fecha del merge; cache miss. doc=%s",
                _LOG_PREFIX,
                merge_document_id,
            )
            return None

        fecha_int = _fecha_iso_a_yyyymmdd(fecha_iso)
        if fecha_int is None:
            logger.info(
                "%s fecha_albaran no parseable (%r); cache miss. doc=%s",
                _LOG_PREFIX,
                fecha_iso,
                merge_document_id,
            )
            return None

        try:
            assert self._cache is not None
            cached = self._cache.find_active_contrato(
                codigo_obra=codigo_obra,
                cif_proveedor=cif_proveedor,
                fecha_albaran_yyyymmdd=fecha_int,
            )
        except Exception:
            logger.exception(
                "%s Fallo consultando caché; se delega a Sigrid. doc=%s",
                _LOG_PREFIX,
                merge_document_id,
            )
            return None

        if cached is None:
            logger.info(
                "%s CACHE MISS obra=%s cif=%s fecha=%s. doc=%s",
                _LOG_PREFIX,
                codigo_obra,
                cif_proveedor,
                fecha_int,
                merge_document_id,
            )
            return None

        # Reparación de caché incompleta (jun 2026): si el contrato
        # cacheado NO tiene PDF en SharePoint y tenemos pdf_storage,
        # degradamos el hit a MISS para que el flujo siga a Sigrid,
        # aplique la selección de documentos vigente (Words combinados /
        # PDF sin audit-trail), suba el PDF y reescriba la caché ya
        # completa. Sin esto, una caché escrita cuando el PDF fallaba
        # (o cuando el criterio antiguo ignoraba los contratos cuyo
        # único documento es Word) propagaba "sin PDF" a TODOS los
        # albaranes futuros del mismo proveedor+obra, y la valoración
        # corría sin contrato (Fase 1B imposible).
        if (
            cached.pdf_sharepoint_relative_path is None
            and self._pdf_storage is not None
        ):
            logger.info(
                "%s CACHE HIT INCOMPLETO (sin PDF) obra=%s cif=%s "
                "codigo=%s gra_rep_ide=%s -> se trata como MISS para "
                "regenerar el PDF desde Sigrid. doc=%s",
                _LOG_PREFIX,
                codigo_obra,
                cif_proveedor,
                cached.codigo_contrato,
                cached.gra_rep_ide,
                merge_document_id,
            )
            return None

        # Hit: copiamos el contrato cacheado al merge usando
        # replace_contratos. Reusamos los paths del PDF si los tenía
        # cacheados (en cuyo caso ahorramos la subida a SharePoint).
        logger.info(
            "%s CACHE HIT obra=%s cif=%s fecha=%s -> codigo=%s "
            "vigencia=[%s, %s] fecha_alta=%s gra_rep_ide=%s pdf_path=%s. doc=%s",
            _LOG_PREFIX,
            codigo_obra,
            cif_proveedor,
            fecha_int,
            cached.codigo_contrato,
            cached.vigencia_desde,
            cached.vigencia_hasta,
            cached.fecha_alta_contrato,
            cached.gra_rep_ide,
            cached.pdf_sharepoint_relative_path,
            merge_document_id,
        )

        try:
            self._repository.replace_contratos(
                document_id=merge_document_id,
                contratos=[cached],
            )
        except Exception:
            logger.exception(
                "%s FALLO escribiendo contrato cacheado al merge. doc=%s",
                _LOG_PREFIX,
                merge_document_id,
            )
            return None

        return cached

    def _download_and_store_pdf(
        self,
        *,
        document_id: str,
        contrato: ContratoEnrichmentResult,
    ) -> tuple[str | None, str | None]:
        """Descarga + sube + actualiza BBDD para un contrato concreto.

        Silencia todas las excepciones: el objetivo es que un contrato
        con problema de PDF no impida procesar los otros. Lo grave se
        loguea como exception; lo esperado como warning.

        Returns
        -------
        tuple[str | None, str | None]
            ``(relative_path, web_url)`` si la descarga + subida + UPDATE
            de BBDD ha ido bien. ``(None, None)`` en cualquier otro
            caso (sin pdf_storage, sin gra_rep_ide, fallo de descarga,
            fallo de subida o fallo de UPDATE).

            El caller debe usar estos paths para reconstruir el DTO en
            memoria (``ContratoEnrichmentResult`` es ``frozen=True``,
            no se puede mutar). De lo contrario, el paso C (caché) verá
            ``pdf_sharepoint_* = None`` y persistirá una caché inútil.
        """
        if self._pdf_storage is None:
            return None, None
        if contrato.gra_rep_ide is None:
            return None, None

        try:
            payload = self._client.download_contrato_pdf(
                gra_rep_ide=contrato.gra_rep_ide,
            )
        except Exception:
            logger.exception(
                "%s FALLO descarga PDF. codigo=%s gra_rep_ide=%s",
                _LOG_PREFIX,
                contrato.codigo_contrato,
                contrato.gra_rep_ide,
            )
            return None, None

        if payload is None:
            logger.warning(
                "%s Descarga PDF devolvió vacío. codigo=%s gra_rep_ide=%s",
                _LOG_PREFIX,
                contrato.codigo_contrato,
                contrato.gra_rep_ide,
            )
            return None, None

        try:
            stored = self._pdf_storage.upload_contrato_pdf(
                filename=payload.filename,
                file_bytes=payload.content,
                codigo_contrato=contrato.codigo_contrato,
                gra_rep_ide=contrato.gra_rep_ide,
            )
        except Exception:
            logger.exception(
                "%s FALLO subida PDF a SharePoint. codigo=%s gra_rep_ide=%s",
                _LOG_PREFIX,
                contrato.codigo_contrato,
                contrato.gra_rep_ide,
            )
            return None, None

        try:
            self._repository.update_contrato_pdf_paths(
                document_id=document_id,
                codigo_contrato=contrato.codigo_contrato,
                relative_path=stored.relative_path,
                web_url=stored.web_url,
            )
            logger.info(
                "%s PDF OK codigo=%s gra_rep_ide=%s -> %s",
                _LOG_PREFIX,
                contrato.codigo_contrato,
                contrato.gra_rep_ide,
                stored.relative_path,
            )
        except Exception:
            logger.exception(
                "%s FALLO persistiendo paths del PDF. codigo=%s",
                _LOG_PREFIX,
                contrato.codigo_contrato,
            )
            return None, None

        # ---- Markdown del contrato (markitdown / LibreOffice) ----
        # Se sube junto al PDF para que la IA (sv5) lo consuma. Best-effort:
        # si falla, el contrato sigue valiendo (se mantiene el PDF). El
        # ``markdown`` lo genera el cliente Sigrid al combinar las fuentes.
        md_text = getattr(payload, "markdown", None)
        if md_text:
            try:
                stored_md = self._pdf_storage.upload_contrato_md(
                    markdown=md_text,
                    codigo_contrato=contrato.codigo_contrato,
                    gra_rep_ide=contrato.gra_rep_ide,
                )
                logger.info(
                    "%s MD OK codigo=%s -> %s (%s chars)",
                    _LOG_PREFIX,
                    contrato.codigo_contrato,
                    stored_md.relative_path,
                    len(md_text),
                )
                # Persistimos el path del MD si el repositorio lo soporta
                # (defensivo: funciona aunque aún no exista el método).
                _persist_md = getattr(
                    self._repository, "update_contrato_md_paths", None
                )
                if callable(_persist_md):
                    _persist_md(
                        document_id=document_id,
                        codigo_contrato=contrato.codigo_contrato,
                        relative_path=stored_md.relative_path,
                        web_url=stored_md.web_url,
                    )
            except Exception:
                logger.exception(
                    "%s FALLO subiendo/persistiendo MD codigo=%s (se continua).",
                    _LOG_PREFIX,
                    contrato.codigo_contrato,
                )
        else:
            logger.info(
                "%s sin markdown en el payload del contrato codigo=%s "
                "(revisa que LibreOffice/markitdown esten disponibles).",
                _LOG_PREFIX,
                contrato.codigo_contrato,
            )

        # El UPDATE de BBDD ha ido bien. Devolvemos los paths al caller
        # para que reconstruya el DTO en memoria. Si NO los propagara,
        # el paso C (caché) escribiría pdf_sharepoint_* = None en
        # contratos_cache y rompería la reutilización de PDFs en
        # albaranes futuros que apunten al mismo contrato.
        return stored.relative_path, stored.web_url
