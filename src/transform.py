"""
Transformación de CSV crudos a Parquet tipado (modelo estrella).

Hechos (facts):
  - exp_ncm, imp_ncm   (base detallada por NCM, la más completa)
  - exp_mun, imp_mun   (base por municipio + SH4)

Dimensiones (dims), con nombres en español cuando existen:
  - dim_ncm, dim_ncm_sh, dim_pais, dim_uf, dim_mun, dim_via, dim_urf,
    dim_fat_agreg, dim_unidade

Notas:
  - Códigos (CO_NCM, CO_PAIS, CO_UNID, CO_VIA, CO_URF, CO_MUN...) se guardan
    como string: preservan ceros a la izquierda y garantizan joins exactos.
  - Codificación latin-1 (los archivos usan ç, ã, etc.).
  - Compresión snappy en parquet para equilibrio tamaño/velocidad.

Uso:
  python -m src.transform                 # todo
  python -m src.transform --only facts    # solo hechos
  python -m src.transform --only dims     # solo dimensiones
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

import pandas as pd

from .config import ensure_dirs, load_config, project_path

log = logging.getLogger("comexstat.transform")

SEP = ";"
ENCODINGS = ("utf-8", "latin-1")


# --------------------------------------------------------------------- #
# Helpers de lectura
# --------------------------------------------------------------------- #
def _read_csv(path, dtype: dict | None = None, usecols=None) -> pd.DataFrame:
    """Lee CSV probando codificaciones (utf-8 primero, latin-1 de respaldo)."""
    last_err = None
    for enc in ENCODINGS:
        try:
            return pd.read_csv(
                path, sep=SEP, dtype=dtype, usecols=usecols,
                encoding=enc, na_values=[""], keep_default_na=False,
            )
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise RuntimeError(f"No se pudo leer {path} (encoding): {last_err}")


def _write_parquet(df: pd.DataFrame, dest, label: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(dest, index=False, compression="snappy")
    n = len(df)
    mb = dest.stat().st_size / 1e6
    log.info("  ✓ %-14s %10s filas  %6.1f MB  -> %s", label, f"{n:,}", mb, dest.name)


# --------------------------------------------------------------------- #
# Esquemas de tipos (códigos = string, métricas = float)
# --------------------------------------------------------------------- #
NCM_FACT_DTYPES = {
    "CO_ANO": "int16", "CO_MES": "int8",
    "CO_NCM": "str", "CO_UNID": "str", "CO_PAIS": "str",
    "SG_UF_NCM": "str", "CO_VIA": "str", "CO_URF": "str",
    "QT_ESTAT": "float64", "KG_LIQUIDO": "float64", "VL_FOB": "float64",
    "VL_FRETE": "float64", "VL_SEGURO": "float64",
}
MUN_FACT_DTYPES = {
    "CO_ANO": "int16", "CO_MES": "int8",
    "SH4": "str", "CO_PAIS": "str", "SG_UF_MUN": "str", "CO_MUN": "str",
    "KG_LIQUIDO": "float64", "VL_FOB": "float64",
}


# --------------------------------------------------------------------- #
# Construcción de HECHOS
# --------------------------------------------------------------------- #
def _build_fact(cfg: dict, dataset_key: str, years: list[int]) -> None:
    ds = cfg["datasets"][dataset_key]
    src_dir = project_path(cfg["paths"][f"raw_{ds['source']}"])
    dtype = NCM_FACT_DTYPES if ds["source"] == "ncm" else MUN_FACT_DTYPES
    # columnas presentes según el layout declarado (imp tiene 2 columnas extra)
    expected_cols = list(ds["layout"])

    frames = []
    for year in years:
        # el nombre de archivo sale del template (último segmento de la URL)
        fname = ds["template"].split("/")[-1].format(year=year)
        path = src_dir / fname
        if not path.exists():
            log.warning("  (skip) no existe %s", path.name)
            continue
        df = _read_csv(path, dtype={c: t for c, t in dtype.items() if c in expected_cols})
        # conservar solo columnas esperadas que existan
        cols = [c for c in expected_cols if c in df.columns]
        df = df[cols]
        frames.append(df)
        log.info("    leído %-22s %10s filas", fname, f"{len(df):,}")

    if not frames:
        log.warning("  Sin datos para %s", dataset_key)
        return

    out = pd.concat(frames, ignore_index=True)
    proc_dir = project_path(cfg["paths"]["processed"])
    flow = "exp" if ds["flow"] == "exp" else "imp"
    dest = proc_dir / f"{flow}_{ds['source']}.parquet"
    _write_parquet(out, dest, dataset_key)


def build_facts(cfg: dict, years: list[int]) -> None:
    log.info("Hechos (facts):")
    for key in ("ncm_exp", "ncm_imp", "mun_exp", "mun_imp"):
        _build_fact(cfg, key, years)


# --------------------------------------------------------------------- #
# Construcción de DIMENSIONES (select + renombre a español)
# --------------------------------------------------------------------- #
def _first_col(df: pd.DataFrame, *candidates) -> str | None:
    """Devuelve la primera columna existente de las candidatas."""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def build_dims(cfg: dict) -> None:
    log.info("Dimensiones (dims):")
    tdir = project_path(cfg["paths"]["raw_tabelas"])
    proc_dir = project_path(cfg["paths"]["processed"])

    def load(fname, dtype=None):
        return _read_csv(tdir / fname, dtype=dtype)

    # --- dim_ncm (tabla maestra, ya trae todas las FK) ---
    ncm = load("NCM.csv", dtype="str")
    ncm = ncm.rename(columns={"NO_NCM_ESP": "NO_NCM"})
    keep = [c for c in [
        "CO_NCM", "CO_UNID", "CO_SH6", "CO_PPE", "CO_PPI", "CO_FAT_AGREG",
        "CO_CUCI_ITEM", "CO_CGCE_N3", "CO_ISIC_CLASSE", "NO_NCM",
    ] if c in ncm.columns]
    _write_parquet(ncm[keep], proc_dir / "dim_ncm.parquet", "dim_ncm")

    # --- dim_ncm_sh (jerarquía Sistema Armonizado: sección->cap->pos->subpos) ---
    sh = load("NCM_SH.csv", dtype="str")
    sh = sh.rename(columns={
        "NO_SH6_ESP": "NO_SH6", "NO_SH4_ESP": "NO_SH4",
        "NO_SH2_ESP": "NO_SH2", "NO_SEC_ESP": "NO_SEC",
    })
    keep = [c for c in [
        "CO_SH6", "NO_SH6", "CO_SH4", "NO_SH4", "CO_SH2", "NO_SH2",
        "CO_NCM_SECROM", "NO_SEC",
    ] if c in sh.columns]
    _write_parquet(sh[keep], proc_dir / "dim_ncm_sh.parquet", "dim_ncm_sh")

    # --- dim_pais (con bloque económico) ---
    p = load("PAIS.csv", dtype="str")
    name_col = "NO_PAIS_ESP" if "NO_PAIS_ESP" in p.columns else "NO_PAIS"
    pais = pd.DataFrame({
        "CO_PAIS": p["CO_PAIS"],
        "CO_PAIS_ISO3": p.get("CO_PAIS_ISOA3", ""),
        "NO_PAIS": p[name_col],
    })
    bloco = load("PAIS_BLOCO.csv", dtype="str")
    bloco_name = "NO_BLOCO_ESP" if "NO_BLOCO_ESP" in bloco.columns else "NO_BLOCO"
    bloco = (bloco[["CO_PAIS", bloco_name]]
             .rename(columns={bloco_name: "NO_BLOCO"})
             .drop_duplicates("CO_PAIS"))
    pais = pais.merge(bloco, on="CO_PAIS", how="left")
    _write_parquet(pais, proc_dir / "dim_pais.parquet", "dim_pais")

    # --- dim_uf ---
    uf = load("UF.csv", dtype="str")
    _write_parquet(uf[["SG_UF", "CO_UF", "NO_UF", "NO_REGIAO"]]
                   [[c for c in ["SG_UF", "CO_UF", "NO_UF", "NO_REGIAO"] if c in uf.columns]],
                   proc_dir / "dim_uf.parquet", "dim_uf")

    # --- dim_mun (municipio: CO_MUN_GEO = clave que usa la base MUN) ---
    mun = load("UF_MUN.csv", dtype="str")
    mun = mun.rename(columns={"CO_MUN_GEO": "CO_MUN"})
    keep = [c for c in ["CO_MUN", "NO_MUN", "SG_UF"] if c in mun.columns]
    _write_parquet(mun[keep], proc_dir / "dim_mun.parquet", "dim_mun")

    # --- dim_via ---
    via = load("VIA.csv", dtype="str")
    _write_parquet(via, proc_dir / "dim_via.parquet", "dim_via")

    # --- dim_urf ---
    urf = load("URF.csv", dtype="str")
    _write_parquet(urf, proc_dir / "dim_urf.parquet", "dim_urf")

    # --- dim_fat_agreg ---
    fa = load("NCM_FAT_AGREG.csv", dtype="str")
    _write_parquet(fa, proc_dir / "dim_fat_agreg.parquet", "dim_fat_agreg")

    # --- dim_unidade ---
    un = load("NCM_UNIDADE.csv", dtype="str")
    _write_parquet(un, proc_dir / "dim_unidade.parquet", "dim_unidade")


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Transforma CSV Comex Stat a Parquet.")
    ap.add_argument("--only", choices=["facts", "dims", "all"], default="all")
    ap.add_argument("--years", default=None, help="Años (override config), ej: 2025,2026")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    cfg = load_config()
    ensure_dirs(cfg)
    years = [int(y) for y in args.years.split(",")] if args.years else cfg["years"]

    log.info("=" * 64)
    log.info("ComexStat - Transformación CSV -> Parquet")
    log.info("  Años: %s", years)
    log.info("=" * 64)
    t0 = time.time()
    if args.only in ("facts", "all"):
        build_facts(cfg, years)
    if args.only in ("dims", "all"):
        build_dims(cfg)
    log.info("-" * 64)
    log.info("Transformación completada en %.1fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
