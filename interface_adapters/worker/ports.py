# interface_adapters/worker/ports.py
"""Puertos del worker de persistencia (sv3 consume q-persistencia).

Necesita DOS colaboradores resueltos por document_id:
- :class:`FuenteEnvelope`  — el envelope de extraccion (lo dejo sv2).
- :class:`FuenteDocumento` — el PDF (sv3 lo persiste/sube a SharePoint).
En produccion ambos vendran de BBDD/SharePoint; en el piloto, de disco.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class DocumentoPdf:
    filename: str
    mime_type: str
    file_bytes: bytes


class FuenteEnvelope(ABC):
    @abstractmethod
    def obtener(self, document_id: str) -> Dict[str, Any]:
        raise NotImplementedError


class FuenteDocumento(ABC):
    @abstractmethod
    def obtener(self, document_id: str) -> DocumentoPdf:
        raise NotImplementedError
