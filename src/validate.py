"""
Validación de los Parquet contra los totales oficiales (TOTAIS_CONFERENCIA).

Compara, por año y por dataset, los siguientes agregados calculados desde
nuestros Parquet frente a los valores oficiales publicados por Comex Stat:
  - NUMERO_LINHAS (conteo de filas)
  - KG_LIQUIDO (suma)
  - VL_FOB (suma)
  - QT_ESTAT (suma, solo base NCM)

Los agregados se calculan con DuckDB (motor OLAP que usará el dashboard),
lo que además valida que el Parquet se lee correctamente.

Uso:
  python -m src.validate
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import duckdb
import pandas as pd

from .config import load_config, project_path

log = logging.getLogger("comexstat.validate")

# datasets a validar: (parquet_key, conf_key, grupo_raw, arquivo_pattern, tiene_qt_estat)
DATASETS = [
    ("exp_ncm", "ncm_exp", "ncm", "EXP_{year}.csv", True),
    ("imp_ncm", "ncm_imp", "ncm", "IMP_{year}.csv", True),
    ("exp_mun", "mun_exp", "mun", "EXP_{year}_MUN.csv", False),
    ("imp_mun", "mun_imp", "mun", "IMP_{year}_MUN.csv", False),
]


def _read_conferencia(path: Path, has_qt: bool) -> pd.DataFrame:
    """Lee un archivo TOTAIS_CONFERENCIA (latin-1, ; separado, valores con comillas)."""
    for enc in ("utf-8", "latin-1"):
        try:
            df = pd.read_csv(path, sep=";", encoding=enc, dtype=str)
            break
        except UnicodeDecodeError:
            continue
    # limpiar comillas residuales y espacios en nombres de archivo/códigos
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip().str.strip('"')
    return df


def _compute_totals(parquet_path: Path, has_qt: bool) -> pd.DataFrame:
    """Agrega el Parquet por CO_ANO con DuckDB."""
    con = duckdb.connect()
    qt = ", SUM(QT_ESTAT) AS QT_ESTAT" if has_qt else ""
    q = f"""
        SELECT CO_ANO,
               COUNT(*)        AS NUMERO_LINHAS,
               SUM(KG_LIQUIDO) AS KG_LIQUIDO,
               SUM(VL_FOB)     AS VL_FOB{qt}
        FROM read_parquet('{parquet_path.as_posix()}')
        GROUP BY CO_ANO
        ORDER BY CO_ANO
    """
    df = con.execute(q).fetchdf()
    df["CO_ANO"] = df["CO_ANO"].astype(str)
    return df


def _fmt(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (ValueError, TypeError):
        return str(v)


def _diff_label(official: float, computed: float) -> tuple[str, bool]:
    """Devuelve (texto_diferencia, ok?). ok si coinciden (exacto o epsilon)."""
    if official == computed:
        return "OK", True
    # epsilon para tolerar diferencias ínfimas de float
    denom = max(abs(official), 1.0)
    rel = abs(official - computed) / denom
    if rel < 1e-9:
        return "OK (≈)", True
    delta = computed - official
    return f"Δ {delta:+,.0f}", False


def validate(cfg: dict) -> int:
    years = [str(y) for y in cfg["years"]]
    proc = project_path(cfg["paths"]["processed"])
    conf_urls = cfg["validation"]
    raw_map = {"ncm": cfg["paths"]["raw_ncm"], "mun": cfg["paths"]["raw_mun"]}

    # descarga simple de los 4 archivos de conferencia (reusa el downloader)
    from .download import ComexDownloader, RemoteFile
    dl = ComexDownloader(cfg)
    conf_files = []
    for pkey, ckey, group, _, _ in DATASETS:
        url = conf_urls[ckey]
        fname = Path(url).name
        conf_files.append(RemoteFile(
            url=url, dest=project_path(raw_map[group], fname),
            label=f"Conferencia {pkey}", group="validation",
        ))
    log.info("Descargando totales oficiales de conferencia...")
    dl.fetch_many(conf_files)

    all_ok = True
    print()
    print("=" * 78)
    print("VALIDACIÓN: Parquet vs TOTAIS_CONFERENCIA (oficial)")
    print("=" * 78)

    for pkey, ckey, group, pattern, has_qt in DATASETS:
        parquet_path = proc / f"{pkey}.parquet"
        if not parquet_path.exists():
            log.warning("Falta %s, se omite.", parquet_path.name)
            all_ok = False
            continue

        conf_path = project_path(raw_map[group], Path(conf_urls[ckey]).name)
        conf = _read_conferencia(conf_path, has_qt)
        comp = _compute_totals(parquet_path, has_qt)

        print(f"\n■ {pkey.upper()}  ({parquet_path.name})")
        header = f"  {'Año':<6}{'Métrica':<16}{'Oficial':>22}{'Calculado':>22}  Estado"
        print(header)
        print("  " + "-" * (len(header) - 2))

        for year in years:
            row_off = conf[conf["ARQUIVO"] == pattern.format(year=year)]
            row_com = comp[comp["CO_ANO"] == year]
            if row_off.empty:
                print(f"  {year:<6}{'(sin dato oficial)':<40}")
                all_ok = False
                continue
            if row_com.empty:
                print(f"  {year:<6}{'(sin dato en parquet)':<40}")
                all_ok = False
                continue
            off = row_off.iloc[0]
            com = row_com.iloc[0]
            metrics = [("NUMERO_LINHAS", has_qt is not None),
                       ("KG_LIQUIDO", True), ("VL_FOB", True)]
            if has_qt:
                metrics.append(("QT_ESTAT", True))
            for metric, _ in metrics:
                ov = float(off[metric])
                cv = float(com[metric])
                state, ok = _diff_label(ov, cv)
                if not ok:
                    all_ok = False
                print(f"  {year:<6}{metric:<16}{_fmt(ov):>22}{_fmt(cv):>22}  {state}")

    print("\n" + "=" * 78)
    if all_ok:
        print("✓ VALIDACIÓN CORRECTA: todos los totales coinciden con el oficial.")
    else:
        print("✗ VALIDACIÓN CON DISCREPANCIAS (revisar líneas marcadas).")
    print("=" * 78)
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config()
    return validate(cfg)


if __name__ == "__main__":
    sys.exit(main())
