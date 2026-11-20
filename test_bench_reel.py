#!/usr/bin/env python3
"""Tests du harnais d'usage réel : quantiles, ouvriers, mesures ponctuelles."""

from __future__ import annotations

import multiprocessing
import sys
import time
import unittest

import bench_reel


class TestQuantiles(unittest.TestCase):
    def test_mediane_impair(self) -> None:
        self.assertEqual(bench_reel.quantiles([3.0, 1.0, 2.0])["mediane"], 2.0)

    def test_mediane_pair(self) -> None:
        self.assertEqual(bench_reel.quantiles([4.0, 1.0, 3.0, 2.0])["mediane"], 2.5)

    def test_p95_et_max(self) -> None:
        valeurs = [float(i) for i in range(1, 101)]
        q = bench_reel.quantiles(valeurs)
        self.assertEqual(q["p95"], 95.0)
        self.assertEqual(q["max"], 100.0)

    def test_echantillon_unique(self) -> None:
        q = bench_reel.quantiles([5.0])
        self.assertEqual((q["mediane"], q["p95"], q["max"]), (5.0, 5.0, 5.0))


class TestOuvrier(unittest.TestCase):
    def test_tranches_partielles(self) -> None:
        """La mémoire et la zone chaude ne tombent pas rond sur les blocs : l'ouvrier doit survivre."""
        arret = multiprocessing.Event()
        taille = 25 * 1024 * 1024 + 1234567
        chaud = 8 * 1024 * 1024 + 654321
        p = multiprocessing.Process(target=bench_reel.ouvrier, args=(taille, arret, chaud))
        p.start()
        time.sleep(0.4)
        arret.set()
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()
            p.join(timeout=2)
        self.assertEqual(p.exitcode, 0)


class TestMesures(unittest.TestCase):
    def test_sysctl_courant(self) -> None:
        valeurs = bench_reel.sysctl_courant()
        self.assertEqual(set(valeurs), {"swappiness", "vfs_cache_pressure", "page_cluster"})
        for valeur in valeurs.values():
            self.assertIsInstance(valeur, int)

    def test_latence_spawn(self) -> None:
        self.assertGreater(bench_reel.latence_spawn(), 0.0)

    def test_latence_fsync(self) -> None:
        try:
            self.assertGreater(bench_reel.latence_fsync(), 0.0)
        finally:
            bench_reel.FICHIER_FSYNC.unlink(missing_ok=True)

    def test_debit_lecture(self) -> None:
        duree, mib_s = bench_reel.debit_lecture(1)
        self.assertGreater(duree, 0.0)
        self.assertGreater(mib_s, 0.0)


if __name__ == "__main__":
    sys.exit(unittest.main())
