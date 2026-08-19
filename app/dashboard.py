"""
Dashboard ComexStat - Comercio Exterior de Brasil (2025-2026)
App Streamlit para el equipo comercial: filtros multi-flujo (Exp+Imp),
comparación entre fronteras (URF) y filtro por NCM.

Ejecutar:
  streamlit run app/dashboard.py
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app.formatting import NOMBRES_MESES, fmt_int, fmt_money, fmt_price, fmt_weight
from src.config import project_path
from src.query import ComexDB, Filters

PALETTE = {"exp": "#1f9d55", "imp": "#e3342f"}
FLOW_LABELS = {"exp": "Exportación", "imp": "Importación"}
MES_LABEL = [NOMBRES_MESES[i] for i in range(1, 13)]

_SERIE_COLORS = {
    "EXP 2025": "#6ee7b7",
    "EXP 2026": "#1f9d55",
    "IMP 2025": "#fca5a5",
    "IMP 2026": "#e3342f",
}

PERIOD_MONTHS = {"1m": 1, "3m": 3, "6m": 6, "1a": 12}
PERIOD_LABELS = {
    "1m": "Último mes", "3m": "Últimos 3 meses",
    "6m": "Últimos 6 meses", "1a": "Último año",
}


def _compute_ym_range(latest_ym: int, months_back: int) -> tuple[int, int]:
    year, month = latest_ym // 100, latest_ym % 100
    total = year * 12 + (month - 1) - (months_back - 1)
    sy, sm = total // 12, total % 12 + 1
    return sy * 100 + sm, latest_ym


def _ym_label(ym: int) -> str:
    y, m = ym // 100, ym % 100
    return f"{NOMBRES_MESES[m].lower()} {y}"


# ----------------------------------------------------------------- #
# Recursos cacheados
# ----------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def get_db() -> ComexDB:
    return ComexDB()


@st.cache_data(show_spinner=False)
def get_options(_db: ComexDB, _schema_version: int = 2) -> dict:
    return _db.filter_options()


def data_freshness() -> str:
    proc = project_path("data/processed")
    try:
        ts = max(f.stat().st_mtime for f in proc.glob("*.parquet"))
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return "desconocida"


# ----------------------------------------------------------------- #
# Sidebar
# ----------------------------------------------------------------- #
def sidebar(opt: dict) -> Filters:
    st.sidebar.title("🔍 Filtros")

    flows = st.sidebar.multiselect(
        "Operación", ["exp", "imp"], default=["exp"],
        format_func=lambda x: FLOW_LABELS[x],
        help="Selecciona uno o ambos flujos para comparar.",
    )
    if not flows:
        flows = ["exp"]

    years = st.sidebar.multiselect("Año", opt["years"], default=opt["years"])
    meses = st.sidebar.multiselect(
        "Mes", list(range(1, 13)), default=[],
        format_func=lambda m: NOMBRES_MESES[m],
        help="Vacío = acumulado anual.",
    )

    with st.sidebar.expander("🛃 Frontera (URF)", expanded=False):
        urfs = _multiselect_named("Frontera / Aduana", opt["urfs"])

    with st.sidebar.expander("📦 Producto", expanded=False):
        sections = _multiselect_named("Sección SH", opt["sections"])
        sh4 = _multiselect_named("Posición SH4", opt["sh4"])
        fat = _multiselect_named("Factor agregado", opt["fat_agreg"])
        ncms = _ncm_picker(opt["ncms"])

    with st.sidebar.expander("🌎 País / Bloque", expanded=False):
        countries = _multiselect_named("País", opt["countries"])

    with st.sidebar.expander("📍 Geografía (UF)", expanded=False):
        ufs = _multiselect_named("Estado (UF)", opt["ufs"])

    with st.sidebar.expander("🚚 Operacional", expanded=False):
        vias = _multiselect_named("Vía de transporte", opt["vias"])

    return Filters(
        flows=flows, years=years, months=meses,
        countries=countries, ufs=ufs, sections=sections,
        sh4=sh4, vias=vias, fat_agreg=fat,
        urfs=urfs, ncms=ncms,
    )


def _multiselect_named(label: str, options: dict) -> list[str]:
    if not options:
        return []
    nombres = sorted(options.values())
    sel = st.multiselect(label, nombres, default=[])
    inv = {v: k for k, v in options.items()}
    return [inv[n] for n in sel]


def _ncm_picker(ncms: dict) -> list[str]:
    if not ncms:
        return []
    search = st.text_input(
        "🔍 Buscar NCM (código o descripción)",
        placeholder="Ej: soja, tractor, 8701...",
    )
    if not search or len(search.strip()) < 2:
        return []
    q = search.strip().lower()
    matches = {k: v for k, v in ncms.items() if q in k.lower() or q in v.lower()}
    if not matches:
        st.caption("Sin coincidencias.")
        return []
    matches = dict(sorted(matches.items())[:100])
    fmt = {k: f"{k} — {v[:60]}" for k, v in matches.items()}
    sel = st.multiselect(f"NCM ({len(matches)} coincidencias)", list(fmt.values()))
    inv = {v: k for k, v in fmt.items()}
    return [inv[s] for s in sel]


def active_filters_badges(f: Filters) -> str:
    p = []
    if len(f.flows) > 1:
        p.append("Ambos flujos")
    if f.urfs:
        p.append(f"{len(f.urfs)} frontera(s)")
    if f.ncms:
        p.append(f"{len(f.ncms)} NCM(s)")
    if f.countries:
        p.append(f"{len(f.countries)} país(es)")
    if f.ufs:
        p.append(f"{len(f.ufs)} UF(s)")
    if f.sections:
        p.append(f"{len(f.sections)} sección(es)")
    if f.sh4:
        p.append(f"{len(f.sh4)} SH4")
    if f.fat_agreg:
        p.append(f"{len(f.fat_agreg)} factor(es)")
    if f.vias:
        p.append(f"{len(f.vias)} vía(s)")
    if f.months:
        p.append(f"{len(f.months)} mes(es)")
    return ", ".join(p) if p else "ninguno (todos los datos)"


# ----------------------------------------------------------------- #
# KPIs
# ----------------------------------------------------------------- #
def render_kpis(db: ComexDB, f: Filters):
    k = db.kpis(f)

    if len(f.flows) == 1:
        fl = f.flows[0]
        d = k["flows"][fl]
        c = st.columns(4)
        c[0].metric(f"FOB {FLOW_LABELS[fl]}", fmt_money(d["fob"]))
        c[1].metric("Peso líquido", fmt_weight(d["kg"]))
        c[2].metric("Precio medio", fmt_price(d["avg_price"]))
        c[3].metric("Operaciones", fmt_int(d["n_reg"]))
        if fl == "imp" and d.get("cif"):
            c2 = st.columns(3)
            c2[0].metric("CIF (flete+seguro)", fmt_money(d["cif"]))
            c2[1].metric("Flete+Seguro", fmt_money(d["cif"] - d["fob"]))
    else:
        de = k["flows"].get("exp", {})
        di = k["flows"].get("imp", {})
        c = st.columns(4)
        c[0].metric("FOB Exportación", fmt_money(de.get("fob", 0)))
        c[1].metric("FOB Importación", fmt_money(di.get("fob", 0)))
        c[2].metric("Saldo", fmt_money(k["saldo"]),
                     delta="Superávit" if k["saldo"] >= 0 else "Déficit")
        c[3].metric("Corriente", fmt_money(k["corriente"]))
        c2 = st.columns(4)
        c2[0].metric("Peso Exp", fmt_weight(de.get("kg", 0)))
        c2[1].metric("Precio Exp", fmt_price(de.get("avg_price", 0)))
        c2[2].metric("Peso Imp", fmt_weight(di.get("kg", 0)))
        c2[3].metric("Precio Imp", fmt_price(di.get("avg_price", 0)))

    st.caption(
        f"Saldo (Exp − Imp): **{fmt_money(k['saldo'])}** · "
        f"Corriente: **{fmt_money(k['corriente'])}**"
    )


# ----------------------------------------------------------------- #
# Gráficos
# ----------------------------------------------------------------- #
def chart_monthly(db: ComexDB, f: Filters):
    df = db.monthly(f)
    if df.empty:
        st.info("Sin datos para los filtros seleccionados.")
        return
    df = df.copy()
    df["mes_nom"] = df["CO_MES"].map(NOMBRES_MESES)
    df["serie"] = df["flujo"].str.upper() + " " + df["CO_ANO"].astype(str)

    st.subheader("📈 Evolución mensual (FOB)")
    fig = px.line(
        df, x="mes_nom", y="fob", color="serie", markers=True,
        labels={"fob": "FOB (US$)", "serie": ""},
        category_orders={"mes_nom": MES_LABEL},
        color_discrete_map=_SERIE_COLORS,
    )
    fig.update_layout(hovermode="x unified", height=380, legend_title_text="")
    fig.update_traces(hovertemplate="US$ %{y:,.0f}<extra></extra>")
    st.plotly_chart(fig, width="stretch")
    _yoy_badge(df)


def _yoy_badge(df):
    for fl in df["flujo"].unique():
        sub = df[df["flujo"] == fl]
        piv = sub.pivot_table(index="CO_MES", columns="CO_ANO", values="fob")
        if piv.shape[1] == 2 and len(piv) > 0:
            common = piv.dropna()
            if not common.empty:
                yrs = sorted(piv.columns)
                tot_prev = common[yrs[0]].sum()
                tot_curr = common[yrs[1]].sum()
                if tot_prev:
                    var = (tot_curr - tot_prev) / tot_prev * 100
                    lbl = FLOW_LABELS.get(fl, fl)
                    st.caption(
                        f"**{lbl} {yrs[0]}→{yrs[1]}** "
                        f"(meses: {', '.join(NOMBRES_MESES[m] for m in common.index)}): "
                        f"**{var:+.1f}%**"
                    )


def chart_balance(db: ComexDB, f: Filters):
    bal = db.trade_balance(f)
    if bal.empty:
        return
    bal = bal.copy()
    bal["mes_nom"] = bal["CO_MES"].map(NOMBRES_MESES)
    bal["año"] = bal["CO_ANO"].astype(str)
    st.subheader("⚖️ Balanza comercial mensual")
    fig = go.Figure()
    for yr in sorted(bal["año"].unique()):
        sub = bal[bal["año"] == yr]
        fig.add_trace(go.Bar(
            x=sub["mes_nom"], y=sub["saldo"], name=f"Saldo {yr}",
            marker_color="#3b82f6",
        ))
    fig.update_layout(barmode="group", height=340,
                      xaxis_title="Mes", yaxis_title="Saldo (US$)")
    st.plotly_chart(fig, width="stretch")
    with st.expander("Ver Exportaciones vs Importaciones mensuales"):
        fig2 = go.Figure()
        for col, color in (("exp", PALETTE["exp"]), ("imp", PALETTE["imp"])):
            fig2.add_trace(go.Bar(x=bal["mes_nom"], y=bal[col],
                                  name=col.upper(), marker_color=color))
        fig2.update_layout(barmode="group", height=320)
        st.plotly_chart(fig2, width="stretch")


def chart_top(db: ComexDB, f: Filters, dim: str, title: str, n: int = 15):
    df = db.top(f, dim, n)
    if df.empty:
        st.info(f"Sin datos para '{title}'.")
        return
    df = df.copy()
    df["fob_MM"] = df["fob"] / 1e9
    df["flujo_lbl"] = df["flujo"].map(FLOW_LABELS)
    order = df.groupby("nombre")["fob_MM"].sum().sort_values(ascending=True).index
    df["nombre"] = pd.Categorical(df["nombre"], categories=order, ordered=True)
    st.subheader(title)
    fig = px.bar(
        df, x="fob_MM", y="nombre", color="flujo_lbl", orientation="h",
        barmode="group",
        labels={"fob_MM": "FOB (mil mill. US$)", "nombre": "", "flujo_lbl": ""},
        color_discrete_map={FLOW_LABELS["exp"]: PALETTE["exp"],
                            FLOW_LABELS["imp"]: PALETTE["imp"]},
    )
    n_items = df["nombre"].nunique()
    fig.update_layout(height=max(320, n_items * 28 + 80),
                      showlegend=len(f.flows) > 1)
    st.plotly_chart(fig, width="stretch")


def chart_pie_fat(db: ComexDB, f: Filters):
    df = db.top(f, "fat_agreg", 10)
    if df.empty:
        return
    df = df.groupby("nombre", as_index=False)["fob"].sum()
    df["fob_MM"] = df["fob"] / 1e9
    st.subheader("🥧 Composición por factor agregado")
    fig = px.pie(df, values="fob_MM", names="nombre", hole=0.4)
    fig.update_layout(height=340)
    st.plotly_chart(fig, width="stretch")


def chart_fronteras(db: ComexDB, f: Filters):
    if not f.urfs:
        st.info(
            "Selecciona una o más fronteras en el panel lateral "
            "(🛃 Frontera) para ver el detalle."
        )
        return

    periodo = st.radio(
        "Período",
        options=list(PERIOD_LABELS.keys()),
        format_func=lambda x: PERIOD_LABELS[x],
        horizontal=True,
    )

    latest = db.latest_period()
    months_back = PERIOD_MONTHS[periodo]
    ym_start, ym_end = _compute_ym_range(latest, months_back)
    st.caption(
        f"Datos de **{_ym_label(ym_start)}** a **{_ym_label(ym_end)}** "
        f"(fuente: Comex Stat MDIC)."
    )

    f_period = replace(f, ym_start=ym_start, ym_end=ym_end)
    df = db.top_products_by_urf(f_period, 10)
    if df.empty:
        st.warning("Sin datos para las fronteras y período seleccionados.")
        return

    urf_codes = list(dict.fromkeys(
        zip(df["codigo"], df["frontera"])
    ))

    if len(urf_codes) <= 3:
        cols = st.columns(len(urf_codes))
        for i, (code, name) in enumerate(urf_codes):
            with cols[i]:
                _urf_top_chart(df[df["codigo"] == code], name)
    else:
        for code, name in urf_codes:
            with st.expander(name, expanded=True):
                _urf_top_chart(df[df["codigo"] == code], name)

    st.divider()
    st.subheader("📋 Resumen comparativo")
    summary = df.groupby(["frontera", "flujo"]).agg(
        toneladas=("toneladas", "sum"),
        fob=("fob", "sum"),
        productos=("ncm", "nunique"),
    ).reset_index()
    summary["Toneladas"] = summary["toneladas"].map(
        lambda v: f"{int(v):,}".replace(",", "."))
    summary["FOB"] = summary["fob"].map(fmt_money)
    summary["Flujo"] = summary["flujo"].map(FLOW_LABELS)
    st.dataframe(
        summary[["frontera", "Flujo", "Toneladas", "FOB", "productos"]],
        width="stretch", hide_index=True,
    )
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Descargar CSV", csv,
        file_name="comexstat_fronteras_top10.csv", mime="text/csv",
    )


def _urf_top_chart(sub: pd.DataFrame, name: str):
    st.markdown(f"**🛃 {name}**")
    sub = sub.copy()
    sub["label"] = sub["producto"].str[:45]
    sub = sub.sort_values("toneladas", ascending=True)
    fig = px.bar(
        sub, x="toneladas", y="label", color="flujo",
        orientation="h",
        color_discrete_map=PALETTE,
        labels={"toneladas": "Toneladas", "label": "", "flujo": ""},
    )
    fig.update_layout(
        height=max(280, len(sub) * 24 + 60),
        showlegend=len(sub["flujo"].unique()) > 1,
        margin=dict(l=0, r=10, t=0, b=0),
    )
    st.plotly_chart(fig, width="stretch")


def chart_municipios(db: ComexDB, f: Filters):
    if f.urfs or f.ncms:
        st.warning(
            "⚠️ Los filtros de **frontera (URF)** y **NCM** no aplican a la base "
            "municipal (que usa SH4). Limpia esos filtros para ver municipios."
        )
        return
    df = db.top_municipios(f, 15)
    if df.empty:
        st.info("Sin datos municipales para los filtros.")
        return
    df = df.copy()
    df["fob_MM"] = df["fob"] / 1e9
    df["flujo_lbl"] = df["flujo"].map(FLOW_LABELS)
    df["label"] = df["municipio"] + " (" + df["SG_UF"] + ")"
    order = df.groupby("label")["fob_MM"].sum().sort_values(ascending=True).index
    df["label"] = pd.Categorical(df["label"], categories=order, ordered=True)
    st.subheader("🏙️ Top municipios (domicilio de la empresa)")
    fig = px.bar(
        df, x="fob_MM", y="label", color="flujo_lbl", orientation="h",
        barmode="group",
        labels={"fob_MM": "FOB (mil mill. US$)", "label": "", "flujo_lbl": ""},
        color_discrete_map={FLOW_LABELS["exp"]: PALETTE["exp"],
                            FLOW_LABELS["imp"]: PALETTE["imp"]},
    )
    n_items = df["label"].nunique()
    fig.update_layout(height=max(320, n_items * 28 + 80),
                      showlegend=len(f.flows) > 1)
    st.plotly_chart(fig, width="stretch")


def render_camiones(db: ComexDB, f: Filters):
    st.subheader("🚛 Camiones por NCM")
    st.caption(
        "_Estimación: toneladas ÷ 15 (peso medio referencial por camión)._"
    )
    df = db.camiones_por_ncm(f)
    if df is None or df.empty:
        st.info("Sin datos para el cálculo de camiones con los filtros actuales.")
        return
    df = df.copy()
    df["Flujo"] = df["flujo"].map({"exp": "EXP", "imp": "IMP"})
    df["Toneladas"] = df["toneladas"].map(lambda v: f"{int(v):,}".replace(",", "."))
    df["FOB"] = df["fob_total"].map(fmt_money)
    df["Camiones"] = df["camiones"].map(
        lambda v: f"{int(v):,}".replace(",", ".") if pd.notna(v) else "—"
    )
    df["Ops"] = df["n_ops"].map(fmt_int)
    show = df[["Flujo", "ncm", "producto", "Toneladas", "FOB", "Camiones", "Ops"]]
    st.dataframe(show, width="stretch", hide_index=True)
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Descargar CSV", csv, file_name="comexstat_camiones.csv",
        mime="text/csv",
    )


def render_detail(db: ComexDB, f: Filters):
    st.subheader("📋 Detalle de operaciones (top por FOB)")
    limit = st.slider("Número de registros", 50, 2000, 500, step=50)
    df = db.detail(f, limit)
    if df is None or df.empty:
        st.info("Sin datos.")
        return
    df = df.copy()
    df["FOB (US$)"] = df["fob_usd"].round(2)
    df["KG"] = df["kg"].round(0)
    df["Flujo"] = df["flujo"].map({"exp": "EXP", "imp": "IMP"})
    df["Mes"] = df["mes"].map(NOMBRES_MESES)
    show = df[["Flujo", "ano", "Mes", "pais", "uf", "frontera",
               "ncm", "producto", "via", "KG", "FOB (US$)"]]
    st.dataframe(show, width="stretch", hide_index=True)
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Descargar CSV", csv, file_name="comexstat_detalle.csv",
        mime="text/csv",
    )


# ----------------------------------------------------------------- #
# App principal
# ----------------------------------------------------------------- #
def main():
    st.set_page_config(
        page_title="ComexStat · Comercio Exterior Brasil",
        page_icon="🌍", layout="wide",
    )

    with st.spinner("Cargando base de datos (optimizando consultas)..."):
        db = get_db()
    opt = get_options(db)

    st.title("🌍 ComexStat · Comercio Exterior de Brasil")
    st.caption(
        f"Datos: base detallada Comex Stat (MDIC) · Última actualización: "
        f"{data_freshness()} · Período: {min(opt['years'])}–{max(opt['years'])}"
    )

    f = sidebar(opt)

    flows_lbl = " + ".join(FLOW_LABELS[fl] for fl in f.flows)
    st.markdown(f"**Operación:** {flows_lbl} · **Filtros:** {active_filters_badges(f)}")
    st.divider()

    if not f.years:
        st.warning("Selecciona al menos un año en el panel lateral.")
        return

    render_kpis(db, f)

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "📊 Panel general", "📦 Productos", "🌎 Mercados",
        "🛃 Fronteras", "🏙️ Municipios", "📋 Detalle",
    ])

    with tab1:
        chart_monthly(db, f)
        chart_balance(db, f)

    with tab2:
        c1, c2 = st.columns([3, 2])
        with c1:
            chart_top(db, f, "sh4", "Top posiciones de producto (SH4)", n=15)
        with c2:
            chart_pie_fat(db, f)
        chart_top(db, f, "seccion", "Por sección del Sistema Armonizado", n=21)

    with tab3:
        c1, c2 = st.columns(2)
        with c1:
            chart_top(db, f, "pais", "Top países", n=15)
        with c2:
            chart_top(db, f, "uf", "Top estados (UF)", n=27)

    with tab4:
        chart_fronteras(db, f)

    with tab5:
        st.markdown(
            "_Base por municipio: domicilio fiscal de la empresa "
            "(agrupado por SH4)._"
        )
        chart_municipios(db, f)

    with tab6:
        render_camiones(db, f)
        st.divider()
        render_detail(db, f)


if __name__ == "__main__":
    main()
