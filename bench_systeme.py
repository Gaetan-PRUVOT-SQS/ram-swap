#!/usr/bin/env python3
"""Benchmark système : churn mémoire sous une configuration noyau donnée.

Phase 1 : hog de 4 Gio touché puis churné (l'éviction vers le swap est
le fait du noyau, pas de l'outil). Phase 2 : latence de refault 4 KiB
aléatoire sur une région explicitement poussée en swap par MADV_PAGEOUT,
étalonnée contre la même tournée en RAM. Un échantillonneur note le
swappiness effectif et la PSI pendant toute la charge.
"""

from __future__ import annotations

import ctypes
import json
import mmap
import random
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ram_swap

PROC = Path("/proc")
PAGE = 4096
CHUNK = 64 * 1024 * 1024
MADV_PAGEOUT = 21
MAP_PRIVE_ANON_NORESERVE = 0x02 | 0x20 | 0x4000
PROT_ECRIRE_LIRE = 0x03

LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
LIBC.mmap.restype = ctypes.c_void_p
LIBC.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
LIBC.madvise.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
LIBC.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]


def sysctl_snapshot() -> dict[str, int]:
    noms = ("swappiness", "vfs_cache_pressure", "page-cluster")
    return {n.replace("-", "_"): int((PROC / "sys" / "vm" / n).read_text().strip()) for n in noms}


def compteurs() -> dict[str, float]:
    vm = ram_swap.parse_vmstat((PROC / "vmstat").read_text())
    psi = ram_swap.parse_pressure((PROC / "pressure" / "memory").read_text())
    return {
        "pswpin": vm.get("pswpin", 0),
        "pswpout": vm.get("pswpout", 0),
        "psi_some_total_us": psi.get("some_total", 0.0),
        "psi_full_total_us": psi.get("full_total", 0.0),
    }


def delta_compteurs(avant: dict[str, float], apres: dict[str, float]) -> dict[str, float]:
    return {cle: apres[cle] - avant[cle] for cle in avant}


def vider_caches() -> None:
    for tentative in range(3):
        try:
            subprocess.run(
                ["sudo", "-n", "sysctl", "-w", "vm.drop_caches=3"],
                check=True,
                capture_output=True,
            )
            time.sleep(3)
            return
        except subprocess.CalledProcessError:
            time.sleep(2 * tentative + 1)
    raise RuntimeError("drop_caches a échoué après 3 tentatives")


def passe_churn(mm: mmap.mmap, taille: int, zeros: bytes) -> tuple[float, float, int]:
    """Une passe : écriture complète puis lecture complète. Retourne (durée, durée_lecture, somme)."""
    debut = time.perf_counter()
    for o in range(0, taille, len(zeros)):
        mm[o : o + len(zeros)] = zeros
    lecture = time.perf_counter()
    somme = 0
    for o in range(0, taille, len(zeros)):
        bloc = mm[o : o + len(zeros)]
        somme ^= bloc[0]
    return debut, lecture, time.perf_counter(), somme


class Echantillonneur:
    def __init__(self) -> None:
        self.arret = threading.Event()
        self.releves: list[dict[str, float]] = []
        self._fil = threading.Thread(target=self._boucle, daemon=True)

    def _boucle(self) -> None:
        t0 = time.monotonic()
        while not self.arret.is_set():
            psi = ram_swap.parse_pressure((PROC / "pressure" / "memory").read_text())
            self.releves.append(
                {
                    "t": round(time.monotonic() - t0, 2),
                    "swappiness": sysctl_snapshot()["swappiness"],
                    "psi_some_avg10": psi.get("some_avg10", 0.0),
                    "psi_full_avg10": psi.get("full_avg10", 0.0),
                }
            )
            self.arret.wait(0.5)

    def __enter__(self) -> "Echantillonneur":
        self._fil.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.arret.set()
        self._fil.join(timeout=2)


def mmap_ctypes(taille: int) -> int:
    addr = LIBC.mmap(None, taille, PROT_ECRIRE_LIRE, MAP_PRIVE_ANON_NORESERVE, -1, 0)
    if not addr or addr == 2**64 - 1:
        raise MemoryError(f"mmap ctypes a échoué pour {taille} octets")
    return addr


def tournure(addr: int, offsets: list[int]) -> list[float]:
    temps = []
    for off in offsets:
        debut = time.perf_counter()
        ctypes.string_at(addr + off, 8)
        temps.append((time.perf_counter() - debut) * 1e6)
    return temps


