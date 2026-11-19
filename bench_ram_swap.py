#!/usr/bin/env python3
"""Benchmark d'une version de ram_swap : coût d'itération, latence, démarrage."""

from __future__ import annotations

import argparse
import importlib.util
import statistics
import sys
import subprocess
import sys
import time
from pathlib import Path

ITERATIONS = 300
INVOCATIONS = 15


def charger_module(chemin: Path):
    spec = importlib.util.spec_from_file_location("ram_swap_bench", chemin)
    if spec is None or spec.loader is None:
        raise SystemExit(f"module illisible : {chemin}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def quantiles(valeurs: list[float]) -> tuple[float, float, float]:
    tri = sorted(valeurs)
    return (
        statistics.median(tri),
        tri[int(len(tri) * 0.95) - 1],
        tri[-1],
    )


def bench_iterations(module, proc: Path) -> list[float]:
    module.read_sample(proc)
    durees = []
    for _ in range(ITERATIONS):
        debut = time.perf_counter()
        echantillon = module.read_sample(proc)
        module.assess(echantillon)
        durees.append((time.perf_counter() - debut) * 1e6)
    return durees


def bench_parse(module, proc: Path) -> dict[str, float]:
    textes = {
        "meminfo": (proc / "meminfo").read_text(),
        "pressure": (proc / "pressure" / "memory").read_text(),
        "vmstat": (proc / "vmstat").read_text(),
    }
    fonctions = {
        "meminfo": module.parse_meminfo,
        "pressure": module.parse_pressure,
        "vmstat": module.parse_vmstat,
    }
    resultats = {}
    for nom, texte in textes.items():
        debut = time.perf_counter()
        for _ in range(ITERATIONS):
            fonctions[nom](texte)
        resultats[nom] = (time.perf_counter() - debut) / ITERATIONS * 1e6
    return resultats


def latence_subprocess(commande: list[str]) -> list[float]:
    durees = []
    for _ in range(INVOCATIONS):
        debut = time.perf_counter()
        subprocess.run(commande, capture_output=True, check=True)
        durees.append((time.perf_counter() - debut) * 1000)
    return durees


def ligne(nom: str, valeurs: list[float], unite: str) -> str:
    mediane, p95, maxi = quantiles(valeurs)
    return f"{nom:<28} médiane {mediane:9.2f} {unite}   p95 {p95:9.2f}   max {maxi:9.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark d'une version de ram_swap.")
    parser.add_argument(
        "module",
        nargs="?",
        type=Path,
        default=Path(__file__).parent / "ram_swap.py",
        help="chemin du ram_swap.py à mesurer (défaut : celui du dossier)",
    )
    parser.add_argument("--proc", type=Path, default=Path("/proc"))
    args = parser.parse_args(argv)

    chemin = args.module.resolve()
    module = charger_module(chemin)
    python = sys.executable

    print(f"module            {chemin}")
    print(f"itérations        {ITERATIONS}   invocations {INVOCATIONS}")
    print()
    print(ligne("itération lecture+décision", bench_iterations(module, args.proc), "µs"))
    for nom, µs in bench_parse(module, args.proc).items():
        print(f"  parse {nom:<21} moyenne {µs:9.2f} µs")
    print()
    e2e = latence_subprocess([python, str(chemin), "--dry-run", "--json"])
    print(ligne("invocation --dry-run --json", e2e, "ms"))
    base = latence_subprocess([python, "-c", "pass"])
    print(ligne("démarrage python nu", base, "ms"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
