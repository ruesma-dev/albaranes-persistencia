# scripts/diagnose_sigrid_relationships_v2.py
"""
Diagnóstico de relaciones entre tablas SIGRID usando Azure Function /api/sql/read.

Cambios v2:
- Respeta max_rows <= 1000, que es el límite real de tu Azure Function.
- Evita truncar columnas: carga metadatos de columnas tabla a tabla.
- Evita truncar sys.tables para el caso normal: consulta sólo las tablas necesarias.
- Corrige la validación por datos: no usa subconsultas dentro de SUM/COUNT.
- Todas las consultas empiezan por SELECT o WITH, compatible con la validación de la API.

Ejemplos:
    python scripts/diagnose_sigrid_relationships_v2.py

    python scripts/diagnose_sigrid_relationships_v2.py --tables con,auxemp,obr,ctr,prv,gra,rcg,PFfir,dog --validate-data

    python scripts/diagnose_sigrid_relationships_v2.py --tables con,auxemp,obr,ctr,prv,gra,rcg,PFfir,dog --no-validate-data

    python scripts/diagnose_sigrid_relationships_v2.py --database ruesma_rep --tables gra,rcg,PFfir,dog --validate-data
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:
    print("Falta httpx. Instala con: pip install httpx")
    sys.exit(2)


# =============================================================================
# .env
# =============================================================================

def load_dotenv_manually(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}

    result: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
ENV = load_dotenv_manually(ENV_PATH)


def get_cfg(name: str) -> str | None:
    return os.environ.get(name) or ENV.get(name)


# =============================================================================
# Utilidades
# =============================================================================

def qident(name: str) -> str:
    return "[" + str(name).replace("]", "]]" ) + "]"


def normalize_table_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def rows_to_dicts(body: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not body or not body.get("rows"):
        return []
    cols = body.get("columns") or []
    return [dict(zip(cols, row)) for row in body["rows"]]


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        path.write_text("", encoding="utf-8")
        return

    preferred_order = [
        "relation_kind", "confidence", "reason",
        "child_schema", "child_table", "child_column",
        "parent_schema", "parent_table", "parent_column",
        "child_rows", "child_nonzero_rows", "child_distinct_nonzero_values",
        "matched_rows", "unmatched_rows", "match_pct",
        "fk_name", "is_disabled", "is_not_trusted",
    ]

    keys: list[str] = []
    seen: set[str] = set()
    for key in preferred_order:
        if any(key in row for row in rows):
            keys.append(key)
            seen.add(key)
    for row in rows:
        for key in row.keys():
            if key not in seen:
                keys.append(key)
                seen.add(key)

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def print_relation_rows(title: str, rows: list[dict[str, Any]], limit: int = 80) -> None:
    print()
    print("═" * 100)
    print(f" {title}: {len(rows)} fila(s)")
    print("═" * 100)

    if not rows:
        print("  Sin resultados.")
        return

    for i, row in enumerate(rows[:limit], start=1):
        child = f"{row.get('child_schema', 'dbo')}.{row.get('child_table')}.{row.get('child_column')}"
        parent = f"{row.get('parent_schema', 'dbo')}.{row.get('parent_table')}.{row.get('parent_column')}"
        kind = row.get("relation_kind", "?")
        confidence = row.get("confidence", "?")
        reason = row.get("reason", "")
        extra = ""
        if "match_pct" in row:
            extra = (
                f" | match={row.get('match_pct')}%"
                f" | matched={row.get('matched_rows')}"
                f" | nonzero={row.get('child_nonzero_rows')}"
                f" | unmatched={row.get('unmatched_rows')}"
            )
        print(f"{i:>3}. [{kind}/{confidence}] {child} -> {parent} | {reason}{extra}")

    if len(rows) > limit:
        print(f"... {len(rows) - limit} relación(es) más no mostradas.")


# =============================================================================
# Cliente Azure Function
# =============================================================================

class SigridSqlClient:
    MAX_API_ROWS = 1000

    def __init__(self, base_url: str, function_key: str, database: str, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.function_key = function_key
        self.database = database
        self.timeout_s = timeout_s

    def run_query(
        self,
        label: str,
        sql: str,
        parameters: list[Any] | None = None,
        max_rows: int = 1000,
        verbose: bool = True,
    ) -> dict[str, Any] | None:
        url = f"{self.base_url}/api/sql/read"
        requested_max_rows = max_rows
        max_rows = min(int(max_rows), self.MAX_API_ROWS)

        payload = {
            "database": self.database,
            "sql": sql,
            "parameters": parameters or [],
            "timeout_seconds": int(self.timeout_s),
            "max_rows": max_rows,
        }
        headers = {"x-functions-key": self.function_key, "Content-Type": "application/json"}

        if verbose:
            print()
            print("─" * 100)
            print(f"  📡 {label}")
            print("─" * 100)
            print(f"  database={self.database}  max_rows={max_rows}")
            if requested_max_rows > self.MAX_API_ROWS:
                print(
                    f"  ⚠️  max_rows solicitado={requested_max_rows}; "
                    f"la API sólo permite {self.MAX_API_ROWS}. Se usa {max_rows}."
                )
            if parameters:
                print(f"  parameters={parameters}")

        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                response = client.post(url, json=payload, headers=headers)
        except Exception as exc:
            if verbose:
                print(f"  ❌ Error HTTP: {exc!r}")
            return None

        if verbose:
            print(f"  HTTP {response.status_code}")

        if response.status_code != 200:
            if verbose:
                print(f"  Body: {response.text[:1000]}")
            return None

        try:
            body = response.json()
        except json.JSONDecodeError:
            if verbose:
                print("  ❌ Respuesta no es JSON.")
                print(response.text[:1000])
            return None

        if not body.get("ok"):
            if verbose:
                print(f"  ❌ ok=false: {json.dumps(body, ensure_ascii=False)[:1000]}")
            return None

        if verbose:
            row_count = body.get("row_count", 0)
            truncated = body.get("truncated", False)
            print(f"  ✅ {row_count} fila(s){' (TRUNCADO)' if truncated else ''}")
            if truncated:
                print("  ⚠️  Resultado truncado.")

        return body


# =============================================================================
# SQL de metadatos
# =============================================================================

SQL_ALL_TABLES = """
SELECT
    s.name AS schema_name,
    t.name AS table_name
