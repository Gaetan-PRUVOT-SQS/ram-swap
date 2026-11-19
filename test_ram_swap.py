import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

import ram_swap


MEMINFO = """
MemTotal:        6795220 kB
MemAvailable:    2796064 kB
AnonPages:        113284 kB
SwapCached:            0 kB
SwapTotal:       4194300 kB
SwapFree:        4194300 kB
"""

PRESSURE = """
some avg10=0.00 avg60=0.10 avg300=0.20 total=3454875
full avg10=0.00 avg60=0.00 avg300=0.00 total=3229205
"""

VMSTAT = """
pswpin 10
pswpout 30
pgscan_anon 0
"""


def sample(**overrides: object) -> ram_swap.Sample:
    base = dict(
        mem_total_kb=8 * 1024 * 1024,
        mem_available_kb=4 * 1024 * 1024,
        anon_pages_kb=2 * 1024 * 1024,
        swap_total_kb=4 * 1024 * 1024,
        swap_free_kb=4 * 1024 * 1024,
        swap_cached_kb=0,
        psi_some_avg10=0.0,
        psi_full_avg10=0.0,
        pswpin_pages=0,
        pswpout_pages=0,
        taken_at=0.0,
    )
    base.update(overrides)
    return ram_swap.Sample(**base)


class ParseTests(unittest.TestCase):
    def test_meminfo_keeps_kilobytes(self) -> None:
        parsed = ram_swap.parse_meminfo(MEMINFO)
        self.assertEqual(parsed["MemTotal"], 6795220)
        self.assertEqual(parsed["SwapFree"], 4194300)

    def test_pressure_reads_both_lines(self) -> None:
        parsed = ram_swap.parse_pressure(PRESSURE)
        self.assertEqual(parsed["some_avg10"], 0.0)
        self.assertEqual(parsed["some_avg60"], 0.10)
        self.assertEqual(parsed["full_total"], 3229205)

    def test_vmstat_reads_swap_counters(self) -> None:
        parsed = ram_swap.parse_vmstat(VMSTAT)
        self.assertEqual(parsed["pswpin"], 10)
        self.assertEqual(parsed["pswpout"], 30)

    def test_read_sample_from_a_directory(self) -> None:
        root = Path(__file__).with_name("_proc_fixture")
        (root / "pressure").mkdir(parents=True, exist_ok=True)
        (root / "meminfo").write_text(MEMINFO)
        (root / "pressure" / "memory").write_text(PRESSURE)
        (root / "vmstat").write_text(VMSTAT)
        try:
            got = ram_swap.read_sample(root, taken_at=12.5)
        finally:
            for path in (
                root / "vmstat",
                root / "meminfo",
                root / "pressure" / "memory",
            ):
                path.unlink(missing_ok=True)
            (root / "pressure").rmdir()
            root.rmdir()
        self.assertEqual(got.mem_total_kb, 6795220)
        self.assertEqual(got.pswpout_pages, 30)
        self.assertEqual(got.taken_at, 12.5)


