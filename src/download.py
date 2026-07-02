"""
Descarga de la base de datos Comex Stat (NCM + Municipio + tablas de correlación).

Características:
  - Idempotente: omite descarga si el archivo local está actualizado (Last-Modified).
  - Fallback SSL: el host balanca.economia.gov.br no envía el cert intermedio;
    tras fallar la verificación reintenta con verify=False (datos públicos verificados).
  - Reintentos con backoff exponencial y descarga por chunks.
  - CLI flexible: --datasets, --years, --force.

Uso:
  python -m src.download                      # todo (NCM + MUN + tabelas), años del config
  python -m src.download --datasets ncm,mun    # solo bases de datos
  python -m src.download --datasets tabelas    # solo tablas de correlación
  python -m src.download --years 2026 --force  # forzar re-descarga de 2026
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import urllib3
from requests.adapters import HTTPAdapter

from .config import ensure_dirs, load_config, project_path

# El host de Comex Stat no envía el cert intermedio; usamos fallback verify=False
# de forma intencional, así que silenciamos esa advertencia concreta.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("comexstat.download")

# Reintentos a nivel de transporte (conexiones caídas, 5xx, etc.)
RETRY_STATUSES = (429, 500, 502, 503, 504)


@dataclass
class RemoteFile:
    """Representa un archivo remoto a descargar."""
    url: str
    dest: Path
    label: str            # descripción corta para logs
    group: str            # 'ncm' | 'mun' | 'tabelas' | 'validation'


class ComexDownloader:
    """Gestiona la descarga robusta de archivos de Comex Stat."""

    def __init__(self, cfg: dict, force: bool = False):
        self.cfg = cfg
        self.force = force
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg["download"]["user_agent"]
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        self.session.mount("https://", adapter)
        self.timeout = cfg["download"]["timeout_seconds"]
        self.max_retries = cfg["download"]["max_retries"]
        self.chunk_size = cfg["download"]["chunk_size"]
        self.ssl_fallback = cfg["download"].get("ssl_fallback_insecure", True)

    # ----------------------------------------------------------------- #
    # Núcleo de descarga
    # ----------------------------------------------------------------- #
    def _head(self, url: str, verify: bool) -> requests.Response:
        return self.session.head(
            url, timeout=self.timeout, allow_redirects=True, verify=verify
        )

    def _get(self, url: str, verify: bool, stream: bool = True) -> requests.Response:
        return self.session.get(
            url, timeout=self.timeout, stream=stream, allow_redirects=True, verify=verify
        )

    def _remote_meta(self, url: str) -> dict | None:
        """HEAD con fallback SSL para obtener Last-Modified / Content-Length."""
        for verify in (True, False):
            for attempt in range(self.max_retries):
                try:
                    r = self._head(url, verify=verify)
                    if r.status_code == 200:
                        return {
                            "verify": verify,
                            "last_modified": r.headers.get("Last-Modified", ""),
                            "content_length": r.headers.get("Content-Length", ""),
                        }
                    log.warning("  HEAD %s -> HTTP %s", url, r.status_code)
                    return None
                except requests.exceptions.SSLError:
                    if verify and self.ssl_fallback:
                        log.debug("  SSL fail (verify=True), reintentando insecure")
                        break  # pasa a verify=False
                    log.error("  SSL error incluso con verify=False: %s", url)
                    return None
                except requests.RequestException as e:
                    if attempt < self.max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    log.error("  HEAD error %s: %s", url, e)
                    return None
        return None

    def _download_file(self, url: str, dest: Path, verify: bool,
                       expected_size: int = 0) -> bool:
        """Descarga por chunks con REANUDACIÓN (HTTP Range) y reintentos.

        Si el servidor corta la conexión a mitad de un archivo grande, reanuda
        desde el último byte recibido usando el header Range, evitando reiniciar.
        """
        tmp = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(self.max_retries):
            # Punto de reanudación = bytes ya en el .part
            offset = tmp.stat().st_size if tmp.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
            try:
                r = self.session.get(
                    url, stream=True, timeout=self.timeout, headers=headers,
                    allow_redirects=True, verify=verify,
                )
                # 416 = el rango ya cubre todo el archivo -> ya está completo
                if r.status_code == 416:
                    break
                r.raise_for_status()

                if r.status_code == 206:        # Partial Content: append
                    mode = "ab"
                else:                           # 200 OK: empezar de cero
                    offset = 0
                    mode = "wb"
                # Content-Length en 206 = bytes restantes; en 200 = total
                clen = int(r.headers.get("Content-Length", 0) or 0)
                total_expected = (offset + clen) if clen else expected_size
                written = offset
                with open(tmp, mode) as f:
                    for chunk in r.iter_content(self.chunk_size):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                # ¿Completo?
                if total_expected and written >= total_expected:
                    break
                if not total_expected:
                    break  # sin tamaño conocido; stream cerrado sin error
                # incompleto sin excepción: reintentar reanudando
                log.warning("  intento %d/%d: incompleto %d/%d bytes",
                            attempt + 1, self.max_retries, written, total_expected)
            except requests.RequestException as e:
                got = tmp.stat().st_size if tmp.exists() else 0
                log.warning("  intento %d/%d fallido en byte %d (%s)",
                            attempt + 1, self.max_retries, got, e)
            if attempt < self.max_retries - 1:
                time.sleep(min(2 ** attempt, 15))

        # Verificación final
        if not tmp.exists():
            return False
        final = tmp.stat().st_size
        if expected_size and final != expected_size:
            log.error("  tamaño final %d != esperado %d (%s)", final, expected_size, dest.name)
            return False
        # SHA-256 sobre el archivo completo (válido aunque haya habido reanudación)
        sha = hashlib.sha256()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                sha.update(chunk)
        tmp.replace(dest)
        return self._save_meta(dest, {
            "last_modified": "",  # se rellena abajo desde el HEAD remoto
            "content_length": str(final),
            "sha256": sha.hexdigest(),
        })

    # ----------------------------------------------------------------- #
    # Idempotencia
    # ----------------------------------------------------------------- #
    def _meta_path(self, dest: Path) -> Path:
        return dest.with_suffix(dest.suffix + ".meta.json")

    def _load_meta(self, dest: Path) -> dict | None:
        mp = self._meta_path(dest)
        if mp.exists():
            try:
                return json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def _save_meta(self, dest: Path, meta: dict) -> bool:
        try:
            self._meta_path(dest).write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )
            return True
        except Exception as e:
            log.warning("  no se pudo guardar meta de %s: %s", dest.name, e)
            return True  # el archivo se bajó igual

    def _is_up_to_date(self, rf: RemoteFile, remote: dict) -> bool:
        """True si el local coincide con el remoto (Last-Modified)."""
        if self.force:
            return False
        if not rf.dest.exists():
            return False
        local = self._load_meta(rf.dest)
        if not local:
            return False
        return bool(
            remote.get("last_modified")
            and local.get("last_modified") == remote["last_modified"]
        )

    # ----------------------------------------------------------------- #
    # API pública
    # ----------------------------------------------------------------- #
    def fetch(self, rf: RemoteFile) -> str:
        """Descarga un archivo si es necesario. Devuelve un estado."""
        remote = self._remote_meta(rf.url)
        if remote is None:
            return "ERROR"
        if self._is_up_to_date(rf, remote):
            mb = rf.dest.stat().st_size / 1e6
            log.info("  ✓ CACHE  %s  (%.1f MB)  %s", rf.label, mb, rf.dest.name)
            return "CACHE"

        verify = remote["verify"]
        if verify is False:
            log.warning("  descargando SIN verificar SSL (cadena incompleta del host)")
        expected = int(remote.get("content_length") or 0)
        mb = float(expected) / 1e6
        log.info("  ↓ DESCARGA  %s  (~%.1f MB)  %s", rf.label, mb, rf.dest.name)
        rf.dest.parent.mkdir(parents=True, exist_ok=True)
        ok = self._download_file(rf.url, rf.dest, verify=verify, expected_size=expected)
        if ok:
            # rellenar last_modified remoto para idempotencia futura
            local = self._load_meta(rf.dest) or {}
            if remote.get("last_modified"):
                local["last_modified"] = remote["last_modified"]
                self._save_meta(rf.dest, local)
            log.info("    OK  %s (%.1f MB)", rf.dest.name, rf.dest.stat().st_size / 1e6)
            return "DOWNLOADED"
        log.error("    FALLO  %s", rf.dest.name)
        return "ERROR"

    def fetch_many(self, files: list[RemoteFile]) -> dict:
        results = {"DOWNLOADED": 0, "CACHE": 0, "ERROR": 0}
        for rf in files:
            results[self.fetch(rf)] += 1
        return results


# --------------------------------------------------------------------- #
# Construcción de la lista de archivos a partir del config
# --------------------------------------------------------------------- #
def build_file_list(cfg: dict, datasets: list[str], years: list[int]) -> list[RemoteFile]:
    files: list[RemoteFile] = []
    base_urls = cfg["base_urls"]
    path_map = {
        "ncm": cfg["paths"]["raw_ncm"],
        "mun": cfg["paths"]["raw_mun"],
    }

    # Bases de datos (NCM / MUN)
    for ds_filter in ("ncm", "mun"):
        if ds_filter not in datasets:
            continue
        for key, ds in cfg["datasets"].items():
            if not key.startswith(ds_filter):
                continue
            base = base_urls[ds["source"]]
            dest_dir = project_path(path_map[ds["source"]])
            for year in years:
                url = ds["template"].format(base=base, year=year)
                dest = dest_dir / Path(url).name
                flow = "Exportación" if ds["flow"] == "exp" else "Importación"
                files.append(RemoteFile(
                    url=url, dest=dest,
                    label=f"{ds['source'].upper()} {flow} {year}",
                    group=ds["source"],
                ))

    # Tablas de correlación
    if "tabelas" in datasets:
        base = base_urls["tabelas"]
        dest_dir = project_path(cfg["paths"]["raw_tabelas"])
        for name, fname in cfg["tabelas"].items():
            files.append(RemoteFile(
                url=f"{base}/{fname}", dest=dest_dir / fname,
                label=f"Tabla {name}", group="tabelas",
            ))

    return files


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Descarga datos Comex Stat.")
    ap.add_argument(
        "--datasets", default="ncm,mun,tabelas",
        help="Grupos a descargar: ncm,mun,tabelas (default: todos)",
    )
    ap.add_argument(
        "--years", default=None,
        help="Años (override del config), ej: 2025,2026",
    )
    ap.add_argument("--force", action="store_true", help="Ignorar caché local")
    ap.add_argument("-v", "--verbose", action="store_true", help="Logging DEBUG")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    cfg = load_config()
    ensure_dirs(cfg)
    years = (
        [int(y) for y in args.years.split(",")] if args.years else cfg["years"]
    )
    datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]

    files = build_file_list(cfg, datasets, years)
    if not files:
        log.warning("No hay archivos para descargar con esos filtros.")
        return 0

    log.info("=" * 64)
    log.info("ComexStat - Descarga de datos")
    log.info("  Años:     %s", years)
    log.info("  Grupos:   %s", datasets)
    log.info("  Archivos: %d", len(files))
    log.info("=" * 64)

    dl = ComexDownloader(cfg, force=args.force)
    results = dl.fetch_many(files)

    log.info("-" * 64)
    log.info(
        "Resumen: %d descargados, %d en caché, %d errores",
        results["DOWNLOADED"], results["CACHE"], results["ERROR"],
    )
    return 0 if results["ERROR"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