FROM sys.tables AS t
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
WHERE
    t.is_ms_shipped = 0
ORDER BY
    s.name,
    t.name
"""


def build_tables_by_name_query(table_names: list[str], schema_filter: str | None) -> tuple[str, list[Any]]:
    where_parts = ["t.is_ms_shipped = 0"]
    params: list[Any] = []

    if schema_filter:
        where_parts.append("s.name = ?")
        params.append(schema_filter)

    if table_names:
        placeholders = ", ".join(["?"] * len(table_names))
        where_parts.append(f"t.name IN ({placeholders})")
        params.extend(table_names)

    where_sql = "\n    AND ".join(where_parts)
    sql = f"""
SELECT
    s.name AS schema_name,
    t.name AS table_name
FROM sys.tables AS t
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
WHERE
    {where_sql}
ORDER BY
    s.name,
    t.name
"""
    return sql, params


def build_columns_for_one_table_query(schema_filter: str | None, table_name: str) -> tuple[str, list[Any]]:
    where_parts = ["t.is_ms_shipped = 0", "t.name = ?"]
    params: list[Any] = [table_name]

    if schema_filter:
        where_parts.insert(1, "s.name = ?")
        params.insert(0, schema_filter)

    where_sql = "\n    AND ".join(where_parts)
    sql = f"""
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    c.name AS column_name,
    ty.name AS data_type,
    c.max_length,
    c.precision,
    c.scale,
    c.is_nullable,
    c.column_id,
    CASE WHEN pk_ic.column_id IS NULL THEN 0 ELSE 1 END AS is_primary_key
FROM sys.tables AS t
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
JOIN sys.columns AS c
    ON c.object_id = t.object_id
JOIN sys.types AS ty
    ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.indexes AS pk_idx
    ON pk_idx.object_id = t.object_id
   AND pk_idx.is_primary_key = 1
