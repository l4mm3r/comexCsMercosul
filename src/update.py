"""
Orquestador: ejecuta la actualización completa de datos Comex Stat.

Secuencia:  descarga  ->  transformación  ->  validación

Pensado para ejecución manual o automática (cron). Cada etapa se ejecuta
solo si la anterior tuvo éxito. Es idempotente: re-ejecutar no re-descarga
archivos sin cambios (gracias a la caché por Last-Modified en download).

Uso:
  python -m src.update                  # actualización completa
  python -m src.update --datasets ncm   # solo NCM (datos + dims necesarias)
  python -m src.update --force          # forzar re-descarga ignorando caché
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from . import download, transform, validate
from .config import load_config

log = logging.getLogger("comexstat.update")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Actualización completa Comex Stat.")
    ap.add_argument("--datasets", default="ncm,mun,tabelas",
                    help="Grupos a descargar (default: todos)")
    ap.add_argument("--force", action="store_true", help="Ignorar caché local")
    ap.add_argument("--skip-validate", action="store_true",
                    help="Omitir la validación final")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )
    cfg = load_config()
    results: list[tuple[str, int, float]] = []

    def run(name, fn, argv_list):
        log.info("\n" + "#" * 70)
        log.info("# ETAPA: %s", name.upper())
        log.info("#" * 70)
        t0 = time.time()
        rc = fn.main(argv_list)
        dt = time.time() - t0
        results.append((name, rc, dt))
        return rc

    # 1) Descarga
    dl_argv = ["--datasets", args.datasets]
    if args.force:
        dl_argv.append("--force")
    rc = run("descarga", download, dl_argv)
    if rc != 0:
        log.error("Descarga falló (errores en archivos). Abortando.")
        return _summary(results)

    # 2) Transformación (siempre dims + facts; dims son baratas)
    tf_argv = []
    if args.force:
        tf_argv.append("--force")  # transform no usa --force, se ignora
    rc = run("transformación", transform, [])
    if rc != 0:
        log.error("Transformación falló. Abortando.")
        return _summary(results)

    # 3) Validación
    if not args.skip_validate:
        rc = run("validación", validate, [])

    return _summary(results)


def _summary(results: list[tuple[str, int, float]]) -> int:
    log.info("\n" + "=" * 70)
    log.info("RESUMEN DE ACTUALIZACIÓN")
    log.info("=" * 70)
    overall = 0
    for name, rc, dt in results:
        status = "✓ OK" if rc == 0 else "✗ FALLÓ"
        log.info("  %-16s %s   (%.1fs)", name, status, dt)
        overall = max(overall, rc)
    log.info("-" * 70)
    log.info("Resultado global: %s", "✓ ÉXITO" if overall == 0 else "✗ CON ERRORES")
    return overall


if __name__ == "__main__":
    sys.exit(main())
