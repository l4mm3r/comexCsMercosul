"""
Capa de acceso a datos para el dashboard ComexStat (multi-flujo).

Soporta ver Exportación e Importación simultáneamente (UNION de flujos con
columna `flujo`), y filtros por frontera (URF) y por código NCM.

Las tablas enriquecidas v_{exp,imp}_{ncm,mun} se materializan en memoria
una sola vez (joins ya resueltos), de modo que las consultas filtradas son
muy rápidas.

Uso típico:
    db = ComexDB()
    filt = Filters(flows=["exp", "imp"], years=[2025, 2026],
                   urfs=["1017503", "1017500"], ncms=["12019000"])
    db.kpis(filt)
    db.monthly(filt)
    db.top(filt, dim="pais", n=10)
    db.compare_urfs(filt)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

import duckdb
import pandas as pd

from .config import load_config, project_path


# --------------------------------------------------------------------- #
# Modelo de filtros
# --------------------------------------------------------------------- #
@dataclass
class Filters:
    """Estado de filtrado del dashboard. Listas vacías = sin filtro.

    flows: ["exp"], ["imp"] o ["exp","imp"] (Ambos).
    urfs/ncms solo aplican a la base NCM (la de Municipio no los tiene).
    """
    flows: list[str] = field(default_factory=lambda: ["exp"])
    years: list[int] = field(default_factory=list)
    months: list[int] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)   # CO_PAIS
    ufs: list[str] = field(default_factory=list)         # SG_UF
    sections: list[str] = field(default_factory=list)    # CO_NCM_SECROM (sección SH)
    sh4: list[str] = field(default_factory=list)         # CO_SH4
    vias: list[str] = field(default_factory=list)        # CO_VIA
    fat_agreg: list[str] = field(default_factory=list)   # CO_FAT_AGREG
    urfs: list[str] = field(default_factory=list)        # CO_URF (frontera) — solo NCM
    ncms: list[str] = field(default_factory=list)        # CO_NCM (8 dígitos) — solo NCM
    ym_start: int | None = None                          # YYYYMM (ej: 202512)
    ym_end: int | None = None                            # YYYYMM (ej: 202605)


# Columnas comunes a v_exp_ncm y v_imp_ncm (la importación tiene además
# VL_FRETE/VL_SEGURO/VL_CIF, que se tratan aparte en kpis).
NCM_COMMON_COLS = """
CO_ANO, CO_MES, CO_NCM, CO_UNID, CO_PAIS, SG_UF, CO_VIA, CO_URF,
QT_ESTAT, KG_LIQUIDO, VL_FOB,
NO_NCM, CO_SH6, NO_SH6, CO_SH4, NO_SH4, CO_SH2, NO_SH2,
CO_NCM_SECROM, NO_SEC, CO_FAT_AGREG, NO_FAT_AGREG, NO_FAT_AGREG_GP,
NO_PAIS, CO_PAIS_ISO3, NO_BLOCO, NO_UF, NO_REGIAO, NO_VIA, NO_URF
"""
MUN_COMMON_COLS = """
CO_ANO, CO_MES, CO_SH4, CO_PAIS, SG_UF, CO_MUN, KG_LIQUIDO, VL_FOB,
NO_MUN, NO_PAIS, NO_BLOCO, NO_UF, NO_REGIAO
"""

# Mapa dimensión -> (columna código, columna nombre) para top()
_DIMS = {
    "pais": ("CO_PAIS", "NO_PAIS"),
    "uf": ("SG_UF", "NO_UF"),
    "sh4": ("CO_SH4", "NO_SH4"),
    "seccion": ("CO_NCM_SECROM", "NO_SEC"),
    "via": ("CO_VIA", "NO_VIA"),
    "fat_agreg": ("CO_FAT_AGREG", "NO_FAT_AGREG"),
    "ncm": ("CO_NCM", "NO_NCM"),
    "urf": ("CO_URF", "NO_URF"),
}


def clean_urf_name(name: str) -> str:
    """Limpia el nombre de una URF: '1017503 - IRF - SÃO BORJA' -> 'SÃO BORJA'."""
    if not name:
        return name
    s = re.sub(r"^\s*\d+\s*-\s*", "", str(name))  # quita "1017503 - "
    for prefix in ("ALF - ", "IRF - ", "ALF/", "IRF/"):
        if s.upper().startswith(prefix):
            s = s[len(prefix):]
            break
    return s.strip()


# --------------------------------------------------------------------- #
# Conexión y tablas
# --------------------------------------------------------------------- #
class ComexDB:
    """Conexión DuckDB con tablas enriquecidas y consultas multi-flujo."""

    def __init__(self, cfg: dict | None = None, materialize: bool = True):
        self.cfg = cfg or load_config()
        self.proc = project_path(self.cfg["paths"]["processed"]).resolve()
        self.con = duckdb.connect()
        try:
            self.con.execute("PRAGMA disable_progress_bar")
        except Exception:
            pass
        self._build(materialize=materialize)

    def _p(self, name: str) -> str:
        return f"'{(self.proc / name).as_posix()}'"

    def _build(self, materialize: bool = True) -> None:
        p = self._p
        kind = "TABLE" if materialize else "VIEW"

        def ncm_view(flow: str) -> str:
            extra = ""
            if flow == "imp":
                extra = ", f.VL_FRETE, f.VL_SEGURO, (f.VL_FOB+f.VL_FRETE+f.VL_SEGURO) AS VL_CIF"
            return f"""
                CREATE OR REPLACE {kind} v_{flow}_ncm AS
                SELECT f.CO_ANO, f.CO_MES, f.CO_NCM, f.CO_UNID,
                       f.CO_PAIS, f.SG_UF_NCM AS SG_UF, f.CO_VIA, f.CO_URF,
                       f.QT_ESTAT, f.KG_LIQUIDO, f.VL_FOB{extra},
                       n.NO_NCM, n.CO_SH6, s.NO_SH6, s.CO_SH4, s.NO_SH4,
                       s.CO_SH2, s.NO_SH2, s.CO_NCM_SECROM, s.NO_SEC,
                       n.CO_FAT_AGREG, fa.NO_FAT_AGREG, fa.NO_FAT_AGREG_GP,
                       p.NO_PAIS, p.CO_PAIS_ISO3, p.NO_BLOCO,
                       u.NO_UF, u.NO_REGIAO, v.NO_VIA, r.NO_URF
                FROM read_parquet({p(f'{flow}_ncm.parquet')}) f
                LEFT JOIN read_parquet({p('dim_ncm.parquet')}) n ON f.CO_NCM = n.CO_NCM
                LEFT JOIN read_parquet({p('dim_ncm_sh.parquet')}) s ON n.CO_SH6 = s.CO_SH6
                LEFT JOIN read_parquet({p('dim_fat_agreg.parquet')}) fa ON n.CO_FAT_AGREG = fa.CO_FAT_AGREG
                LEFT JOIN read_parquet({p('dim_pais.parquet')}) p ON f.CO_PAIS = p.CO_PAIS
                LEFT JOIN read_parquet({p('dim_uf.parquet')}) u ON f.SG_UF_NCM = u.SG_UF
                LEFT JOIN read_parquet({p('dim_via.parquet')}) v ON f.CO_VIA = v.CO_VIA
                LEFT JOIN read_parquet({p('dim_urf.parquet')}) r ON f.CO_URF = r.CO_URF
            """

        def mun_view(flow: str) -> str:
            return f"""
                CREATE OR REPLACE {kind} v_{flow}_mun AS
                SELECT f.CO_ANO, f.CO_MES, f.SH4 AS CO_SH4, f.CO_PAIS,
                       f.SG_UF_MUN AS SG_UF, f.CO_MUN,
                       f.KG_LIQUIDO, f.VL_FOB,
                       m.NO_MUN, p.NO_PAIS, p.NO_BLOCO,
                       u.NO_UF, u.NO_REGIAO
                FROM read_parquet({p(f'{flow}_mun.parquet')}) f
                LEFT JOIN read_parquet({p('dim_mun.parquet')}) m ON f.CO_MUN = m.CO_MUN
                LEFT JOIN read_parquet({p('dim_pais.parquet')}) p ON f.CO_PAIS = p.CO_PAIS
                LEFT JOIN read_parquet({p('dim_uf.parquet')}) u ON f.SG_UF_MUN = u.SG_UF
            """

        for flow in ("exp", "imp"):
            self.con.execute(ncm_view(flow))
            self.con.execute(mun_view(flow))

    # ----------------------------------------------------------------- #
    # Construcción de fuente (UNION de flujos) y WHERE parametrizado
    # ----------------------------------------------------------------- #
    def _source(self, filt: Filters, table: str = "ncm") -> str:
        """Subquery con la UNION de los flujos seleccionados + columna `flujo`."""
        cols = NCM_COMMON_COLS if table == "ncm" else MUN_COMMON_COLS
        parts = [f"SELECT {cols}, '{fl}' AS flujo FROM v_{fl}_{table}"
                 for fl in filt.flows]
        return "(" + " UNION ALL ".join(parts) + ")"

    def _where(self, filt: Filters, table: str = "ncm") -> tuple[str, list]:
        """Devuelve (sql_sin_WHERE_incluyendo_espacio, params)."""
        clauses, params = [], []
        if filt.ym_start is not None and filt.ym_end is not None:
            clauses.append("(CAST(CO_ANO AS INTEGER) * 100 + CO_MES BETWEEN ? AND ?)")
            params += [filt.ym_start, filt.ym_end]
        else:
            if filt.years:
                clauses.append(f"CO_ANO IN ({','.join('?' * len(filt.years))})")
                params += list(filt.years)
            if filt.months:
                clauses.append(f"CO_MES IN ({','.join('?' * len(filt.months))})")
                params += list(filt.months)
        if filt.countries:
            clauses.append(f"CO_PAIS IN ({','.join('?' * len(filt.countries))})")
            params += [str(c) for c in filt.countries]
        if filt.ufs:
            clauses.append(f"SG_UF IN ({','.join('?' * len(filt.ufs))})")
            params += list(filt.ufs)
        # Filtros exclusivos de la base NCM
        if table == "ncm":
            if filt.sections:
                clauses.append(f"CO_NCM_SECROM IN ({','.join('?' * len(filt.sections))})")
                params += list(filt.sections)
            if filt.sh4:
                clauses.append(f"CO_SH4 IN ({','.join('?' * len(filt.sh4))})")
                params += [str(s) for s in filt.sh4]
            if filt.vias:
                clauses.append(f"CO_VIA IN ({','.join('?' * len(filt.vias))})")
                params += list(filt.vias)
            if filt.fat_agreg:
                clauses.append(f"CO_FAT_AGREG IN ({','.join('?' * len(filt.fat_agreg))})")
                params += list(filt.fat_agreg)
            if filt.urfs:
                clauses.append(f"CO_URF IN ({','.join('?' * len(filt.urfs))})")
                params += list(filt.urfs)
            if filt.ncms:
                clauses.append(f"CO_NCM IN ({','.join('?' * len(filt.ncms))})")
                params += [str(c) for c in filt.ncms]
        sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return sql, params

    # ----------------------------------------------------------------- #
    # Opciones para los widgets del sidebar
    # ----------------------------------------------------------------- #
    def filter_options(self) -> dict:
        """Diccionarios {código: nombre} para poblar los multiselect."""
        opt = {}
        opt["years"] = [int(r[0]) for r in
                        self.con.execute("SELECT DISTINCT CO_ANO FROM v_exp_ncm ORDER BY 1").fetchall()]
        opt["countries"] = self._opts("SELECT DISTINCT CO_PAIS, NO_PAIS FROM v_exp_ncm "
                                       "WHERE NO_PAIS IS NOT NULL ORDER BY 2")
        opt["ufs"] = self._opts("SELECT DISTINCT SG_UF, NO_UF FROM v_exp_ncm "
                                 "WHERE NO_UF IS NOT NULL ORDER BY 2")
        opt["sections"] = self._opts("SELECT DISTINCT CO_NCM_SECROM, NO_SEC FROM v_exp_ncm "
                                      "WHERE NO_SEC IS NOT NULL ORDER BY 2")
        opt["vias"] = self._opts("SELECT DISTINCT CO_VIA, NO_VIA FROM v_exp_ncm "
                                  "WHERE NO_VIA IS NOT NULL ORDER BY 2")
        opt["fat_agreg"] = self._opts("SELECT DISTINCT CO_FAT_AGREG, NO_FAT_AGREG FROM v_exp_ncm "
                                       "WHERE NO_FAT_AGREG IS NOT NULL ORDER BY 2")
        opt["sh4"] = self._opts("SELECT DISTINCT CO_SH4, NO_SH4 FROM v_exp_ncm "
                                 "WHERE NO_SH4 IS NOT NULL ORDER BY 2")
        opt["urfs"] = self._urf_options()
        opt["ncms"] = self._ncm_options()
        return opt

    def _opts(self, sql: str) -> dict:
        return {str(r[0]): r[1] for r in self.con.execute(sql).fetchall()}

    def _urf_options(self) -> dict:
        """URFs con datos reales, nombres limpios y desambiguados."""
        rows = self.con.execute("""
            SELECT DISTINCT CO_URF, NO_URF FROM (
                SELECT CO_URF, NO_URF FROM v_exp_ncm WHERE CO_URF IS NOT NULL
                UNION ALL SELECT CO_URF, NO_URF FROM v_imp_ncm WHERE CO_URF IS NOT NULL
            )
        """).fetchall()
        by_code = {str(c): clean_urf_name(n) for c, n in rows}
        # desambiguar nombres duplicados añadiendo código entre paréntesis
        from collections import Counter
        counts = Counter(by_code.values())
        return {code: (f"{name} ({code[-4:]})" if counts[name] > 1 else name)
                for code, name in by_code.items()}

    def _ncm_options(self) -> dict:
        """NCM con datos: {código: descripción}."""
        rows = self.con.execute("""
            SELECT DISTINCT CO_NCM, NO_NCM FROM (
                SELECT CO_NCM, NO_NCM FROM v_exp_ncm
                UNION ALL SELECT CO_NCM, NO_NCM FROM v_imp_ncm
            ) WHERE NO_NCM IS NOT NULL
        """).fetchall()
        return {str(c): n for c, n in rows}

    # ----------------------------------------------------------------- #
    # Consultas
    # ----------------------------------------------------------------- #
    def kpis(self, filt: Filters) -> dict:
        """KPIs por flujo + agregados de saldo y corriente."""
        where, params = self._where(filt)
        src = self._source(filt)
        q = f"""
            SELECT flujo, SUM(VL_FOB) AS fob, SUM(KG_LIQUIDO) AS kg,
                   COUNT(*) AS n_reg, SUM(QT_ESTAT) AS qt
            FROM {src} s {where}
            GROUP BY flujo
        """
        df = self.con.execute(q, params).fetchdf()
        per_flow = {}
        for fl in filt.flows:
            row = df[df["flujo"] == fl]
            if not row.empty:
                fob = float(row.iloc[0]["fob"] or 0)
                kg = float(row.iloc[0]["kg"] or 0)
                d = {"fob": fob, "kg": kg,
                     "n_reg": int(row.iloc[0]["n_reg"] or 0),
                     "qt": float(row.iloc[0]["qt"] or 0),
                     "avg_price": (fob / kg) if kg else 0.0}
            else:
                d = {"fob": 0, "kg": 0, "n_reg": 0, "qt": 0, "avg_price": 0.0}
            # CIF solo para importación
            d["cif"] = None
            if fl == "imp":
                cif = self.con.execute(
                    f"SELECT SUM(VL_CIF) FROM v_imp_ncm{where}", params
                ).fetchone()[0]
                d["cif"] = float(cif or 0)
            per_flow[fl] = d
        exp_fob = per_flow.get("exp", {}).get("fob", 0)
        imp_fob = per_flow.get("imp", {}).get("fob", 0)
        return {
            "flows": per_flow,
            "saldo": exp_fob - imp_fob,
            "corriente": exp_fob + imp_fob,
        }

    def monthly(self, filt: Filters) -> pd.DataFrame:
        """Evolución mensual por año y por flujo."""
        where, params = self._where(filt)
        src = self._source(filt)
        q = f"""
            SELECT flujo, CO_ANO, CO_MES,
                   SUM(VL_FOB) AS fob, SUM(KG_LIQUIDO) AS kg
            FROM {src} s {where}
            GROUP BY flujo, CO_ANO, CO_MES
            ORDER BY flujo, CO_ANO, CO_MES
        """
        df = self.con.execute(q, params).fetchdf()
        if not df.empty:
            df["CO_ANO"] = df["CO_ANO"].astype(int)
            df["CO_MES"] = df["CO_MES"].astype(int)
        return df

    def top(self, filt: Filters, dim: str, n: int = 10) -> pd.DataFrame:
        """Ranking de los top-n ítems por FOB total, con desglose por flujo.

        dim: 'pais' | 'uf' | 'sh4' | 'seccion' | 'via' | 'fat_agreg'.
        Devuelve columnas: codigo, nombre, flujo, fob, kg, n_reg.
        """
        code_col, name_col = _DIMS[dim]
        where, params = self._where(filt)
        src = self._source(filt)
        q = f"""
            SELECT {code_col} AS codigo, COALESCE({name_col}, CAST({code_col} AS VARCHAR)) AS nombre,
                   flujo, SUM(VL_FOB) AS fob, SUM(KG_LIQUIDO) AS kg, COUNT(*) AS n_reg
            FROM {src} s {where}
            GROUP BY {code_col}, {name_col}, flujo
        """
        df = self.con.execute(q, params).fetchdf()
        if df.empty:
            return df
        # top-n por FOB total (suma de flujos)
        totals = df.groupby("codigo")["fob"].sum().sort_values(ascending=False)
        top_codes = totals.head(n).index
        df = df[df["codigo"].isin(top_codes)].copy()
        df["_total"] = df.groupby("codigo")["fob"].transform("sum")
        df = df.sort_values(["_total", "flujo"], ascending=[False, True]).drop(columns="_total")
        return df

    def detail(self, filt: Filters, limit: int = 500) -> pd.DataFrame:
        where, params = self._where(filt)
        src = self._source(filt)
        q = f"""
            SELECT flujo, CO_ANO AS ano, CO_MES AS mes, NO_PAIS AS pais, NO_UF AS uf,
                   CO_URF AS urf_cod, NO_URF AS frontera,
                   CO_NCM AS ncm, NO_NCM AS producto, NO_SH4 AS posicion_sh4,
                   NO_VIA AS via, KG_LIQUIDO AS kg, VL_FOB AS fob_usd
            FROM {src} s {where}
            ORDER BY VL_FOB DESC LIMIT {int(limit)}
        """
        df = self.con.execute(q, params).fetchdf()
        return df if df is not None else pd.DataFrame()

    def camiones_por_ncm(self, filt: Filters, n: int = 200) -> pd.DataFrame:
        """Tabla agregada por NCM con cálculo de camiones.

        camiones = toneladas / 15 (peso medio referencial por camión).
        Aplica a todos los NCM como valor de referencia.
        """
        where, params = self._where(filt)
        src = self._source(filt)
        q = f"""
            SELECT flujo, CO_NCM AS ncm, NO_NCM AS producto,
                   ROUND(SUM(KG_LIQUIDO) / 1000.0, 0) AS toneladas,
                   SUM(VL_FOB) AS fob_total,
                   ROUND(SUM(KG_LIQUIDO) / 15000.0, 0) AS camiones,
                   COUNT(*) AS n_ops
            FROM {src} s {where}
            GROUP BY flujo, CO_NCM, NO_NCM
            ORDER BY camiones DESC, fob_total DESC
            LIMIT {int(n)}
        """
        df = self.con.execute(q, params).fetchdf()
        return df if df is not None else pd.DataFrame()

    def trade_balance(self, filt: Filters) -> pd.DataFrame:
        """Balanza mensual (exp, imp, saldo) respetando todos los filtros.
        Siempre calcula ambos flujos, sin importar filt.flows."""
        f = replace(filt, flows=["exp", "imp"])
        where, params = self._where(f)
        src = self._source(f)
        q = f"""
            SELECT CO_ANO, CO_MES, flujo, SUM(VL_FOB) AS fob
            FROM {src} s {where}
            GROUP BY CO_ANO, CO_MES, flujo
            ORDER BY CO_ANO, CO_MES
        """
        df = self.con.execute(q, params).fetchdf()
        if df.empty:
            return df
        df["CO_ANO"] = df["CO_ANO"].astype(int)
        df["CO_MES"] = df["CO_MES"].astype(int)
        piv = df.pivot_table(index=["CO_ANO", "CO_MES"], columns="flujo",
                             values="fob", aggfunc="sum").reset_index()
        for fl in ("exp", "imp"):
            if fl not in piv.columns:
                piv[fl] = 0.0
        piv["saldo"] = piv["exp"] - piv["imp"]
        return piv.sort_values(["CO_ANO", "CO_MES"])

    def compare_urfs(self, filt: Filters) -> dict:
        """Comparación entre las URF seleccionadas (requiere filt.urfs).
        Devuelve {'summary': df por URF+flujo, 'top_products': df por URF+flujo}."""
        f = replace(filt, flows=["exp", "imp"])
        where, params = self._where(f)  # incluye CO_URF IN (seleccionadas)
        src = self._source(f)
        q = f"""
            SELECT CO_URF AS codigo, COALESCE(NO_URF, CAST(CO_URF AS VARCHAR)) AS frontera,
                   flujo, SUM(VL_FOB) AS fob, SUM(KG_LIQUIDO) AS kg, COUNT(*) AS n_reg
            FROM {src} s {where}
            GROUP BY CO_URF, NO_URF, flujo
            ORDER BY CO_URF, flujo
        """
        summary = self.con.execute(q, params).fetchdf()
        if not summary.empty:
            summary["frontera"] = summary["frontera"].map(clean_urf_name)
        q2 = f"""
            WITH per AS (
                SELECT CO_URF, CO_SH4, COALESCE(NO_SH4, CO_SH4) AS producto, flujo,
                       COALESCE(NO_URF, CAST(CO_URF AS VARCHAR)) AS frontera,
                       SUM(VL_FOB) AS fob,
                       ROW_NUMBER() OVER (
                           PARTITION BY CO_URF, flujo ORDER BY SUM(VL_FOB) DESC
                       ) AS rn
                FROM {src} s {where}
                GROUP BY CO_URF, CO_SH4, NO_SH4, NO_URF, flujo
            )
            SELECT CO_URF AS codigo, flujo, producto, frontera, fob FROM per WHERE rn = 1
        """
        top_products = self.con.execute(q2, params).fetchdf()
        if not top_products.empty:
            top_products["frontera"] = top_products["frontera"].map(clean_urf_name)
        return {"summary": summary, "top_products": top_products}

    def latest_period(self) -> int:
        """Último año-mes con datos, formato YYYYMM (ej: 202605)."""
        return self.con.execute(
            "SELECT MAX(CAST(CO_ANO AS INTEGER) * 100 + CO_MES) FROM v_exp_ncm"
        ).fetchone()[0]

    def top_products_by_urf(self, filt: Filters, n: int = 10) -> pd.DataFrame:
        """Top-N productos por toneladas para cada URF seleccionada.

        Devuelve: codigo(CO_URF), frontera, flujo, ncm, producto,
                  toneladas, fob, n_ops, rn(ranking dentro de cada URF+flujo).
        """
        where, params = self._where(filt)
        src = self._source(filt)
        q = f"""
            WITH ranked AS (
                SELECT CO_URF, COALESCE(NO_URF, CAST(CO_URF AS VARCHAR)) AS frontera,
                       CO_NCM, NO_NCM, flujo,
                       ROUND(SUM(KG_LIQUIDO) / 1000.0, 0) AS toneladas,
                       SUM(VL_FOB) AS fob,
                       COUNT(*) AS n_ops,
                       ROW_NUMBER() OVER (
                           PARTITION BY CO_URF, flujo
                           ORDER BY SUM(KG_LIQUIDO) DESC
                       ) AS rn
                FROM {src} s {where}
                GROUP BY CO_URF, NO_URF, CO_NCM, NO_NCM, flujo
            )
            SELECT CO_URF AS codigo, frontera, flujo, CO_NCM AS ncm,
                   NO_NCM AS producto, toneladas, fob, n_ops, rn
            FROM ranked WHERE rn <= {int(n)}
            ORDER BY CO_URF, flujo, rn
        """
        df = self.con.execute(q, params).fetchdf()
        if df is None or df.empty:
            return pd.DataFrame()
        df["frontera"] = df["frontera"].map(clean_urf_name)
        return df

    def top_municipios(self, filt: Filters, n: int = 15) -> pd.DataFrame:
        """Ranking de municipios (base por municipio). URF/NCM no aplican aquí."""
        where, params = self._where(filt, table="mun")
        src = self._source(filt, table="mun")
        q = f"""
        SELECT CO_MUN AS codigo, COALESCE(NO_MUN, CO_MUN) AS municipio,
               SG_UF, NO_UF, flujo, SUM(VL_FOB) AS fob, SUM(KG_LIQUIDO) AS kg
        FROM {src} s {where}
        GROUP BY CO_MUN, NO_MUN, SG_UF, NO_UF, flujo
        """
        df = self.con.execute(q, params).fetchdf()
        if df.empty:
            return df
        totals = df.groupby("codigo")["fob"].sum().sort_values(ascending=False)
        top_codes = totals.head(n).index
        df = df[df["codigo"].isin(top_codes)].copy()
        df["_total"] = df.groupby("codigo")["fob"].transform("sum")
        df = df.sort_values(["_total", "flujo"], ascending=[False, True]).drop(columns="_total")
        return df