LEFT JOIN sys.index_columns AS pk_ic
    ON pk_ic.object_id = t.object_id
   AND pk_ic.index_id = pk_idx.index_id
   AND pk_ic.column_id = c.column_id
WHERE
    {where_sql}
ORDER BY
    s.name,
    t.name,
    c.column_id
"""
    return sql, params


def build_fk_query(selected_tables: list[str] | None, schema_filter: str | None) -> tuple[str, list[Any]]:
    where_parts = ["1 = 1"]
    params: list[Any] = []

    if schema_filter:
        where_parts.append("SCHEMA_NAME(child_t.schema_id) = ?")
        params.append(schema_filter)

    if selected_tables:
        placeholders = ", ".join(["?"] * len(selected_tables))
        where_parts.append(f"(child_t.name IN ({placeholders}) OR parent_t.name IN ({placeholders}))")
        params.extend(selected_tables)
        params.extend(selected_tables)

    where_sql = "\n    AND ".join(where_parts)

    sql = f"""
SELECT
    fk.name AS fk_name,
    SCHEMA_NAME(child_t.schema_id) AS child_schema,
    child_t.name AS child_table,
    child_c.name AS child_column,
    SCHEMA_NAME(parent_t.schema_id) AS parent_schema,
    parent_t.name AS parent_table,
    parent_c.name AS parent_column,
    fk.is_disabled,
    fk.is_not_trusted,
    fkc.constraint_column_id
FROM sys.foreign_keys AS fk
JOIN sys.foreign_key_columns AS fkc
    ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables AS child_t
    ON child_t.object_id = fk.parent_object_id
JOIN sys.columns AS child_c
    ON child_c.object_id = child_t.object_id
   AND child_c.column_id = fkc.parent_column_id
JOIN sys.tables AS parent_t
    ON parent_t.object_id = fk.referenced_object_id
JOIN sys.columns AS parent_c
    ON parent_c.object_id = parent_t.object_id
   AND parent_c.column_id = fkc.referenced_column_id
WHERE
    {where_sql}
ORDER BY
    child_schema,
    child_table,
    fk.name,
    fkc.constraint_column_id
