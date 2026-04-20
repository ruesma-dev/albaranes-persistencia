# scripts/diagnose_sigrid_schema.py
"""
Consulta el esquema de la BBDD de Sigrid (SQL Server on-prem) a través
de la Function App de Azure y muestra en pantalla:

  1. Todas las tablas con número aproximado de filas.
  2. Todas las relaciones (foreign keys) entre tablas.

Uso:
    python scripts/diagnose_sigrid_schema.py

Variables de entorno necesarias (en .env o exportadas):
    SIGRID_API_BASE_URL      https://func-sigridapi-dev-huyke.azurewebsites.net
    SIGRID_API_FUNCTION_KEY  <tu clave>
    SIGRID_API_DATABASE      ruesma
"""
from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.environ.get("SIGRID_API_BASE_URL", "").rstrip("/")
FUNCTION_KEY = os.environ.get("SIGRID_API_FUNCTION_KEY", "")
DATABASE = os.environ.get("SIGRID_API_DATABASE", "ruesma")
TIMEOUT_S = 30


# ------------------------------------------------------------------ #
# Queries
# ------------------------------------------------------------------ #
SQL_TABLAS = """\
SELECT
    t.name          AS tabla,
    s.name          AS esquema,
    p.rows          AS filas_aprox
FROM sys.tables AS t
JOIN sys.schemas AS s ON t.schema_id = s.schema_id
JOIN sys.partitions AS p
    ON t.object_id = p.object_id
    AND p.index_id IN (0, 1)
ORDER BY s.name, t.name
"""

SQL_RELACIONES = """\
SELECT
    fk.name                                AS nombre_fk,
    tp.name                                AS tabla_padre,
    cp.name                                AS columna_padre,
    tr.name                                AS tabla_hija,
    cr.name                                AS columna_hija,
    fk.delete_referential_action_desc      AS on_delete,
    fk.update_referential_action_desc      AS on_update
FROM sys.foreign_keys AS fk
JOIN sys.foreign_key_columns AS fkc
    ON fk.object_id = fkc.constraint_object_id
JOIN sys.tables AS tp
    ON fkc.referenced_object_id = tp.object_id
JOIN sys.columns AS cp
    ON fkc.referenced_object_id = cp.object_id
    AND fkc.referenced_column_id = cp.column_id
JOIN sys.tables AS tr
    ON fkc.parent_object_id = tr.object_id
JOIN sys.columns AS cr
    ON fkc.parent_object_id = cr.object_id
    AND fkc.parent_column_id = cr.column_id
ORDER BY tabla_padre, tabla_hija, fk.name
"""


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
def _call(sql: str, max_rows: int = 500) -> tuple[list[str], list[list]]:
    """Llama a /api/sql/read y devuelve (columns, rows)."""
    if not BASE_URL:
        sys.exit("ERROR: SIGRID_API_BASE_URL no está definido.")
    if not FUNCTION_KEY:
        sys.exit("ERROR: SIGRID_API_FUNCTION_KEY no está definido.")

    url = f"{BASE_URL}/api/sql/read"
    payload = {
        "database": DATABASE,
        "sql": sql,
        "parameters": [],
        "timeout_seconds": TIMEOUT_S,
        "max_rows": max_rows,
    }
    headers = {
        "x-functions-key": FUNCTION_KEY,
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            resp = client.post(url, json=payload, headers=headers)
    except Exception as exc:
        sys.exit(f"ERROR de transporte: {exc}")

    if resp.status_code >= 400:
        sys.exit(f"ERROR HTTP {resp.status_code}: {resp.text[:500]}")

    body = resp.json()
    if not body.get("ok"):
        sys.exit(f"ERROR sigrid-api ok=false: {body}")

    return body.get("columns") or [], body.get("rows") or []


def _col_widths(columns: list[str], rows: list[list]) -> list[int]:
    widths = [len(c) for c in columns]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell) if cell is not None else "—"))
    return widths


def _print_table(columns: list[str], rows: list[list]) -> None:
    if not rows:
        print("  (sin resultados)")
        return
    widths = _col_widths(columns, rows)
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    header = "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns)) + " |"
    print(sep)
    print(header)
    print(sep)
    for row in rows:
        line = "| " + " | ".join(
            str(cell if cell is not None else "—").ljust(widths[i])
            for i, cell in enumerate(row)
        ) + " |"
        print(line)
    print(sep)
    print(f"  {len(rows)} fila(s)\n")


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #
def main() -> None:
    print("=" * 60)
    print(f"  Base de datos : {DATABASE}")
    print(f"  Endpoint      : {BASE_URL}")
    print("=" * 60)

    print("\n[1] TABLAS\n")
    cols, rows = _call(SQL_TABLAS, max_rows=500)
    _print_table(cols, rows)

    print("\n[2] RELACIONES (Foreign Keys)\n")
    cols, rows = _call(SQL_RELACIONES, max_rows=500)
    _print_table(cols, rows)


if __name__ == "__main__":
    main()
