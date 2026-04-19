# domain/ports/obra_merge_repository_port.py
from __future__ import annotations

from typing import Protocol


class ObraMergeRepository(Protocol):
    """Puerto mínimo para el servicio de enriquecimiento.

    ``SqlAlchemyAlbaranRepository`` lo cumple por duck-typing al exponer
    estos dos métodos. No hace falta herencia explícita.
    """

    def get_merge_obra_codigo(self, *, document_id: str) -> str | None: ...

    def update_merge_obra_fields(
        self,
        *,
        document_id: str,
        obra_nombre: str | None,
        obra_direccion: str | None,
    ) -> None: ...