"""
    return sql, params


# =============================================================================
# Inferencia de relaciones
# =============================================================================

SPECIAL_COLUMN_TARGETS: dict[str, list[tuple[str, str, str]]] = {
    "con": [("con", "ide", "columna llamada 'con', usada como concepto")],
    "conide": [("con", "ide", "sufijo conide")],
    "docide": [("con", "ide", "docide suele apuntar a un concepto/documento")],
    "gra": [("gra", "ide", "columna llamada 'gra', usada como gráfico")],
    "graide": [("gra", "ide", "sufijo graide")],
    "dogide": [("dog", "ide", "sufijo dogide")],
    "ctride": [("ctr", "ide", "sufijo ctride")],
    "obride": [("obr", "ide", "sufijo obride")],
    "prvide": [("prv", "ide", "sufijo prvide")],
    "cliide": [("cli", "ide", "sufijo cliide")],
    "entide": [
        ("con", "ide", "entide suele apuntar a una entidad/concepto"),
        ("prv", "ide", "entide puede apuntar a proveedor en compras"),
        ("cli", "ide", "entide puede apuntar a cliente en ventas"),
    ],
    "empide": [("emp", "ide", "sufijo empide")],
    "ageide": [("age", "ide", "sufijo ageide")],
    "almide": [("alm", "ide", "sufijo almide")],
    "cenide": [("cen", "ide", "sufijo cenide")],
    "caaide": [("caa", "ide", "sufijo caaide")],
    "cuaide": [("cua", "ide", "sufijo cuaide")],
    "cueide": [("cua", "ide", "cueide suele apuntar a cuenta/cua")],
    "com": [("con", "ide", "com suele ser concepto común/padre")],
    "des": [("con", "ide", "des suele ser concepto destino/hijo")],
    "emp": [("auxemp", "numemp", "empresa lógica: emp -> auxemp.numemp")],
}


def make_table_lookup(tables: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for row in tables:
        schema = str(row["schema_name"])
        table = str(row["table_name"])
        key = table.lower()
        if key not in lookup or schema.lower() == "dbo":
            lookup[key] = {"schema_name": schema, "table_name": table}
    return lookup


def resolve_table(table_lookup: dict[str, dict[str, str]], table_name: str) -> dict[str, str] | None:
    return table_lookup.get(table_name.lower())


def add_candidate(
    candidates: list[dict[str, Any]],
    seen: set[tuple[str, str, str, str, str, str]],
    child_schema: str,
    child_table: str,
    child_column: str,
    parent_schema: str,
    parent_table: str,
    parent_column: str,
    reason: str,
    relation_kind: str,
    confidence: str,
) -> None:
    key = (
        child_schema.lower(), child_table.lower(), child_column.lower(),
        parent_schema.lower(), parent_table.lower(), parent_column.lower(),
    )
    if key in seen:
        return
    seen.add(key)
    candidates.append(
        {
            "relation_kind": relation_kind,
            "confidence": confidence,
            "reason": reason,
            "child_schema": child_schema,
            "child_table": child_table,
            "child_column": child_column,
            "parent_schema": parent_schema,
            "parent_table": parent_table,
            "parent_column": parent_column,
        }
    )


def collect_candidate_parent_names(columns: list[dict[str, Any]], selected_tables: list[str]) -> list[str]:
    names: set[str] = {t.lower() for t in selected_tables}

    for targets in SPECIAL_COLUMN_TARGETS.values():
        for parent_table, _, _ in targets:
            names.add(parent_table.lower())

    for col in columns:
        col_name = str(col["column_name"]).lower()
        if col_name.endswith("ide") and len(col_name) > 3:
            names.add(col_name[:-3])
        if col_name not in {"ide", "cod", "res", "fec", "tip", "emp"}:
            names.add(col_name)

    return sorted(names)


def infer_relationship_candidates(columns: list[dict[str, Any]], table_lookup: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    con_target = resolve_table(table_lookup, "con")

    for col in columns:
        child_schema = str(col["schema_name"])
        child_table = str(col["table_name"])
        child_column = str(col["column_name"])
        child_table_l = child_table.lower()
        child_column_l = child_column.lower()

        # Posible extensión de con: tabla.ide = con.ide
        if (
            child_column_l == "ide"
            and con_target is not None
            and child_table_l != "con"
            and not child_table_l.startswith("aux")
        ):
            add_candidate(
                candidates, seen,
                child_schema, child_table, child_column,
                con_target["schema_name"], con_target["table_name"], "ide",
                "posible tabla 'Propiedades de con': tabla.ide = con.ide",
                "inferred_extension_of_con",
                "media",
            )

        # Mapa especial de columnas conocidas.
        if child_column_l in SPECIAL_COLUMN_TARGETS:
            for parent_table_name, parent_column_name, reason in SPECIAL_COLUMN_TARGETS[child_column_l]:
                parent = resolve_table(table_lookup, parent_table_name)
                if parent is None:
                    continue
                add_candidate(
                    candidates, seen,
                    child_schema, child_table, child_column,
                    parent["schema_name"], parent["table_name"], parent_column_name,
                    reason,
                    "inferred_by_known_column",
                    "media",
                )

        # Convención general: xxxxide -> xxxx.ide
        if child_column_l.endswith("ide") and len(child_column_l) > 3:
            prefix = child_column_l[:-3]
            parent = resolve_table(table_lookup, prefix)
            if parent is not None:
                add_candidate(
                    candidates, seen,
                    child_schema, child_table, child_column,
                    parent["schema_name"], parent["table_name"], "ide",
                    f"convención: {child_column} -> {parent['table_name']}.ide",
                    "inferred_by_suffix_ide",
                    "media",
                )

        # Columna llamada igual que tabla: rcg.con -> con.ide, rcg.gra -> gra.ide
        parent_same_name = resolve_table(table_lookup, child_column_l)
        if parent_same_name is not None and child_column_l not in {"ide", "cod", "res", "fec", "tip", "emp"}:
            add_candidate(
                candidates, seen,
                child_schema, child_table, child_column,
                parent_same_name["schema_name"], parent_same_name["table_name"], "ide",
                f"columna llamada como tabla: {child_column} -> {parent_same_name['table_name']}.ide",
                "inferred_by_column_equals_table",
                "media",
            )

    return candidates


# =============================================================================
# Validación por datos
# =============================================================================

def build_validation_sql(candidate: dict[str, Any]) -> str:
    child_schema = qident(candidate["child_schema"])
    child_table = qident(candidate["child_table"])
    child_column = qident(candidate["child_column"])
    parent_schema = qident(candidate["parent_schema"])
    parent_table = qident(candidate["parent_table"])
    parent_column = qident(candidate["parent_column"])

    # Importante: no usar EXISTS dentro de SUM/COUNT, porque SQL Server lo rechaza
    # con "No es posible usar una función de agregado con una expresión que contiene un agregado o una subconsulta".
    return f"""
