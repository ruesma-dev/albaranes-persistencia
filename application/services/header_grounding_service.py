# application/services/header_grounding_service.py
"""Grounding DETERMINISTA de la cabecera de fase 1 contra Sigrid.

Consumido por el endpoint ``POST /v1/sigrid/header-grounding`` (que a
su vez llama el orquestador sv7 ANTES de lanzar la 2ª IA de revisión
en sv2).

Contrato funcional (validado con el usuario):

  1. PROVEEDOR — lookup determinista por CIF exacto contra el maestro
     ``prv``. Si el CIF existe:
       * status='validated', se devuelve el CIF canónico y la razón
         social canónica (``prv.raz``).
       * La 2ª IA NO debe revisar el bloque proveedor (eso se le
         instruye vía prompt) y sv7 además sobreescribe los campos de
         forma determinista tras la fase 2.
     Si el CIF NO existe (o no llegó):
       * status='not_found' / 'skipped'. El bloque proveedor SÍ entra
         en la revisión IA, acompañado de candidatos (proveedores con
         contrato en la obra validada, si la hay) para que la IA case
         "maco tran S.L." con la entidad real del ERP.

  2. OBRA — lookup determinista por código normalizado (4 dígitos).
     Si existe → status='validated' con nombre + dirección canónicos.
     Si no → status='not_found'/'skipped' + lista de obras candidatas
     (código + nombre) para que la IA resuelva por texto.

Arquitectura: este servicio NO toca BBDD del merge (es de SOLO
lectura contra Sigrid); vive en application porque orquesta dos
puertos de infraestructura (obra client + contrato client) y aplica
las reglas de negocio del grounding. Best-effort interno: cada lookup
captura sus excepciones y degrada a 'skipped' con nota en ``errors``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from application.services.obra_code_normalizer import normalize_obra_code

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[header-grounding]"


@dataclass(frozen=True)
class HeaderGroundingRequest:
    """Cabecera tal como la extrajo la 1ª IA (todo opcional)."""

    proveedor_cif: str | None = None
    proveedor_nombre: str | None = None
    obra_codigo: str | None = None
    obra_nombre: str | None = None
    obra_direccion: str | None = None


@dataclass(frozen=True)
class HeaderGroundingResponse:
    """Respuesta serializable a JSON (asdict) para sv7/sv2."""

    proveedor: dict[str, Any]
    obra: dict[str, Any]
    obras_candidatas: list[dict[str, Any]] = field(default_factory=list)
    proveedores_candidatos: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class HeaderGroundingService:
    """Orquesta los lookups deterministas de proveedor y obra."""

    def __init__(
        self,
        *,
        obra_client,
        proveedor_client,
        max_obras_candidatas: int = 300,
        max_proveedores_candidatos: int = 200,
        enabled: bool = True,
    ) -> None:
        """``obra_client``  → SigridApiObraClient
        (``fetch_obra_by_codigo``, ``search_obras``).
        ``proveedor_client`` → SigridApiContratoClient
        (``fetch_proveedor_by_cif``, ``fetch_proveedores_por_obra`` si
        existe, ``search_proveedores`` como fallback).

        Los topes de candidatos protegen el tamaño del prompt de fase 2
        (límite práctico de tokens). Configurables por ENV en app.py.
        """
        self._obra_client = obra_client
        self._proveedor_client = proveedor_client
        self._max_obras = int(max_obras_candidatas)
        self._max_proveedores = int(max_proveedores_candidatos)
        self._enabled = enabled
        logger.info(
            "%s INSTANCIADO (enabled=%s max_obras=%s max_proveedores=%s)",
            _LOG_PREFIX, enabled, self._max_obras, self._max_proveedores,
        )

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    def ground(
        self, request: HeaderGroundingRequest
    ) -> HeaderGroundingResponse:
        if not self._enabled:
            return HeaderGroundingResponse(
                proveedor={"status": "skipped", "reason": "disabled"},
                obra={"status": "skipped", "reason": "disabled"},
            )

        errors: list[str] = []

        obra_block, obra_validated_codigo = self._ground_obra(
            request, errors
        )
        proveedor_block, proveedor_validated = self._ground_proveedor(
            request, errors
        )

        obras_candidatas: list[dict[str, Any]] = []
        proveedores_candidatos: list[dict[str, Any]] = []

        # Candidatos SOLO para lo NO validado (ahorra tokens del prompt
        # y evita tentar a la IA a "mejorar" lo ya determinista).
        if obra_block.get("status") != "validated":
            obras_candidatas = self._list_obras_candidatas(errors)

        if proveedor_block.get("status") != "validated":
            proveedores_candidatos = self._list_proveedores_candidatos(
                obra_codigo_validado=obra_validated_codigo,
                errors=errors,
            )

        logger.info(
            "%s ground() proveedor=%s obra=%s candidatas_obras=%d "
            "candidatos_proveedores=%d errors=%d",
            _LOG_PREFIX,
            proveedor_block.get("status"),
            obra_block.get("status"),
            len(obras_candidatas),
            len(proveedores_candidatos),
            len(errors),
        )

        return HeaderGroundingResponse(
            proveedor=proveedor_block,
            obra=obra_block,
            obras_candidatas=obras_candidatas,
            proveedores_candidatos=proveedores_candidatos,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # Bloque OBRA
    # ------------------------------------------------------------------ #
    def _ground_obra(
        self,
        request: HeaderGroundingRequest,
        errors: list[str],
    ) -> tuple[dict[str, Any], str | None]:
        """Devuelve (bloque_obra, codigo_validado | None)."""
        normalized = normalize_obra_code(request.obra_codigo)
        if normalized is None:
            return (
                {
                    "status": "skipped",
                    "reason": "obra_codigo ausente o no normalizable",
                    "codigo_leido": request.obra_codigo,
                    "nombre_leido": request.obra_nombre,
                },
                None,
            )
        try:
            result = self._obra_client.fetch_obra_by_codigo(
                codigo_obra_normalizado=normalized,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            errors.append(f"obra_lookup: {type(exc).__name__}")
            logger.exception(
                "%s fetch_obra_by_codigo FALLO codigo=%s",
                _LOG_PREFIX, normalized,
            )
            return (
                {"status": "skipped", "reason": "error consultando Sigrid"},
                None,
            )

        if result is None:
            return (
                {
                    "status": "not_found",
                    "codigo_leido": normalized,
                    "nombre_leido": request.obra_nombre,
                },
                None,
            )

        return (
            {
                "status": "validated",
                "match_method": "codigo_exact",
                "codigo": normalized,
                "nombre": (result.nombre_obra or "").strip() or None,
                "direccion": result.direccion_completa,
            },
            normalized,
        )

    # ------------------------------------------------------------------ #
    # Bloque PROVEEDOR
    # ------------------------------------------------------------------ #
    def _ground_proveedor(
        self,
        request: HeaderGroundingRequest,
        errors: list[str],
    ) -> tuple[dict[str, Any], bool]:
        cif_clean = (
            (request.proveedor_cif or "").strip().upper().replace(" ", "")
        )
        if not cif_clean:
            return (
                {
                    "status": "skipped",
                    "reason": "proveedor_cif ausente",
                    "nombre_leido": request.proveedor_nombre,
                },
                False,
            )
        try:
            found = self._proveedor_client.fetch_proveedor_by_cif(
                cif=cif_clean,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            errors.append(f"proveedor_lookup: {type(exc).__name__}")
            logger.exception(
                "%s fetch_proveedor_by_cif FALLO cif=%s",
                _LOG_PREFIX, cif_clean,
            )
            return (
                {"status": "skipped", "reason": "error consultando Sigrid"},
                False,
            )

        if found is None:
            return (
                {
                    "status": "not_found",
                    "cif_leido": cif_clean,
                    "nombre_leido": request.proveedor_nombre,
                },
                False,
            )

        cif_canon, nombre_canon = found
        return (
            {
                "status": "validated",
                "match_method": "cif_exact",
                "cif": cif_canon,
                "nombre_canonico": nombre_canon,
                "nombre_leido": request.proveedor_nombre,
            },
            True,
        )

    # ------------------------------------------------------------------ #
    # Candidatos
    # ------------------------------------------------------------------ #
    def _list_obras_candidatas(
        self, errors: list[str]
    ) -> list[dict[str, Any]]:
        try:
            obras = self._obra_client.search_obras()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"obras_candidatas: {type(exc).__name__}")
            logger.exception("%s search_obras FALLO", _LOG_PREFIX)
            return []
        out: list[dict[str, Any]] = []
        for obra in obras or []:
            codigo = (getattr(obra, "codigo_obra", None) or "").strip()
            nombre = (getattr(obra, "nombre_obra", None) or "").strip()
            if not codigo:
                continue
            out.append({"codigo": codigo, "nombre": nombre or None})
            if len(out) >= self._max_obras:
                break
        return out

    def _list_proveedores_candidatos(
        self,
        *,
        obra_codigo_validado: str | None,
        errors: list[str],
    ) -> list[dict[str, Any]]:
        """Proveedores candidatos para que la IA case el nombre leído.

        Preferencia: proveedores CON CONTRATO en la obra validada (lista
        corta y de máxima relevancia). Fallback: maestro global de
        proveedores con contrato en la empresa (capado).
        """
        rows: list[tuple[str | None, str | None]] = []
        if obra_codigo_validado:
            fetch_por_obra = getattr(
                self._proveedor_client, "fetch_proveedores_por_obra", None
            )
            if fetch_por_obra is not None:
                try:
                    items = fetch_por_obra(codigo_obra=obra_codigo_validado)
                    for item in items or []:
                        if isinstance(item, tuple):
                            cif = item[0] if len(item) > 0 else None
                            nombre = item[1] if len(item) > 1 else None
                        else:
                            cif = getattr(item, "cif", None)
                            nombre = getattr(item, "nombre", None)
                        rows.append((cif, nombre))
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"proveedores_por_obra: {type(exc).__name__}"
                    )
                    logger.exception(
                        "%s fetch_proveedores_por_obra FALLO obra=%s",
                        _LOG_PREFIX, obra_codigo_validado,
                    )
        if not rows:
            try:
                rows = self._proveedor_client.search_proveedores()
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"proveedores_candidatos: {type(exc).__name__}"
                )
                logger.exception(
                    "%s search_proveedores FALLO", _LOG_PREFIX
                )
                return []

        out: list[dict[str, Any]] = []
        for cif, nombre in rows:
            cif_clean = (cif or "").strip()
            if not cif_clean:
                continue
            out.append({
                "cif": cif_clean,
                "nombre": (nombre or "").strip() or None,
            })
            if len(out) >= self._max_proveedores:
                break
        return out