def test_random(taille: int, graine: int) -> dict[str, float | int]:
    rng = random.Random(graine)
    offsets = list(range(0, taille, PAGE))
    rng.shuffle(offsets)
    addr = mmap_ctypes(taille)
    try:
        for off in offsets:
            ctypes.memset(addr + off, 0x41, 8)
        resident = tournure(addr, offsets)
        ret = LIBC.madvise(ctypes.c_void_p(addr), taille, MADV_PAGEOUT)
        if ret != 0:
            raise OSError("MADV_PAGEOUT a échoué")
        time.sleep(1)
        pousse = tournure(addr, offsets)
    finally:
        LIBC.munmap(ctypes.c_void_p(addr), taille)
    resident.sort()
    pousse.sort()

    def pct(serie: list[float], q: float) -> float:
        return serie[max(0, int(len(serie) * q) - 1)]

    return {
        "pages": len(offsets),
        "resident_median_us": statistics.median(resident),
        "resident_p95_us": pct(resident, 0.95),
        "swap_median_us": statistics.median(pousse),
        "swap_p95_us": pct(pousse, 0.95),
        "latence_median_us": statistics.median(pousse) - statistics.median(resident),
        "latence_p95_us": pct(pousse, 0.95) - pct(resident, 0.95),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Churn mémoire et refault : mesure d'une configuration.")
    parser.add_argument("--etiquette", required=True, help="nom du run, ex. solution-run1")
    parser.add_argument("--taille-hog-mio", type=int, default=4096)
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--taille-random-mio", type=int, default=256)
    parser.add_argument("--graine", type=int, default=42)
    parser.add_argument("--dossier", type=Path, default=Path(__file__).parent / "resultats")
    args = parser.parse_args(argv)

    taille = args.taille_hog_mio * 1024 * 1024
    zeros = bytes(CHUNK)
    sysctl_debut = sysctl_snapshot()
    vider_caches()

    hog = mmap.mmap(-1, taille)
    with Echantillonneur() as sonde:
        t_montee = time.perf_counter()
        for o in range(0, taille, CHUNK):
            hog[o : o + CHUNK] = zeros
        montee = time.perf_counter() - t_montee

        c_avant_warm = compteurs()
        passe_churn(hog, taille, zeros)
        c_apres_warm = compteurs()

        passes: list[dict[str, float]] = []
        for _ in range(args.passes):
            c_avant = compteurs()
            duree, duree_lecture, fin, _somme = passe_churn(hog, taille, zeros)
            c_apres = compteurs()
            d = delta_compteurs(c_avant, c_apres)
            pages = d["pswpin"] + d["pswpout"]
            passes.append(
                {
                    "duree_s": fin - duree,
                    "duree_lecture_s": fin - duree_lecture,
                    "pswpin_mio": d["pswpin"] * PAGE / 1024**2,
                    "pswpout_mio": d["pswpout"] * PAGE / 1024**2,
                    "debit_mio_s": pages * PAGE / (fin - duree) / 1024**2,
                    "psi_some_ms": d["psi_some_total_us"] / 1000,
                    "psi_full_ms": d["psi_full_total_us"] / 1000,
                }
            )
    releves = list(sonde.releves)
    del hog

    random_res = test_random(args.taille_random_mio * 1024 * 1024, args.graine)
    sysctl_fin = sysctl_snapshot()
    swap_final = ram_swap.parse_meminfo((PROC / "meminfo").read_text())

    resultat = {
        "etiquette": args.etiquette,
        "horodatage": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "taille_hog_mio": args.taille_hog_mio,
        "passes_churn": args.passes,
        "sysctl_debut": sysctl_debut,
        "sysctl_fin": sysctl_fin,
        "montee_s": montee,
        "warmup_pswpout_mio": (c_apres_warm["pswpout"] - c_avant_warm["pswpout"]) * PAGE / 1024**2,
        "warmup_pswpin_mio": (c_apres_warm["pswpin"] - c_avant_warm["pswpin"]) * PAGE / 1024**2,
        "churn": passes,
        "refault_4k": random_res,
        "echantillons": releves,
        "swap_used_mio": (swap_final.get("SwapTotal", 0) - swap_final.get("SwapFree", 0)) // 1024,
    }
    args.dossier.mkdir(parents=True, exist_ok=True)
    chemin = args.dossier / f"{args.etiquette}.json"
    chemin.write_text(json.dumps(resultat, ensure_ascii=False, indent=1))

    print(f"run {args.etiquette}  hog {args.taille_hog_mio} Mio  passes {args.passes}")
    print(f"sysctl début {sysctl_debut}  fin {sysctl_fin}")
    print(f"montée en charge {montee:.1f} s  swap final {resultat['swap_used_mio']} Mio")
    for i, p in enumerate(passes, 1):
        print(
            f"churn {i}: {p['duree_s']:.1f} s  débit {p['debit_mio_s']:.0f} Mio/s"
            f"  in {p['pswpin_mio']:.0f} out {p['pswpout_mio']:.0f} Mio"
            f"  PSI some {p['psi_some_ms'] / 1000:.1f} s full {p['psi_full_ms'] / 1000:.1f} s"
        )
    r = random_res
    print(
        f"refault 4K: {r['latence_median_us']:.1f} µs médiane"
        f"  p95 {r['latence_p95_us']:.1f} µs"
        f"  (brut swap {r['swap_median_us']:.1f} / resident {r['resident_median_us']:.1f})"
    )
    print(f"résultat : {chemin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