WITH child_values AS (
    SELECT
        TRY_CONVERT(bigint, c.{child_column}) AS child_value
    FROM {child_schema}.{child_table} AS c
),
parent_values AS (
    SELECT DISTINCT
        TRY_CONVERT(bigint, p.{parent_column}) AS parent_value
    FROM {parent_schema}.{parent_table} AS p
    WHERE TRY_CONVERT(bigint, p.{parent_column}) IS NOT NULL
),
joined_values AS (
    SELECT
        cv.child_value,
        pv.parent_value
    FROM child_values AS cv
    LEFT JOIN parent_values AS pv
        ON pv.parent_value = cv.child_value
)
SELECT
    COUNT_BIG(*) AS child_rows,

    SUM(
        CASE
            WHEN child_value IS NOT NULL AND child_value <> 0
            THEN 1 ELSE 0
        END
    ) AS child_nonzero_rows,

    COUNT(DISTINCT
        CASE
            WHEN child_value IS NOT NULL AND child_value <> 0
            THEN child_value
            ELSE NULL
        END
    ) AS child_distinct_nonzero_values,

    SUM(
        CASE
            WHEN child_value IS NOT NULL AND child_value <> 0 AND parent_value IS NOT NULL
            THEN 1 ELSE 0
        END
    ) AS matched_rows,

    SUM(
        CASE
            WHEN child_value IS NOT NULL AND child_value <> 0 AND parent_value IS NULL
            THEN 1 ELSE 0
        END
    ) AS unmatched_rows
