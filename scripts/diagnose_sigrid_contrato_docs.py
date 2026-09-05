# scripts/diagnose_sigrid_contrato_docs.py
"""Diagnóstico bidireccional de documentos de contrato.

Ataque desde dos direcciones para confirmar qué vínculos son reales:

  DIRECCIÓN A — De CIF+obra hacia documentos:
    CIF + obra → ctr.ide → [5 vías] → gra.ide

  DIRECCIÓN B — Del nombre de documento hacia contrato:
    nombre PDF → gra.ide → [5 vías inversas] → concepto → contrato

  Luego compara ambos conjuntos para ver coincidencias y huérfanos.

Las 5 vías oficiales hacia gra (según la tabla de referencias):
  1. rcg.gra          (gráficos en conceptos)
  2. PFfir.graide     (firma de documentos)
  3. acugra.graide    (gráficos de acuerdos)
  4. k_acd.graide     (contrata: actos administrativos)
  5. gra.graant       (versión anterior — autoreferencia)

Modos:

  python scripts/diagnose_sigrid_contrato_docs.py B86359866 0695
      → DIRECCIÓN A: desde CIF+obra

  python scripts/diagnose_sigrid_contrato_docs.py --find SUMINISTROS_DE_OBRAS_MOSTOLES
      → DIRECCIÓN B: desde nombre

  python scripts/diagnose_sigrid_contrato_docs.py B86359866 0695 --find SUMINISTROS
      → AMBAS + comparación

  python scripts/diagnose_sigrid_contrato_docs.py --download-ide 274282
      → descarga directa

CORRECCIÓN 2026-09-05 — cruce con la base documental:
  Las cuatro vías cruzaban a `ruesma_rep.dbo.gra` por `ide`
  (`ON rcg.gra = g.ide`). Está medido que eso devuelve documentos AJENOS:
  los `ide` de `ruesma.gra` y `ruesma_rep.gra` solo coinciden en 426 filas
  de 2009 y desde entonces divergen. La relación real es por **(emp, cod)**,
  con índice único `gra_empcod` en las dos bases.
  Ahora cada vía va `<tabla>.graide → ruesma.gra (g_neg) → ruesma_rep.gra
  (g_rep) ON g_rep.cod = g_neg.cod AND g_rep.emp = g_neg.emp`.
  `gra_neg_ide` es el gráfico de negocio; `gra_rep_ide` es el DOCUMENTAL,
  y es el único válido para `documents/read` / `--download-ide`, así que es
  el que viaja en `_gra_ide` (0 = el gráfico de negocio no tiene pareja
  documental; sin pareja no hay binario que descargar).
  La DIRECCIÓN B tiene el mismo problema al revés: la búsqueda por nombre
  corre sobre `ruesma_rep` y devuelve `ide` DOCUMENTALES, pero `rcg.gra` y
  los `graide` son de NEGOCIO. Se traducen por `(emp, cod)` con
  `traducir_documentales_a_negocio()` antes de buscar los vínculos, y las
  documentales sin pareja en negocio se listan como «sin fila de negocio»
  en vez de desaparecer. Así las dos direcciones comparan el mismo tipo de
  ide (el documental, que es el que se descarga).
  Ver `sigrid-api/progress/explore_F-004_relacion_gra.md` (§C, §G, §H).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def load_dotenv_manually(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

print("=" * 70)
print(" DIAGNÓSTICO SIGRID — BIDIRECCIONAL (5 vías oficiales a gra)")
print("=" * 70)
env = load_dotenv_manually(_ENV_PATH)


def get_cfg(name: str) -> str | None:
    return os.environ.get(name) or env.get(name)


base_url = get_cfg("SIGRID_API_BASE_URL")
function_key = get_cfg("SIGRID_API_FUNCTION_KEY")
database = get_cfg("SIGRID_API_DATABASE") or "ruesma"
database_rep = get_cfg("SIGRID_API_DATABASE_REP") or "ruesma_rep"
timeout_s = float(get_cfg("SIGRID_API_TIMEOUT_S") or "30")

print(f"  database     = {database!r}")
print(f"  database_rep = {database_rep!r}")

if not base_url or not function_key:
    print("❌ Faltan SIGRID_API_BASE_URL o SIGRID_API_FUNCTION_KEY en .env")
    sys.exit(2)
print("-" * 70)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_obra_code(raw: str | None) -> str | None:
    if raw is None:
        return None
    c = str(raw).strip()
    if not c or not c.isdigit():
        return None
    if len(c) == 3:
        return "0" + c
    if len(c) == 4 and c.startswith("0"):
        return c
    return None


def normalize_cif(raw: str | None) -> str | None:
    if raw is None:
        return None
    c = str(raw).strip().upper().replace(" ", "")
    return c or None


def sanitize_filename(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', '_', name)
    return s.strip().rstrip('.') or "documento"


try:
    import httpx
except ImportError:
    print("❌ pip install httpx")
    sys.exit(4)


# Parsear args
positional_args: list[str] = []
flag_download = False
find_query: str | None = None
download_match: str | None = None
download_ide: int | None = None

args_iter = iter(sys.argv[1:])
for arg in args_iter:
    if arg == "--download":
        flag_download = True
    elif arg == "--find":
        find_query = next(args_iter, None)
    elif arg == "--download-match":
        download_match = next(args_iter, None)
        flag_download = True
    elif arg == "--download-ide":
        try:
            download_ide = int(next(args_iter))
        except (StopIteration, ValueError):
            print("❌ --download-ide requiere un entero")
            sys.exit(3)
    else:
        positional_args.append(arg)

cif_input = positional_args[0] if len(positional_args) > 0 else None
obra_input = positional_args[1] if len(positional_args) > 1 else None

cif = normalize_cif(cif_input) if cif_input else None
obra = normalize_obra_code(obra_input) if obra_input else None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def run_query(
    label: str, sql: str, parameters: list,
    max_rows: int = 500, target_database: str | None = None,
    verbose: bool = True,
) -> dict | None:
    db = target_database or database
    url = f"{base_url.rstrip('/')}/api/sql/read"
    payload = {
        "database": db, "sql": sql, "parameters": parameters,
        "timeout_seconds": int(timeout_s), "max_rows": max_rows,
    }
    headers = {"x-functions-key": function_key, "Content-Type": "application/json"}
    if verbose:
        print(f"\n  📡 {label}  [db={db}]")
    try:
        with httpx.Client(timeout=timeout_s) as c:
            r = c.post(url, json=payload, headers=headers)
    except Exception as exc:
        print(f"    ❌ {exc!r}")
        return None
    if r.status_code != 200:
        print(f"    ❌ HTTP {r.status_code}: {r.text[:200]}")
        return None
    try:
        body = r.json()
    except json.JSONDecodeError:
        print("    ❌ JSON inválido")
        return None
    if not body.get("ok"):
        print(f"    ❌ ok=false: {json.dumps(body, ensure_ascii=False)[:200]}")
        return None
    if verbose:
        rc = body.get("row_count", 0)
        print(f"    ✅ {rc} fila(s)")
    return body


def rows_to_dicts(body: dict | None) -> list[dict]:
    if not body or not body.get("rows"):
        return []
    cols = body["columns"]
    return [dict(zip(cols, row)) for row in body["rows"]]


def download_gra(gra_ide: int, out_dir: Path, fallback_name: str) -> bool:
    url = f"{base_url.rstrip('/')}/api/documents/read"
    payload = {
        "database": database_rep, "schema": "dbo", "table": "gra",
        "id_column": "ide", "id_value": gra_ide,
        "blob_column": "ima",
        "filename_columns": ["nomori", "nom"],
        "disposition": "attachment",
    }
    headers = {"x-functions-key": function_key, "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=120) as c:
            r = c.post(url, json=payload, headers=headers)
    except Exception as exc:
        print(f"    ❌ {exc!r}")
        return False
    if r.status_code != 200:
        print(f"    ❌ HTTP {r.status_code}: {r.text[:200]}")
        return False
    binary = r.content
    if not binary:
        print(f"    ❌ Respuesta vacía")
        return False
    fname = sanitize_filename(r.headers.get("X-Document-Filename", fallback_name))
    out_path = out_dir / fname
    if out_path.exists():
        out_path = out_dir / f"{out_path.stem}_{gra_ide}{out_path.suffix}"
    out_path.write_bytes(binary)
    print(f"    ✅ {out_path.name}  ({len(binary):,} bytes)")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# MODO --download-ide
# ═══════════════════════════════════════════════════════════════════════════
if download_ide is not None:
    download_dir = Path.home() / "Downloads" / "sigrid_docs" / f"gra_{download_ide}"
    download_dir.mkdir(parents=True, exist_ok=True)
    body = run_query(
        "Obtener nombre",
        "SELECT nom, nomori FROM gra WHERE ide = ?",
        [download_ide], max_rows=1, target_database=database_rep,
    )
    rows = rows_to_dicts(body)
    if not rows:
        print(f"\n❌ No existe gra.ide={download_ide}")
        sys.exit(1)
    fallback = rows[0].get("nomori") or rows[0].get("nom") or f"gra_{download_ide}"
    print(f"\n⬇️  Descargando gra.ide={download_ide} → {download_dir}")
    download_gra(download_ide, download_dir, fallback)
    sys.exit(0)


# ═══════════════════════════════════════════════════════════════════════════
# FUNCIONES DE EXPLORACIÓN (se reutilizan en ambas direcciones)
# ═══════════════════════════════════════════════════════════════════════════

def buscar_docs_desde_concepto(con_ides: list[int]) -> list[dict]:
    """Dado un conjunto de con.ide, busca documentos por las 5 vías.

    Devuelve lista de dicts con: _via, _gra_ide, _gra_neg_ide, _gra_nom,
    _gra_cod, _gra_fec, _ima_bytes, _concepto_ide, _extra_info

    `_gra_ide` es el ide DOCUMENTAL (`ruesma_rep.gra.ide`), el que acepta
    `documents/read`; `_gra_neg_ide` es el de negocio (`ruesma.gra.ide`),
    al que apuntan `rcg.gra` y los `graide`. Ver CORRECCIÓN 2026-09-05.
    """
    if not con_ides:
        return []
    ides_csv = ",".join(str(i) for i in con_ides)
    docs: list[dict] = []

    # VÍA 1: rcg.gra
    body = run_query(
        f"VÍA 1: rcg (→ {len(con_ides)} concepto(s))",
        f"""
        SELECT rcg.con AS con_ide,
               g_neg.ide AS gra_neg_ide, g_rep.ide AS gra_rep_ide,
               rcg.pos AS rcg_pos, rcg.cla AS rcg_clase,
               g_neg.nom AS gra_nom, g_neg.nomori AS gra_nomori,
               g_rep.nom AS gra_rep_nom, g_rep.nomori AS gra_rep_nomori,
               g_neg.cod AS gra_cod, g_neg.emp AS gra_emp,
               g_neg.fec AS gra_fec, g_neg.usu AS gra_usu,
               DATALENGTH(g_rep.ima) AS ima_bytes
        FROM rcg
        JOIN gra AS g_neg ON rcg.gra = g_neg.ide
        LEFT JOIN {database_rep}.dbo.gra AS g_rep
               ON g_rep.cod = g_neg.cod AND g_rep.emp = g_neg.emp
        WHERE rcg.con IN ({ides_csv})
        """, [], max_rows=500,
    )
    for r in rows_to_dicts(body):
        docs.append({
            "_via": "rcg",
            "_gra_ide": r.get("gra_rep_ide") or 0,
            "_gra_neg_ide": r.get("gra_neg_ide"),
            "_gra_nom": (r.get("gra_nomori") or r.get("gra_nom")
                         or r.get("gra_rep_nomori") or r.get("gra_rep_nom")
                         or "?"),
            "_gra_cod": r.get("gra_cod"),
            "_gra_fec": r.get("gra_fec"),
            "_ima_bytes": r.get("ima_bytes") or 0,
            "_concepto_ide": r["con_ide"],
            "_extra_info": f"rcg.cla={r.get('rcg_clase')}",
        })

    # VÍA 2: PFfir.graide
    body = run_query(
        f"VÍA 2: PFfir (firmas)",
        f"""
        SELECT PFfir.conide AS con_ide,
               g_neg.ide AS gra_neg_ide, g_rep.ide AS gra_rep_ide,
               PFfir.tipfir, PFfir.estfir,
               g_neg.nom AS gra_nom, g_neg.nomori AS gra_nomori,
               g_rep.nom AS gra_rep_nom, g_rep.nomori AS gra_rep_nomori,
               g_neg.cod AS gra_cod, g_neg.emp AS gra_emp,
               g_neg.fec AS gra_fec,
               DATALENGTH(g_rep.ima) AS ima_bytes
        FROM PFfir
        LEFT JOIN gra AS g_neg ON PFfir.graide = g_neg.ide
        LEFT JOIN {database_rep}.dbo.gra AS g_rep
               ON g_rep.cod = g_neg.cod AND g_rep.emp = g_neg.emp
        WHERE PFfir.conide IN ({ides_csv})
          AND PFfir.graide IS NOT NULL AND PFfir.graide <> 0
        """, [], max_rows=500,
    )
    for r in rows_to_dicts(body):
        docs.append({
            "_via": "PFfir",
            "_gra_ide": r.get("gra_rep_ide") or 0,
            "_gra_neg_ide": r.get("gra_neg_ide"),
            "_gra_nom": (r.get("gra_nomori") or r.get("gra_nom")
                         or r.get("gra_rep_nomori") or r.get("gra_rep_nom")
                         or "?"),
            "_gra_cod": r.get("gra_cod"),
            "_gra_fec": r.get("gra_fec"),
            "_ima_bytes": r.get("ima_bytes") or 0,
            "_concepto_ide": r["con_ide"],
            "_extra_info": f"tipfir={r.get('tipfir')} estfir={r.get('estfir')}",
        })

    # VÍA 3: acugra (acuerdos → gráficos)
    # acugra no se une directamente al concepto — va vía acu.
    # acu también hereda de con probablemente, pero para que sea útil
    # cruzamos sobre el ide (si acu es propiedades de con)
    body = run_query(
        f"VÍA 3: acugra (acuerdos)",
        f"""
        SELECT acugra.acuide AS con_ide,
               g_neg.ide AS gra_neg_ide, g_rep.ide AS gra_rep_ide,
               g_neg.nom AS gra_nom, g_neg.nomori AS gra_nomori,
               g_rep.nom AS gra_rep_nom, g_rep.nomori AS gra_rep_nomori,
               g_neg.cod AS gra_cod, g_neg.emp AS gra_emp,
               g_neg.fec AS gra_fec,
               DATALENGTH(g_rep.ima) AS ima_bytes
        FROM acugra
        LEFT JOIN gra AS g_neg ON acugra.graide = g_neg.ide
        LEFT JOIN {database_rep}.dbo.gra AS g_rep
               ON g_rep.cod = g_neg.cod AND g_rep.emp = g_neg.emp
        WHERE acugra.acuide IN ({ides_csv})
        """, [], max_rows=500,
    )
    for r in rows_to_dicts(body):
        docs.append({
            "_via": "acugra",
            "_gra_ide": r.get("gra_rep_ide") or 0,
            "_gra_neg_ide": r.get("gra_neg_ide"),
            "_gra_nom": (r.get("gra_nomori") or r.get("gra_nom")
                         or r.get("gra_rep_nomori") or r.get("gra_rep_nom")
                         or "?"),
            "_gra_cod": r.get("gra_cod"),
            "_gra_fec": r.get("gra_fec"),
            "_ima_bytes": r.get("ima_bytes") or 0,
            "_concepto_ide": r["con_ide"],
            "_extra_info": "",
        })

    # VÍA 4: k_acd (contrata, actos administrativos)
    # Este es más indirecto — k_acd.aceide → k_ace.xpeide → expediente.
    # De momento probamos por si acaso algún contrato está modelado como expediente.
    body = run_query(
        f"VÍA 4: k_acd (contrata/actos)",
        f"""
        SELECT k_acd.aceide AS ace_ide,
               g_neg.ide AS gra_neg_ide, g_rep.ide AS gra_rep_ide,
               g_neg.nom AS gra_nom, g_neg.nomori AS gra_nomori,
               g_rep.nom AS gra_rep_nom, g_rep.nomori AS gra_rep_nomori,
               g_neg.cod AS gra_cod, g_neg.emp AS gra_emp,
               g_neg.fec AS gra_fec,
               DATALENGTH(g_rep.ima) AS ima_bytes
        FROM k_acd
        LEFT JOIN gra AS g_neg ON k_acd.graide = g_neg.ide
        LEFT JOIN {database_rep}.dbo.gra AS g_rep
               ON g_rep.cod = g_neg.cod AND g_rep.emp = g_neg.emp
        WHERE k_acd.aceide IN ({ides_csv})
        """, [], max_rows=500,
    )
    for r in rows_to_dicts(body):
        docs.append({
            "_via": "k_acd",
            "_gra_ide": r.get("gra_rep_ide") or 0,
            "_gra_neg_ide": r.get("gra_neg_ide"),
            "_gra_nom": (r.get("gra_nomori") or r.get("gra_nom")
                         or r.get("gra_rep_nomori") or r.get("gra_rep_nom")
                         or "?"),
            "_gra_cod": r.get("gra_cod"),
            "_gra_fec": r.get("gra_fec"),
            "_ima_bytes": r.get("ima_bytes") or 0,
            "_concepto_ide": r.get("ace_ide"),
            "_extra_info": "",
        })

    return docs


def traducir_documentales_a_negocio(gra_rep_rows: list[dict]) -> dict[int, dict]:
    """Traduce filas de `ruesma_rep.gra` a su fila de `ruesma.gra`.

    La correspondencia es por `(emp, cod)`, nunca por `ide` (ver la
    CORRECCIÓN 2026-09-05 de la cabecera). Devuelve
    `{gra_rep_ide: {"neg_ide": int|None, "emp": ..., "cod": ...}}`.
    `neg_ide` es None cuando la documental no tiene pareja en negocio: hay
    76.187 así y muchas son legítimas (otros módulos, altas borradas en
    negocio), por eso se conservan en la salida en vez de descartarlas.
    """
    vacio = {r["ide"]: {"neg_ide": None, "emp": r.get("emp"), "cod": r.get("cod")}
             for r in gra_rep_rows}
    cods = sorted({r.get("cod") for r in gra_rep_rows if r.get("cod")})
    if not cods:
        return vacio
    marcadores = ",".join("?" for _ in cods)
    body = run_query(
        f"B2: traducir {len(cods)} cod documental(es) a ruesma.gra por (emp, cod)",
        f"""
        SELECT ide AS gra_neg_ide, emp AS gra_emp, cod AS gra_cod
        FROM gra
        WHERE cod IN ({marcadores})
        """, list(cods), max_rows=500,
    )
    por_clave = {(r.get("gra_emp"), r.get("gra_cod")): r["gra_neg_ide"]
                 for r in rows_to_dicts(body)}
    for rep_ide, datos in vacio.items():
        datos["neg_ide"] = por_clave.get((datos["emp"], datos["cod"]))
    return vacio


def buscar_conceptos_desde_gra(gra_neg_ides: list[int],
                               neg_a_rep: dict[int, int]) -> list[dict]:
    """Dado un conjunto de gra.ide DE NEGOCIO (`ruesma.gra`), busca los
    conceptos que los referencian por las 4 vías (excluyendo gra.graant,
    que es intra-gra).

    `neg_a_rep` mapea cada ide de negocio a su ide documental, para que la
    salida se pueda comparar con la DIRECCIÓN A y usar en la descarga.

    Devuelve lista con: _gra_ide (documental), _gra_neg_ide, _via,
    _concepto_ide, + metadatos
    """
    if not gra_neg_ides:
        return []
    ides_csv = ",".join(str(i) for i in gra_neg_ides)
    links: list[dict] = []

    # Vía rcg
    body = run_query(
        f"Inverso VÍA rcg",
        f"""
        SELECT rcg.gra AS gra_neg_ide, rcg.con AS con_ide,
               rcg.pos, rcg.cla
        FROM rcg
        WHERE rcg.gra IN ({ides_csv})
        """, [], max_rows=500,
    )
    for r in rows_to_dicts(body):
        links.append({"_gra_ide": neg_a_rep.get(r["gra_neg_ide"]),
                      "_gra_neg_ide": r["gra_neg_ide"], "_via": "rcg",
                      "_concepto_ide": r["con_ide"],
                      "_extra": f"pos={r.get('pos')} cla={r.get('cla')}"})

    # Vía PFfir
    body = run_query(
        f"Inverso VÍA PFfir",
        f"""
        SELECT PFfir.graide AS gra_neg_ide, PFfir.conide AS con_ide,
               PFfir.tipfir, PFfir.estfir
        FROM PFfir
        WHERE PFfir.graide IN ({ides_csv})
        """, [], max_rows=500,
    )
    for r in rows_to_dicts(body):
        links.append({"_gra_ide": neg_a_rep.get(r["gra_neg_ide"]),
                      "_gra_neg_ide": r["gra_neg_ide"], "_via": "PFfir",
                      "_concepto_ide": r["con_ide"],
                      "_extra": f"tipfir={r.get('tipfir')} estfir={r.get('estfir')}"})

    # Vía acugra
    body = run_query(
        f"Inverso VÍA acugra",
        f"""
        SELECT acugra.graide AS gra_neg_ide, acugra.acuide AS con_ide
        FROM acugra
        WHERE acugra.graide IN ({ides_csv})
        """, [], max_rows=500,
    )
    for r in rows_to_dicts(body):
        links.append({"_gra_ide": neg_a_rep.get(r["gra_neg_ide"]),
                      "_gra_neg_ide": r["gra_neg_ide"], "_via": "acugra",
                      "_concepto_ide": r["con_ide"], "_extra": ""})

    # Vía k_acd
    body = run_query(
        f"Inverso VÍA k_acd",
        f"""
        SELECT k_acd.graide AS gra_neg_ide, k_acd.aceide AS con_ide
        FROM k_acd
        WHERE k_acd.graide IN ({ides_csv})
        """, [], max_rows=500,
    )
    for r in rows_to_dicts(body):
        links.append({"_gra_ide": neg_a_rep.get(r["gra_neg_ide"]),
                      "_gra_neg_ide": r["gra_neg_ide"], "_via": "k_acd",
                      "_concepto_ide": r["con_ide"], "_extra": ""})

    return links


def resolve_concepto_context(con_ide: int) -> dict:
    """Resuelve contexto (contrato/obra/proveedor) para un con.ide."""
    ctx = {
        "con_cod": None, "con_res": None, "con_tip": None,
        "tipo_entidad": None,
        "contrato_cod": None, "obra_cod": None, "obra_res": None,
        "proveedor_cif": None, "proveedor_nombre": None,
    }

    # Consulta combinada: con + ctr + dcf + obr + prv con LEFT JOINs
    body = run_query(
        f"  Resolver con.ide={con_ide}",
        """
        SELECT
            c.ide, c.cod AS con_cod, c.res AS con_res, c.tip AS con_tip,
            ctr.ide AS ctr_ide, ctr.obride AS ctr_obride,
            ctr.entcif AS ctr_entcif, ctr.entres AS ctr_entres,
            obr_ctr.cod AS ctr_obra_cod, obr_ctr.res AS ctr_obra_res,
            dcf.ide AS dcf_ide, dcf.ctride AS dcf_ctride,
            dcf.entcif AS dcf_entcif, dcf.entres AS dcf_entres,
            con_dcfctr.cod AS dcf_ctr_cod,
            obr_dcf.cod AS dcf_obra_cod, obr_dcf.res AS dcf_obra_res,
            obr.ide AS obr_ide,
            prv.ide AS prv_ide, prv.cif AS prv_cif, prv.raz AS prv_raz
        FROM con c
        LEFT JOIN ctr ON c.ide = ctr.ide
        LEFT JOIN con AS obr_ctr ON ctr.obride = obr_ctr.ide
        LEFT JOIN dcf ON c.ide = dcf.ide
        LEFT JOIN ctr AS ctr2 ON dcf.ctride = ctr2.ide
        LEFT JOIN con AS con_dcfctr ON ctr2.ide = con_dcfctr.ide
        LEFT JOIN con AS obr_dcf ON ctr2.obride = obr_dcf.ide
        LEFT JOIN obr ON c.ide = obr.ide
        LEFT JOIN prv ON c.ide = prv.ide
        WHERE c.ide = ?
        """, [con_ide], max_rows=1, verbose=False,
    )
    rows = rows_to_dicts(body)
    if not rows:
        return ctx
    r = rows[0]
    ctx["con_cod"] = r.get("con_cod")
    ctx["con_res"] = r.get("con_res")
    ctx["con_tip"] = r.get("con_tip")

    if r.get("ctr_ide"):
        ctx["tipo_entidad"] = "CONTRATO"
        ctx["contrato_cod"] = r.get("con_cod")
        ctx["obra_cod"] = r.get("ctr_obra_cod")
        ctx["obra_res"] = r.get("ctr_obra_res")
        ctx["proveedor_cif"] = r.get("ctr_entcif")
        ctx["proveedor_nombre"] = r.get("ctr_entres")
    elif r.get("dcf_ide"):
        ctx["tipo_entidad"] = "FACTURA-COMPRA"
        ctx["contrato_cod"] = r.get("dcf_ctr_cod")
        ctx["obra_cod"] = r.get("dcf_obra_cod")
        ctx["obra_res"] = r.get("dcf_obra_res")
        ctx["proveedor_cif"] = r.get("dcf_entcif")
        ctx["proveedor_nombre"] = r.get("dcf_entres")
    elif r.get("obr_ide"):
        ctx["tipo_entidad"] = "OBRA"
        ctx["obra_cod"] = r.get("con_cod")
        ctx["obra_res"] = r.get("con_res")
    elif r.get("prv_ide"):
        ctx["tipo_entidad"] = "PROVEEDOR"
        ctx["proveedor_cif"] = r.get("prv_cif")
        ctx["proveedor_nombre"] = r.get("prv_raz")
    else:
        ctx["tipo_entidad"] = f"con.tip={r.get('con_tip')}"
    return ctx


# ═══════════════════════════════════════════════════════════════════════════
# DIRECCIÓN A: CIF + OBRA → DOCUMENTOS
# ═══════════════════════════════════════════════════════════════════════════
docs_A: list[dict] = []      # Documentos hallados desde CIF+obra
gra_ides_A: set[int] = set()
contrato_ide_A: int | None = None
codigo_contrato_A: str | None = None

if cif and obra:
    print(f"\n{'━' * 70}")
    print(f"  DIRECCIÓN A: CIF={cif!r}  obra={obra!r} → documentos")
    print(f"{'━' * 70}")

    body = run_query(
        "A1: Localizar contrato",
        """
        SELECT ctr.ide AS ctr_ide, con_ctr.cod AS ctr_cod,
               con_ctr.res AS ctr_res
        FROM ctr
        JOIN con AS con_ctr ON ctr.ide = con_ctr.ide
        JOIN con AS con_obr ON ctr.obride = con_obr.ide
        JOIN prv ON ctr.entide = prv.ide
        WHERE prv.cif = ? AND con_obr.cod = ?
        """, [cif, obra], max_rows=20,
    )
    contratos_rows = rows_to_dicts(body)

    if not contratos_rows:
        print(f"\n  ⚠️  No hay contrato para CIF={cif} obra={obra}")
    else:
        for c in contratos_rows:
            print(f"  contrato_ide={c['ctr_ide']}  cod={c['ctr_cod']!r}")
        contrato_ide_A = contratos_rows[0]["ctr_ide"]
        codigo_contrato_A = contratos_rows[0]["ctr_cod"]

        con_ides = [c["ctr_ide"] for c in contratos_rows]
        docs_A = buscar_docs_desde_concepto(con_ides)
        gra_ides_A = {d["_gra_ide"] for d in docs_A}

        print(f"\n  📎 DIRECCIÓN A encontró {len(docs_A)} vínculos "
              f"→ {len(gra_ides_A)} gra únicos")
        # Agrupar por vía
        from collections import Counter
        via_count = Counter(d["_via"] for d in docs_A)
        for via, count in via_count.items():
            print(f"    {via:<10} {count}")


# ═══════════════════════════════════════════════════════════════════════════
# DIRECCIÓN B: NOMBRE DE DOCUMENTO → CONTRATO
# ═══════════════════════════════════════════════════════════════════════════
docs_B: list[dict] = []      # {_gra_ide, _gra_nom, ...}
gra_ides_B: set[int] = set()

if find_query:
    print(f"\n{'━' * 70}")
    print(f"  DIRECCIÓN B: nombre={find_query!r} → conceptos")
    print(f"{'━' * 70}")

    body = run_query(
        "B1: Buscar en ruesma_rep.gra",
        """
        SELECT TOP 50
            ide, emp, cod, nom, nomori, fec, usu,
            DATALENGTH(ima) AS ima_bytes
        FROM gra
        WHERE nom LIKE ? OR nomori LIKE ? OR cod LIKE ?
        ORDER BY fec DESC
        """,
        [f"%{find_query}%", f"%{find_query}%", f"%{find_query}%"],
        max_rows=50, target_database=database_rep,
    )
    gra_found = rows_to_dicts(body)

    if not gra_found:
        print(f"\n  ⚠️  Ningún gra encontrado con {find_query!r}")
    else:
        print(f"\n  Encontrados {len(gra_found)} documento(s) en gra "
              f"(ides DOCUMENTALES):")
        for d in gra_found:
            size = d.get("ima_bytes") or 0
            sz = f"{size/1024:.0f}KB" if size else "0"
            print(f"    gra_rep_ide={d['ide']}  nom={d.get('nom')!r}  "
                  f"fec={d.get('fec')}  {sz}")

        gra_ides_B = {d["ide"] for d in gra_found}

        # Traducir documental → negocio por (emp, cod): `rcg.gra` y los
        # `graide` apuntan a `ruesma.gra`, nunca a la documental.
        rep_a_neg = traducir_documentales_a_negocio(gra_found)
        for d in gra_found:
            d["_gra_neg_ide"] = rep_a_neg[d["ide"]]["neg_ide"]
        neg_a_rep = {v["neg_ide"]: k for k, v in rep_a_neg.items()
                     if v["neg_ide"]}
        sin_negocio = [d for d in gra_found if not d["_gra_neg_ide"]]
        print(f"\n  ↔️  {len(neg_a_rep)}/{len(gra_found)} con fila de negocio "
              f"por (emp, cod); {len(sin_negocio)} sin fila de negocio")
        for d in sin_negocio:
            print(f"    gra_rep_ide={d['ide']}  cod={d.get('cod')!r}  "
                  f"emp={d.get('emp')}  — sin fila de negocio")

        # Buscar vínculos inversos de cada gra.ide DE NEGOCIO
        links_B = buscar_conceptos_desde_gra(list(neg_a_rep), neg_a_rep)
        print(f"\n  🔗 Total vínculos inversos: {len(links_B)}")

        # Resolver contexto para cada concepto
        concepto_to_ctx: dict[int, dict] = {}
        unique_con_ides = {l["_concepto_ide"] for l in links_B if l.get("_concepto_ide")}
        print(f"  Resolviendo contexto de {len(unique_con_ides)} concepto(s)...")
        for cid in unique_con_ides:
            concepto_to_ctx[cid] = resolve_concepto_context(cid)

        # Asociar cada gra con sus vínculos y contexto
        for d in gra_found:
            gid = d["ide"]
            d_links = [l for l in links_B if l["_gra_ide"] == gid]
            d["_links"] = d_links
            for link in d_links:
                link["_ctx"] = concepto_to_ctx.get(link["_concepto_ide"], {})
            docs_B.append(d)

        # Mostrar cada doc con sus vínculos resueltos
        print(f"\n  {'═' * 66}")
        for d in docs_B:
            size = d.get("ima_bytes") or 0
            sz = f"{size/1024:.0f}KB" if size else "0"
            print(f"\n  gra_rep_ide={d['ide']}  "
                  f"gra_neg_ide={d.get('_gra_neg_ide') or '—'}  "
                  f"nom={d.get('nom')!r}  ({sz})")
            for link in d.get("_links", []):
                ctx = link.get("_ctx", {})
                tipo = ctx.get("tipo_entidad") or "?"
                print(f"    [{link['_via']}] → con.ide={link['_concepto_ide']}  "
                      f"cod={ctx.get('con_cod')!r}  [{tipo}]  {link.get('_extra', '')}")
                if ctx.get("contrato_cod"):
                    print(f"        contrato = {ctx['contrato_cod']!r}")
                if ctx.get("obra_cod"):
                    print(f"        obra     = {ctx['obra_cod']!r}  ({ctx.get('obra_res')!r})")
                if ctx.get("proveedor_cif"):
                    print(f"        proveedor= {ctx['proveedor_cif']}  ({ctx.get('proveedor_nombre')!r})")


# ═══════════════════════════════════════════════════════════════════════════
# COMPARACIÓN A vs B
# ═══════════════════════════════════════════════════════════════════════════
if cif and obra and find_query and docs_A and docs_B:
    print(f"\n{'━' * 70}")
    print(f"  🔀 COMPARACIÓN A vs B")
    print(f"{'━' * 70}")

    en_ambos = gra_ides_A & gra_ides_B
    solo_A = gra_ides_A - gra_ides_B
    solo_B = gra_ides_B - gra_ides_A

    print(f"\n  gra_rep_ide en AMBAS direcciones ({len(en_ambos)}):")
    for gid in sorted(en_ambos):
        nom = next((d.get("nom") for d in docs_B if d["ide"] == gid), "?")
        print(f"    {gid}  {nom!r}")

    print(f"\n  gra_rep_ide SOLO en A (desde CIF+obra, no contiene "
          f"'{find_query}') "
          f"({len(solo_A)}):")
    for gid in sorted(solo_A):
        doc = next((d for d in docs_A if d["_gra_ide"] == gid), {})
        print(f"    {gid}  {doc.get('_gra_nom', '?')!r}  [{doc.get('_via')}]")

    print(f"\n  gra_rep_ide SOLO en B (contiene '{find_query}' pero no vinculado "
          f"al contrato {codigo_contrato_A}) ({len(solo_B)}):")
    for gid in sorted(solo_B):
        doc = next((d for d in docs_B if d["ide"] == gid), {})
        print(f"    {gid}  {doc.get('nom', '?')!r}")
        # Mostrar a qué contratos/facturas pertenecen
        for link in doc.get("_links", []):
            ctx = link.get("_ctx", {})
            via = link["_via"]
            tipo = ctx.get("tipo_entidad") or "?"
            ctr_cod = ctx.get("contrato_cod") or "-"
            obra_cod = ctx.get("obra_cod") or "-"
            print(f"        [{via}] → {tipo}  contrato={ctr_cod}  obra={obra_cod}")


# ═══════════════════════════════════════════════════════════════════════════
# DESCARGA
# ═══════════════════════════════════════════════════════════════════════════
docs_descargables: list[dict] = []

# Priorizar docs_B si estamos en modo find
if docs_B:
    for d in docs_B:
        if (d.get("ima_bytes") or 0) > 0:
            name = d.get("nomori") or d.get("nom") or ""
            if download_match:
                if download_match.lower() in name.lower() or download_match.lower() in (d.get("cod") or "").lower():
                    docs_descargables.append({
                        "ide": d["ide"],
                        "name": name,
                        "size": d["ima_bytes"],
                    })
            else:
                docs_descargables.append({
                    "ide": d["ide"],
                    "name": name,
                    "size": d["ima_bytes"],
                })
elif docs_A:
    for d in docs_A:
        if d.get("_ima_bytes", 0) > 0:
            docs_descargables.append({
                "ide": d["_gra_ide"],
                "name": d["_gra_nom"],
                "size": d["_ima_bytes"],
            })

if docs_descargables and flag_download:
    tag = sanitize_filename(find_query) if find_query else (codigo_contrato_A or "ctr")
    download_dir = Path.home() / "Downloads" / "sigrid_docs" / tag
    download_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'━' * 70}")
    print(f"  ⬇️  DESCARGANDO {len(docs_descargables)} documento(s)")
    print(f"  📁 {download_dir}")
    print(f"{'━' * 70}")

    ok = 0
    for idx, d in enumerate(docs_descargables, start=1):
        print(f"\n  [{idx}/{len(docs_descargables)}] gra.ide={d['ide']}  {d['name']!r}")
        if download_gra(d["ide"], download_dir, d["name"]):
            ok += 1
    print(f"\n  📊 {ok}/{len(docs_descargables)} descargados")

elif docs_descargables:
    print(f"\n  ℹ️  {len(docs_descargables)} descargables. Usa --download.")

print()
print("=" * 70)
print(" FIN del diagnóstico")
print("=" * 70)