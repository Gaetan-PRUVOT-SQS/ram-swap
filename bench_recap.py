#!/usr/bin/env python3
"""Synthèse des runs du benchmark système : moyennes par configuration."""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

DOSSIER = Path(__file__).parent / "resultats"
CONFIGS = ("solution", "natif")


def charger(chemin: Path) -> dict:
    brut = json.loads(chemin.read_text())
    passes = brut["churn"]
    refault = brut["refault_4k"]
    return {
        "churn_s": statistics.median(p["duree_s"] for p in passes),
        "debit_mio_s": statistics.median(p["debit_mio_s"] for p in passes),
        "psi_full_s": statistics.median(p["psi_full_ms"] for p in passes) / 1000,
        "psi_some_s": statistics.median(p["psi_some_ms"] for p in passes) / 1000,
        "refault_median_us": refault["latence_median_us"],
        "refault_p95_us": refault["latence_p95_us"],
        "montee_s": brut["montee_s"],
    }


def main(argv: list[str] | None = None) -> int:
    runs: dict[str, list[dict]] = {c: [] for c in CONFIGS}
    for chemin in sorted(DOSSIER.glob("*.json")):
        for config in CONFIGS:
            if chemin.name.startswith(config):
                runs[config].append(charger(chemin))
                break

    if not any(runs.values()):
        print("aucun run trouvé dans", DOSSIER)
        return 1

    cles = list(next(iter(runs.values()))[0].keys())
    print(f"{'métrique (médiane des passes)':<28} {'solution':>14} {'natif':>14} {'écart':>10}")
    for cle in cles:
        s = [r[cle] for r in runs["solution"]]
        n = [r[cle] for r in runs["natif"]]
        ms, mn = statistics.median(s), statistics.median(n)
        ecart = (mn - ms) / ms * 100 if ms else 0.0
        unite = "us" if cle.endswith("_us") else "Mio/s" if cle == "debit_mio_s" else "s"
        print(
            f"{cle + f' [{len(s)}/{len(n)} runs]':<28} {ms:>12.2f} {unite} {mn:>10.2f} {unite} {ecart:>+9.1f} %"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
