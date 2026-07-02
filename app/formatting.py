"""Helpers de formato para el dashboard."""
from __future__ import annotations

NOMBRES_MESES = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}


def fmt_money(v: float, dec: int = 1) -> str:
    """Formatea USD: 3.4e9 -> '3,4 B US$', 2.1e6 -> '2,1 M US$'."""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"{v/1e9:,.{dec}f} B US$".replace(",", "X").replace(".", ",").replace("X", ".")
    if a >= 1e6:
        return f"{v/1e6:,.{dec}f} M US$".replace(",", "X").replace(".", ",").replace("X", ".")
    if a >= 1e3:
        return f"{v/1e3:,.{dec}f} k US$".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{v:,.0f} US$".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_weight(v: float) -> str:
    """Formatea kg en toneladas o miles de toneladas."""
    if v is None:
        return "—"
    ton = v / 1e3
    if ton >= 1e6:
        return f"{ton/1e6:,.1f} M t".replace(",", "X").replace(".", ",").replace("X", ".")
    if ton >= 1e3:
        return f"{ton/1e3:,.1f} k t".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{ton:,.0f} t".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_price(v: float) -> str:
    """US$ por kg."""
    if not v:
        return "—"
    return f"{v:,.2f} US$/kg".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_int(v: float) -> str:
    if v is None:
        return "—"
    return f"{int(v):,}".replace(",", ".")
