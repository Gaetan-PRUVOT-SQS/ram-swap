#!/usr/bin/env python3
"""Benchmark d'usage réel : poste mémoire contraint, latences perçues.

Des ouvriers occupent la RAM comme des applications ouvertes : une zone
chaude réécrite régulièrement, une zone froide posée au démarrage puis
abandonnée. Pendant ce temps, le harnais mesure ce qu'un utilisateur
ressent : latence de lancement d'un outil, sauvegarde d'un petit fichier
(fsync), débit de lecture disque. Mesure des deltas de swap et de PSI.
"""

from __future__ import annotations

import argparse
import json
import mmap
import multiprocessing
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ram_swap

PROC = Path("/proc")
CHUNK = 8 * 1024 * 1024
ZONE_CHAUTE_MIO = 50
FICHIER_FSYNC = Path(__file__).parent / "resultats" / "fsync.tmp"
DOSSIER_LECTURE = Path("/usr/lib/python3.13")


def ouvrier(taille: int, arret: threading.Event) -> None:
    zeros = bytes(CHUNK)
    mm = mmap.mmap(-1, taille)
    for o in range(0, taille, CHUNK):
        mm[o : o + CHUNK] = zeros[: min(CHUNK, taille - o)]
    chaud = ZONE_CHAUTE_MIO * 1024 * 1024
    while not arret.is_set():
        echeance = time.monotonic() + 0.15
        for o in range(0, chaud, CHUNK):
            mm[o : o + CHUNK] = zeros[: min(CHUNK, chaud - o)]
        arret.wait(max(0.02, echeance - time.monotonic()))
    mm.close()


def lancer_ouvriers(nb: int, mio: int) -> tuple[list[multiprocessing.Process], threading.Event]:
    arret = threading.Event()
    taille = mio * 1024 * 1024
    processus = []
    for _ in range(nb):
        p = multiprocessing.Process(target=ouvrier, args=(taille, arret), daemon=True)
        p.start()
        processus.append(p)
    return processus, arret


def latence_spawn() -> float:
    debut = time.perf_counter()
    subprocess.run(["python3", "-c", "pass"], capture_output=True, check=True)
    return (time.perf_counter() - debut) * 1000


def latence_fsync() -> float:
    debut = time.perf_counter()
    donnees = bytes(64 * 1024)
    with open(FICHIER_FSYNC, "wb") as f:
        f.write(donnees)
        f.flush()
        os.fsync(f.fileno())
    with open(FICHIER_FSYNC, "rb") as f:
        f.read()
    return (time.perf_counter() - debut) * 1000


def debit_lecture(limite_mio: int) -> tuple[float, float]:
    debut = time.perf_counter()
    lu = 0
    cible = limite_mio * 1024 * 1024
    for chemin in sorted(DOSSIER_LECTURE.rglob("*")):
        if chemin.is_file() and lu < cible:
            with open(chemin, "rb") as f:
                while bloc := f.read(CHUNK):
                    lu += len(bloc)
        if lu >= cible:
            break
    duree = time.perf_counter() - debut
    return duree, lu / 1024**2 / duree if duree > 0 else 0.0


class Echantillonneur:
    def __init__(self) -> None:
        self.arret = threading.Event()
        self.releves: list[dict[str, float]] = []
        self._fil = threading.Thread(target=self._boucle, daemon=True)

    def _boucle(self) -> None:
        t0 = time.monotonic()
        while not self.arret.is_set():
            mem = ram_swap.parse_meminfo((PROC / "meminfo").read_text())
            psi = ram_swap.parse_pressure((PROC / "pressure" / "memory").read_text())
            self.releves.append(
                {
                    "t": round(time.monotonic() - t0, 1),
                    "swappiness": int((PROC / "sys/vm/swappiness").read_text()),
                    "dispo_pct": round(
                        100 * mem.get("MemAvailable", 0) / max(1, mem.get("MemTotal", 1)), 1
                    ),
                    "psi_some_avg10": psi.get("some_avg10", 0.0),
                    "psi_full_avg10": psi.get("full_avg10", 0.0),
                }
            )
            self.arret.wait(1.0)

    def __enter__(self) -> "Echantillonneur":
        self._fil.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.arret.set()
        self._fil.join(timeout=2)


