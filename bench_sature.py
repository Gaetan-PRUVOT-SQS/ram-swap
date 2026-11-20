#!/usr/bin/env python3
"""Benchmark créatif : poste créatif saturé — design web + onglets + build de dev.

Trois instances d'outils de design web réels (Photopea, Excalidraw) et trois
onglets de contenu chargé tournent dans Chrome headless, chacune adossée à un
« modèle de document » en RAM (les calques d'un fichier de design) touché en
continu. Pendant ce temps, trois builds gcc complets doivent aboutir : le
temps de build est la mesure de la tâche exigeante. Latences perçues, PSI,
swap et décisions du démon sont échantillonnés pendant toute la saturation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import multiprocessing
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bench_reel
import ram_swap

PROC = Path("/proc")
CHROME = "/usr/bin/google-chrome"
PROFILS = Path.home() / ".cache" / "bench-sature"
DOSSIER_BUILD = Path(__file__).parent / "build" / "churn-c"
DESIGNS = (
    "https://www.photopea.com",
    "https://excalidraw.com",
    "https://excalidraw.com/#room=test",
)
ARTICLES = (
    "https://fr.wikipedia.org/wiki/Histoire_des_ordinateurs",
    "https://fr.wikipedia.org/wiki/Microprocesseur",
    "https://fr.wikipedia.org/wiki/Syst%C3%A8me_de_fichiers",
)
NB_FICHIERS = 80


def fichier_source(i: int) -> str:
    lignes = ["#include <stdint.h>", "", "uint64_t calcul_%d(uint64_t graine);" % i, ""]
    lignes.append("static uint64_t grille[256];")
    lignes.append("")
    lignes.append("uint64_t calcul_%d(uint64_t graine) {" % i)
    lignes.append("    uint64_t s = graine ^ 0x%016xULL;" % (i * 0x9E3779B97F4A7C15 % (1 << 64)))
    lignes.append("    for (uint64_t k = 0; k < 400; k++) {")
    lignes.append("        s = s * 6364136223846793005ULL + 1442695040888963407ULL;")
    lignes.append("        grille[(s >> 33) & 255] += k;")
    lignes.append("    }")
    lignes.append("    return grille[0] ^ s;")
    lignes.append("}")
    return "\n".join(lignes) + "\n"


def generer_build() -> Path:
    DOSSIER_BUILD.mkdir(parents=True, exist_ok=True)
    for old in DOSSIER_BUILD.glob("*"):
        old.unlink()
    for i in range(NB_FICHIERS):
        (DOSSIER_BUILD / f"module_{i:03d}.c").write_text(fichier_source(i))
    main = ["#include <stdint.h>", "#include <stdio.h>", ""]
    main += [f"uint64_t calcul_{i}(uint64_t graine);" for i in range(NB_FICHIERS)]
    main += ["", "int main(void) {", "    uint64_t total = 0;", "    for (uint64_t g = 1; g < 4; g++) {"]
    main += [f"        total ^= calcul_{i}(g);" for i in range(NB_FICHIERS)]
    main += ["    }", '    printf("%llu\\n", (unsigned long long)total);', "    return 0;", "}"]
    (DOSSIER_BUILD / "main.c").write_text("\n".join(main) + "\n")
    return DOSSIER_BUILD


def lance_build(jobs: int) -> tuple[float, int]:
    debut = time.perf_counter()
    objets = [str(DOSSIER_BUILD / f"module_{i:03d}.o") for i in range(NB_FICHIERS)]

    def un(i: int) -> int:
        r = subprocess.run(
            ["gcc", "-O2", "-c", str(DOSSIER_BUILD / f"module_{i:03d}.c"), "-o", objets[i]],
            capture_output=True,
        )
        return r.returncode

    codes = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        codes = sum(pool.map(un, range(NB_FICHIERS)))
    lien = subprocess.run(
        ["gcc", "-O2", str(DOSSIER_BUILD / "main.c"), *objets, "-o", str(DOSSIER_BUILD / "churn")],
        capture_output=True,
    )
    duree = time.perf_counter() - debut
    return duree, codes + lien.returncode


def lance_chrome(url: str, numero: int) -> subprocess.Popen:
    profil = PROFILS / f"profil-{numero}"
    profil.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--user-data-dir=" + str(profil),
            "--window-size=1600,900",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def compteur_oom(depuis: str) -> int:
    r = subprocess.run(
        ["sudo", "-n", "journalctl", "-k", "--since", depuis, "--no-pager"],
        capture_output=True,
        text=True,
    )
    return sum(1 for ligne in r.stdout.splitlines() if "oom-killer" in ligne.lower() or "out of memory" in ligne.lower())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Saturation créative : design + onglets + build.")
    parser.add_argument("--etiquette", required=True)
    parser.add_argument("--mio-par-document", type=int, default=600)
    parser.add_argument("--builds", type=int, default=3)
    parser.add_argument("--dossier", type=Path, default=Path(__file__).parent / "resultats")
    args = parser.parse_args()

    subprocess.run(["sudo", "-n", "sysctl", "-w", "vm.drop_caches=3"], check=True, capture_output=True)
    subprocess.run(["sudo", "-n", "/sbin/swapoff", "-a"], check=True, capture_output=True)
    subprocess.run(["sudo", "-n", "/sbin/swapon", "-a"], check=True, capture_output=True)
    subprocess.run(["sudo", "-n", "journalctl", "--rotate"], check=False, capture_output=True)
    heure = time.strftime("%H:%M:%S")
    c0 = {
        "vm": ram_swap.parse_vmstat((PROC / "vmstat").read_text()),
        "psi": ram_swap.parse_pressure((PROC / "pressure" / "memory").read_text()),
        "sysctl": bench_reel.sysctl_courant(),
    }

    generer_build()
    PROFILS.mkdir(parents=True, exist_ok=True)
    urls = list(DESIGNS) + list(ARTICLES)
    navigateurs = [lance_chrome(url, i) for i, url in enumerate(urls)]

    processus, arret = bench_reel.lancer_ouvriers(3, args.mio_par_document)
    time.sleep(25)
    morts_avant = [p for p in processus if not p.is_alive()]
    if morts_avant or any(not n.poll() is None for n in navigateurs):
        raise RuntimeError("un navigateur ou un ouvrier est mort pendant la montée : mesure annulée")

    spawns: list[float] = []
    fsyncs: list[float] = []
    lectures: list[float] = []
    builds: list[dict[str, float | int]] = []
    with bench_reel.Echantillonneur() as sonde:
        for numero in range(args.builds):
            duree, code = lance_build(jobs=2)
            builds.append({"duree_s": duree, "code": code})
            for _ in range(6):
                spawns.append(bench_reel.latence_spawn())
                fsyncs.append(bench_reel.latence_fsync())
                _, mib_s = bench_reel.debit_lecture(32)
                lectures.append(mib_s)
                time.sleep(2.0)
    releves = list(sonde.releves)
    arret.set()
    for p in processus:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()
    surviveurs = sum(1 for p in processus if p.exitcode in (None, 0))
    for n in navigateurs:
        n.terminate()
    for profil in PROFILS.glob("profil-*"):
        subprocess.run(["rm", "-rf", str(profil)], check=False)

    c1 = {
        "vm": ram_swap.parse_vmstat((PROC / "vmstat").read_text()),
        "psi": ram_swap.parse_pressure((PROC / "pressure" / "memory").read_text()),
        "sysctl": bench_reel.sysctl_courant(),
    }
    mem = ram_swap.parse_meminfo((PROC / "meminfo").read_text())
    resultat = {
        "etiquette": args.etiquette,
        "horodatage": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "charge": {
            "navigateurs": len(urls),
            "documents": 3,
            "mio_par_document": args.mio_par_document,
            "fichiers_build": NB_FICHIERS,
        },
        "sysctl_debut": c0["sysctl"],
        "sysctl_fin": c1["sysctl"],
        "builds": builds,
        "build_mediane_s": sorted(b["duree_s"] for b in builds)[len(builds) // 2],
        "tous_builds_ok": all(b["code"] == 0 for b in builds),
        "ouvriers_survivre": surviveurs,
        "spawn_ms": bench_reel.quantiles(spawns),
        "fsync_ms": bench_reel.quantiles(fsyncs),
        "lecture_mio_s": bench_reel.quantiles(lectures),
        "pswpin_mio": (c1["vm"].get("pswpin", 0) - c0["vm"].get("pswpin", 0)) * 4096 / 1024**2,
        "pswpout_mio": (c1["vm"].get("pswpout", 0) - c0["vm"].get("pswpout", 0)) * 4096 / 1024**2,
        "psi_some_s": (c1["psi"].get("some_total", 0) - c0["psi"].get("some_total", 0)) / 1e6,
        "psi_full_s": (c1["psi"].get("full_total", 0) - c0["psi"].get("full_total", 0)) / 1e6,
        "swap_libre_fin_mio": mem.get("SwapFree", 0) // 1024,
        "oom_events": compteur_oom(heure),
        "echantillons": releves,
    }
    args.dossier.mkdir(parents=True, exist_ok=True)
    chemin = args.dossier / f"{args.etiquette}.json"
    chemin.write_text(json.dumps(resultat, ensure_ascii=False, indent=1))

    print(f"run {args.etiquette} — {len(urls)} onglets, 3 documents × {args.mio_par_document} Mio, {args.builds} builds")
    print(f"sysctl début {c0['sysctl']}  fin {c1['sysctl']}")
    for i, b in enumerate(builds, 1):
        print(f"build {i}: {b['duree_s']:.1f} s  code {b['code']}")
    print(
        f"lancement {resultat['spawn_ms']['mediane']:.1f} ms  fsync {resultat['fsync_ms']['mediane']:.1f} ms"
        f" (p95 {resultat['fsync_ms']['p95']:.1f})  lecture {resultat['lecture_mio_s']['mediane']:.0f} Mio/s"
    )
    print(
        f"swap in {resultat['pswpin_mio']:.0f} / out {resultat['pswpout_mio']:.0f} Mio — "
        f"PSI some {resultat['psi_some_s']:.1f} s full {resultat['psi_full_s']:.1f} s — "
        f"OOM {resultat['oom_events']} — survivants {surviveurs}/3"
    )
    print(f"résultat : {chemin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