FROM joined_values
"""


def validate_candidates(client: SigridSqlClient, candidates: list[dict[str, Any]], max_candidates: int) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    total = min(len(candidates), max_candidates)

    for idx, candidate in enumerate(candidates[:max_candidates], start=1):
        label = (
            f"VALIDAR {idx}/{total}: "
            f"{candidate['child_table']}.{candidate['child_column']} -> "
            f"{candidate['parent_table']}.{candidate['parent_column']}"
        )
        sql = build_validation_sql(candidate)
        body = client.run_query(label, sql, parameters=[], max_rows=1)
        rows = rows_to_dicts(body)

        enriched = dict(candidate)
        if rows:
            metrics = rows[0]
            child_nonzero = to_int(metrics.get("child_nonzero_rows"))
            matched = to_int(metrics.get("matched_rows"))
            unmatched = to_int(metrics.get("unmatched_rows"))
            match_pct = round((matched / child_nonzero) * 100, 2) if child_nonzero > 0 else None

            enriched.update(metrics)
            enriched["match_pct"] = match_pct

            if child_nonzero == 0:
                enriched["confidence"] = "sin_datos"
            elif match_pct == 100:
                enriched["confidence"] = "alta"
            elif match_pct is not None and match_pct >= 95:
                enriched["confidence"] = "alta_con_excepciones"
            elif match_pct is not None and match_pct >= 70:
                enriched["confidence"] = "media"
            elif match_pct is not None and match_pct > 0:
                enriched["confidence"] = "baja"
            else:
                enriched["confidence"] = "sin_match"
        else:
            enriched["validation_error"] = "sin respuesta o error"

        validated.append(enriched)

    validated.sort(
        key=lambda r: (
            -1 if r.get("match_pct") is None else -float(r.get("match_pct")),
            -to_int(r.get("child_nonzero_rows")),
            str(r.get("child_table")),
            str(r.get("child_column")),
        )
    )
    return validated


# =============================================================================
# Carga de metadatos robusta con límite 1000
# =============================================================================

def load_columns_for_tables(
    client: SigridSqlClient,
    selected_tables: list[str],
    schema_filter: str | None,
) -> list[dict[str, Any]]:
    all_columns: list[dict[str, Any]] = []

    for table_name in selected_tables:
        sql, params = build_columns_for_one_table_query(schema_filter, table_name)
        body = client.run_query(
            f"METADATOS: columnas de {table_name}",
            sql,
            parameters=params,
            max_rows=1000,
        )
        rows = rows_to_dicts(body)
        all_columns.extend(rows)

    return all_columns


def load_tables_by_names(
    client: SigridSqlClient,
    table_names: list[str],
    schema_filter: str | None,
) -> list[dict[str, Any]]:
    if not table_names:
        return []

    result: list[dict[str, Any]] = []
    # Mantener chunks pequeños por seguridad con parámetros.
    for part in chunked(sorted(set(table_names)), 200):
        sql, params = build_tables_by_name_query(part, schema_filter)
        body = client.run_query(
            f"METADATOS: resolver {len(part)} nombres de tabla",
            sql,
            parameters=params,
            max_rows=1000,
        )
        result.extend(rows_to_dicts(body))
    return result


# =============================================================================
# Main
# =============================================================================

DEFAULT_TABLES = (
    "con,auxemp,"
    "obr,ctr,prv,"
    "gra,rcg,dog,PFfir,"
    "dca,dcapro,dcf,dcfpro,dco,dcp,dvp,dvf,cer"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnostica relaciones entre tablas SIGRID usando /api/sql/read.")
    parser.add_argument("--database", default=get_cfg("SIGRID_API_DATABASE") or "ruesma")
    parser.add_argument("--schema", default="dbo", help="Schema a inspeccionar. Por defecto: dbo. Usa --schema '' para no filtrar.")
    parser.add_argument("--tables", default=DEFAULT_TABLES, help="Lista de tablas separadas por coma.")
    parser.add_argument("--all", action="store_true", help="Intenta inspeccionar todas las tablas. No recomendado con límite 1000.")
    parser.add_argument("--validate-data", dest="validate_data", action="store_true", default=None)
    parser.add_argument("--no-validate-data", dest="validate_data", action="store_false")
    parser.add_argument("--max-validate", type=int, default=250, help="Máximo de relaciones inferidas a validar.")
    parser.add_argument("--out-dir", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    base_url = get_cfg("SIGRID_API_BASE_URL")
    function_key = get_cfg("SIGRID_API_FUNCTION_KEY")
    timeout_s = float(get_cfg("SIGRID_API_TIMEOUT_S") or "30")
    schema_filter = args.schema.strip() if args.schema and args.schema.strip() else None

    if args.validate_data is None:
        args.validate_data = not args.all

    if not base_url:
        print("❌ Falta SIGRID_API_BASE_URL en .env o variables de entorno.")
        return 2
    if not function_key:
        print("❌ Falta SIGRID_API_FUNCTION_KEY en .env o variables de entorno.")
        return 2

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else PROJECT_ROOT / "outputs" / f"sigrid_relationships_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    selected_tables = [] if args.all else normalize_table_list(args.tables)

    print("=" * 100)
    print(" DIAGNÓSTICO DE RELACIONES ENTRE TABLAS SIGRID v2")
    print("=" * 100)
    print(f"Proyecto        : {PROJECT_ROOT}")
    print(f".env            : {ENV_PATH}  existe={ENV_PATH.exists()}")
    print(f"Base URL        : {base_url}")
    print(f"Function key    : <presente, len={len(function_key)}>")
    print(f"Database        : {args.database}")
    print(f"Schema          : {schema_filter or '<todos>'}")
    print(f"Tablas          : {'<todas>' if args.all else selected_tables}")
    print(f"Validar datos   : {args.validate_data}")
    print(f"Salida          : {out_dir}")
    print("=" * 100)

    client = SigridSqlClient(base_url, function_key, args.database, timeout_s)

    if args.all:
        # Con límite de 1000 puede truncar. Mantengo soporte, pero no es el modo recomendado.
        tables_body = client.run_query("METADATOS: tablas disponibles", SQL_ALL_TABLES, [], max_rows=1000)
        base_tables = rows_to_dicts(tables_body)
        selected_tables = [row["table_name"] for row in base_tables]
        if not selected_tables:
            print("❌ No se pudieron obtener tablas.")
            return 3
    else:
        # Resolver sólo las tablas pedidas inicialmente.
        base_tables = load_tables_by_names(client, selected_tables, schema_filter)
        found_initial = {str(row["table_name"]).lower() for row in base_tables}
        missing_initial = [t for t in selected_tables if t.lower() not in found_initial]
        if missing_initial:
            print()
            print("⚠️  Tablas indicadas que no existen o no son visibles en esta base/schema:")
            for t in missing_initial:
                print(f"   - {t}")

    # Columnas de las tablas de interés, una llamada por tabla para evitar truncamiento global.
    columns = load_columns_for_tables(client, selected_tables, schema_filter)
    if not columns:
        print("❌ No se pudieron obtener columnas para inferir relaciones.")
        return 4

    # Resolver tablas padre candidatas después de ver nombres de columnas.
    candidate_parent_names = collect_candidate_parent_names(columns, selected_tables)
    resolved_tables = load_tables_by_names(client, candidate_parent_names, schema_filter)
    table_lookup = make_table_lookup(resolved_tables)

    # Foreign keys físicas filtradas a las tablas seleccionadas.
    fk_sql, fk_params = build_fk_query(selected_tables, schema_filter)
    fk_body = client.run_query("METADATOS: foreign keys físicas filtradas", fk_sql, fk_params, max_rows=1000)
    fks_raw = rows_to_dicts(fk_body)
    fks = [{"relation_kind": "physical_foreign_key", "confidence": "formal", "reason": "foreign key física en SQL Server", **row} for row in fks_raw]

    candidates = infer_relationship_candidates(columns, table_lookup)
    candidates.sort(key=lambda r: (str(r["child_schema"]).lower(), str(r["child_table"]).lower(), str(r["child_column"]).lower(), str(r["parent_table"]).lower()))

    if args.validate_data:
        validated = validate_candidates(client, candidates, args.max_validate)
    else:
        validated = []

    write_csv(out_dir / "01_physical_foreign_keys.csv", fks)
    write_csv(out_dir / "02_inferred_relationship_candidates.csv", candidates)
    write_csv(out_dir / "03_validated_relationships.csv", validated)

    summary = {
        "database": args.database,
        "schema": schema_filter,
        "tables": "all" if args.all else selected_tables,
        "physical_foreign_keys_count": len(fks),
        "inferred_candidates_count": len(candidates),
        "validated_relationships_count": len(validated),
        "validate_data": args.validate_data,
        "output_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print_relation_rows("FOREIGN KEYS FÍSICAS", fks, limit=80)
    print_relation_rows("RELACIONES INFERIDAS", candidates, limit=120)

    if validated:
        print_relation_rows("RELACIONES VALIDADAS POR DATOS", validated, limit=120)
        high = [r for r in validated if r.get("confidence") in {"alta", "alta_con_excepciones"}]
        print_relation_rows("RELACIONES CON MATCH ALTO", high, limit=120)

    print()
    print("=" * 100)
    print(" RESUMEN")
    print("=" * 100)
    print(f"Foreign keys físicas       : {len(fks)}")
    print(f"Relaciones inferidas       : {len(candidates)}")
    print(f"Relaciones validadas       : {len(validated)}")
    print(f"Directorio de salida       : {out_dir}")
    print("Ficheros generados:")
    print(f"  {out_dir / '01_physical_foreign_keys.csv'}")
    print(f"  {out_dir / '02_inferred_relationship_candidates.csv'}")
    print(f"  {out_dir / '03_validated_relationships.csv'}")
    print(f"  {out_dir / 'summary.json'}")
    print("=" * 100)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