def quantiles(valeurs: list[float]) -> dict[str, float]:
    tri = sorted(valeurs)
    return {
        "mediane": statistics.median(tri),
        "p95": tri[max(0, int(len(tri) * 0.95) - 1)],
        "max": tri[-1],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark d'usage réel sous configuration donnée.")
    parser.add_argument("--etiquette", required=True)
    parser.add_argument("--ouvriers", type=int, default=2)
    parser.add_argument("--mio-par-ouvrier", type=int, default=600)
    parser.add_argument("--echantillons", type=int, default=50)
    parser.add_argument("--periode", type=float, default=2.0)
    parser.add_argument("--dossier", type=Path, default=Path(__file__).parent / "resultats")
    args = parser.parse_args()

    FICHIER_FSYNC.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["sudo", "-n", "sysctl", "-w", "vm.drop_caches=3"], check=True, capture_output=True)
    time.sleep(3)
    c0 = {
        "vm": ram_swap.parse_vmstat((PROC / "vmstat").read_text()),
        "psi": ram_swap.parse_pressure((PROC / "pressure" / "memory").read_text()),
        "sysctl": sysctl_courant(),
    }

    processus, arret = lancer_ouvriers(args.ouvriers, args.mio_par_ouvrier)
    time.sleep(8)

    spawns: list[float] = []
    fsyncs: list[float] = []
    lectures: list[float] = []
    with Echantillonneur() as sonde:
        for _ in range(args.echantillons):
            spawns.append(latence_spawn())
            fsyncs.append(latence_fsync())
            _, mib_s = debit_lecture(64)
            lectures.append(mib_s)
            time.sleep(args.periode)
    releves = list(sonde.releves)
    arret.set()
    for p in processus:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()
    FICHIER_FSYNC.unlink(missing_ok=True)

    c1 = {
        "vm": ram_swap.parse_vmstat((PROC / "vmstat").read_text()),
        "psi": ram_swap.parse_pressure((PROC / "pressure" / "memory").read_text()),
        "sysctl": sysctl_courant(),
    }
    resultat = {
        "etiquette": args.etiquette,
        "horodatage": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "charge": {"ouvriers": args.ouvriers, "mio_par_ouvrier": args.mio_par_ouvrier},
        "sysctl_debut": c0["sysctl"],
        "sysctl_fin": c1["sysctl"],
        "spawn_ms": quantiles(spawns),
        "fsync_ms": quantiles(fsyncs),
        "lecture_mio_s": quantiles(lectures),
        "pswpin_mio": (c1["vm"].get("pswpin", 0) - c0["vm"].get("pswpin", 0)) * 4096 / 1024**2,
        "pswpout_mio": (c1["vm"].get("pswpout", 0) - c0["vm"].get("pswpout", 0)) * 4096 / 1024**2,
        "psi_some_s": (c1["psi"].get("some_total", 0) - c0["psi"].get("some_total", 0)) / 1e6,
        "psi_full_s": (c1["psi"].get("full_total", 0) - c0["psi"].get("full_total", 0)) / 1e6,
        "echantillons": releves,
    }
    chemin = args.dossier / f"{args.etiquette}.json"
    chemin.write_text(json.dumps(resultat, ensure_ascii=False, indent=1))

    def ligne(nom: str, q: dict[str, float], unite: str) -> str:
        return f"{nom:<22} médiane {q['mediane']:9.2f} {unite}   p95 {q['p95']:9.2f}   max {q['max']:9.2f}"

    print(f"run {args.etiquette} — {args.ouvriers}×{args.mio_par_ouvrier} Mio, {args.echantillons} échantillons")
    print(f"sysctl début {c0['sysctl']}  fin {c1['sysctl']}")
    print(ligne("lancement outil", resultat["spawn_ms"], "ms"))
    print(ligne("sauvegarde fsync", resultat["fsync_ms"], "ms"))
    print(ligne("lecture disque", resultat["lecture_mio_s"], "Mio/s"))
    print(
        f"swap: in {resultat['pswpin_mio']:.0f} Mio, out {resultat['pswpout_mio']:.0f} Mio — "
        f"PSI some {resultat['psi_some_s']:.1f} s, full {resultat['psi_full_s']:.1f} s"
    )
    print(f"résultat : {chemin}")
    return 0


def sysctl_courant() -> dict[str, int]:
    noms = ("swappiness", "vfs_cache_pressure", "page-cluster")
    return {n.replace("-", "_"): int((PROC / "sys" / "vm" / n).read_text().strip()) for n in noms}


if __name__ == "__main__":
    sys.exit(main())