class StateTests(unittest.TestCase):
    def test_legacy_sample_file_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(asdict(sample())))
            loaded, decision = ram_swap.load_state(path)
        self.assertIsNotNone(loaded)
        self.assertIsNone(decision)
        assert loaded is not None
        self.assertEqual(loaded.mem_total_kb, sample().mem_total_kb)

    def test_round_trip_keeps_the_decision(self) -> None:
        current = sample(mem_available_kb=int(8 * 1024 * 1024 * 0.15))
        decision = ram_swap.assess(current)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            ram_swap.save_state(current, decision, path)
            loaded, previous = ram_swap.load_state(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(previous, decision)


class PolicyTests(unittest.TestCase):
    def test_calm_when_plenty_of_ram(self) -> None:
        decision = ram_swap.assess(sample())
        self.assertEqual(decision.level, "active")
        self.assertEqual(decision.swappiness, 10)
        self.assertEqual(decision.vfs_cache_pressure, 50)
        self.assertEqual(decision.page_cluster, 4)

    def test_urgent_when_ram_is_tight(self) -> None:
        decision = ram_swap.assess(sample(mem_available_kb=int(8 * 1024 * 1024 * 0.08)))
        self.assertEqual(decision.level, "urgent")
        self.assertEqual(decision.swappiness, 40)
        self.assertLessEqual(decision.swappiness, 40)
        self.assertEqual(decision.vfs_cache_pressure, 150)

    def test_archive_moves_only_cold_pages(self) -> None:
        decision = ram_swap.assess(sample(mem_available_kb=int(8 * 1024 * 1024 * 0.15)))
        self.assertEqual(decision.level, "archive")
        self.assertEqual(decision.swappiness, 25)

    def test_psi_stalls_open_the_archive_without_dumping_ram(self) -> None:
        decision = ram_swap.assess(sample(psi_some_avg10=8.0))
        self.assertEqual(decision.level, "archive")
        self.assertEqual(decision.swappiness, 25)

    def test_quiet_zram_holds_until_ram_recovers(self) -> None:
        total = 8 * 1024 * 1024
        zram = dict(
            anon_pages_kb=1024 * 1024,
            zram_total_kb=2 * 1024 * 1024,
            zram_free_kb=2 * 1024 * 1024,
            swap_total_kb=6 * 1024 * 1024,
        )
        entered = ram_swap.assess(sample(mem_available_kb=int(total * 0.30), **zram))
        self.assertEqual(entered.level, "cooled")
        self.assertEqual(entered.swappiness, 30)
        held = ram_swap.assess(
            sample(mem_available_kb=int(total * 0.42), **zram),
            previous=entered,
        )
        self.assertEqual(held.level, "cooled")
        self.assertEqual(held.swappiness, 30)
        released = ram_swap.assess(
            sample(mem_available_kb=int(total * 0.46), **zram),
            previous=held,
        )
        self.assertEqual(released.level, "active")
        self.assertEqual(released.swappiness, 10)

    def test_zram_is_the_cooled_tier(self) -> None:
        decision = ram_swap.assess(
            sample(
                mem_available_kb=int(8 * 1024 * 1024 * 0.30),
                zram_total_kb=2 * 1024 * 1024,
                zram_free_kb=2 * 1024 * 1024,
                swap_total_kb=6 * 1024 * 1024,
            )
        )
        self.assertEqual(decision.level, "cooled")
        self.assertEqual(decision.swappiness, 30)
        self.assertEqual(decision.page_cluster, 4)

    def test_thrashing_backs_off_while_ram_remains(self) -> None:
        current = sample(pswpin_pages=20000, pswpout_pages=20000, taken_at=2.0, psi_full_avg10=2.0)
        previous = sample(taken_at=0.0)
        rate = ram_swap.swap_rate(previous, current, page_size=4096)
        assert rate is not None
        self.assertGreater(rate.mb_per_s, ram_swap.THRASH_MBPS)
        decision = ram_swap.assess(current, rate)
        self.assertEqual(decision.level, "sature")
        self.assertEqual(decision.swappiness, 15)

    def test_emergency_keeps_swapping_despite_disk_load(self) -> None:
        current = sample(
            mem_available_kb=int(8 * 1024 * 1024 * 0.05),
            pswpin_pages=20000,
            pswpout_pages=20000,
            taken_at=2.0,
            psi_full_avg10=6.0,
        )
        rate = ram_swap.swap_rate(sample(taken_at=0.0), current, page_size=4096)
        decision = ram_swap.assess(current, rate)
        self.assertEqual(decision.level, "urgent")
        self.assertEqual(decision.swappiness, 40)
        self.assertLess(decision.swappiness, 100)

    def test_full_swap_stops_pushing(self) -> None:
        decision = ram_swap.assess(
            sample(mem_available_kb=1024 * 1024, swap_free_kb=100 * 1024)
        )
        self.assertEqual(decision.level, "active")
        self.assertEqual(decision.swappiness, 10)
        self.assertIn("256 Mio", decision.reason)
        self.assertNotIn("descendent", decision.reason)

    def test_file_cache_pressure_does_not_force_swap(self) -> None:
        decision = ram_swap.assess(
            sample(mem_available_kb=1024 * 1024, anon_pages_kb=64 * 1024)
        )
        self.assertEqual(decision.level, "active")
        self.assertEqual(decision.swappiness, 10)
        self.assertEqual(decision.vfs_cache_pressure, 100)
        self.assertIn("peu de pages anonymes", decision.reason)
        self.assertNotIn("descendent", decision.reason)

    def test_emergency_with_full_swap_stays_at_the_floor(self) -> None:
        decision = ram_swap.assess(
            sample(
                mem_available_kb=int(8 * 1024 * 1024 * 0.05),
                swap_free_kb=100 * 1024,
            )
        )
        self.assertEqual(decision.level, "active")
        self.assertEqual(decision.swappiness, 10)
        self.assertEqual(decision.vfs_cache_pressure, 150)
        self.assertIn("256 Mio", decision.reason)
        self.assertNotIn("partent", decision.reason)

    def test_archive_holds_until_ram_recovers(self) -> None:
        total = 8 * 1024 * 1024
        anon = 1024 * 1024
        entered = ram_swap.assess(
            sample(mem_available_kb=int(total * 0.18), anon_pages_kb=anon)
        )
        self.assertEqual(entered.level, "archive")
        self.assertEqual(entered.swappiness, 25)
        held = ram_swap.assess(
            sample(mem_available_kb=int(total * 0.22), anon_pages_kb=anon),
            previous=entered,
        )
        self.assertEqual(held.level, "archive")
        self.assertEqual(held.swappiness, 25)
        released = ram_swap.assess(
            sample(mem_available_kb=int(total * 0.26), anon_pages_kb=anon),
            previous=held,
        )
        self.assertEqual(released.level, "active")
        self.assertEqual(released.swappiness, 10)

    def test_just_above_the_line_stays_active(self) -> None:
        total = 8 * 1024 * 1024
        calm = ram_swap.assess(sample(anon_pages_kb=1024 * 1024))
        stayed = ram_swap.assess(
            sample(mem_available_kb=int(total * 0.22), anon_pages_kb=1024 * 1024),
            previous=calm,
        )
        self.assertEqual(stayed.level, "active")
        self.assertEqual(stayed.swappiness, 10)

    def test_urgent_holds_through_the_band(self) -> None:
        total = 8 * 1024 * 1024
        anon = 1024 * 1024
        entered = ram_swap.assess(
            sample(mem_available_kb=int(total * 0.05), anon_pages_kb=anon)
        )
        self.assertEqual(entered.level, "urgent")
        held = ram_swap.assess(
            sample(mem_available_kb=int(total * 0.10), anon_pages_kb=anon),
            previous=entered,
        )
        self.assertEqual(held.level, "urgent")
        self.assertEqual(held.swappiness, 40)
        stepped = ram_swap.assess(
            sample(mem_available_kb=int(total * 0.14), anon_pages_kb=anon),
            previous=held,
        )
        self.assertEqual(stepped.level, "archive")
        self.assertEqual(stepped.swappiness, 25)

    def test_no_swap_device(self) -> None:
        decision = ram_swap.assess(sample(swap_total_kb=0, swap_free_kb=0))
        self.assertEqual(decision.level, "sans-swap")

    def test_counter_reset_does_not_invent_traffic(self) -> None:
        rate = ram_swap.swap_rate(sample(pswpout_pages=500, taken_at=0.0), sample(taken_at=5.0))
        self.assertIsNotNone(rate)
        assert rate is not None
        self.assertEqual(rate.mb_per_s, 0.0)


if __name__ == "__main__":
    unittest.main()
