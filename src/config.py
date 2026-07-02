"""Carga de configuración y rutas del proyecto."""
from __future__ import annotations

from pathlib import Path

import yaml

# Raíz del proyecto: directorio padre de src/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    """Carga config.yaml en un dict."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def project_path(*parts: str) -> Path:
    """Devuelve una ruta absoluta dentro del proyecto."""
    return PROJECT_ROOT.joinpath(*parts)


def ensure_dirs(cfg: dict) -> None:
    """Crea los directorios de datos si no existen."""
    for raw_key in ("raw_ncm", "raw_mun", "raw_tabelas"):
        project_path(cfg["paths"][raw_key]).mkdir(parents=True, exist_ok=True)
    project_path(cfg["paths"]["processed"]).mkdir(parents=True, exist_ok=True)
