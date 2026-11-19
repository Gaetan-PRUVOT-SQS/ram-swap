#!/usr/bin/env python3
"""Trois strates, sur le modèle Strata.

La RAM garde le jeu de travail, les pages touchées récemment. S'il
existe, zram est la strate tiède : les pages froides y sont compressées.
Le fichier de swap est l'archive : il ne reçoit ces pages que lorsque
la RAM se serre, et jamais au point de vider le travail en cours.
Ce noyau n'a en général pas zram. La strate tiède reste alors inactive
et l'archive est le fichier de 8 Go.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
STATE_PATH = Path("/run/ram-swap-policy.json")
SWAPPINESS_PATH = Path("/proc/sys/vm/swappiness")
CACHE_PRESSURE_PATH = Path("/proc/sys/vm/vfs_cache_pressure")
PAGE_CLUSTER_PATH = Path("/proc/sys/vm/page-cluster")

# Au-dessus, la RAM suffit : on ne déplace rien.
CALM_AVAILABLE = 0.40
# En dessous, les pages froides peuvent descendre d'une strate.
ARCHIVE_AVAILABLE = 0.20
# En dessous, la RAM est vraiment pleine.
EMERGENCY_AVAILABLE = 0.08
SWAP_RESERVE_KB = 256 * 1024
ZRAM_READY_KB = 128 * 1024
LOW_ANON_KB = 256 * 1024
# Débit de swap à partir duquel le disque fatigue.
THRASH_MBPS = 40.0
SWAPPINESS_MIN = 10
# Le travail récent reste en RAM. On ne monte pas jusqu'à 100.
SWAPPINESS_THROTTLE = 15
SWAPPINESS_ARCHIVE = 25
SWAPPINESS_COOLED_QUIET = 30
SWAPPINESS_COOLED = 35
SWAPPINESS_URGENT = 40
# Une strate déjà choisie attend ce supplément de RAM disponible avant de lâcher.
HYSTERESIS = 0.05
PAGE_CLUSTER_DISK = 4
PAGE_CLUSTER_ZRAM_ONLY = 0


@dataclass(frozen=True)
class Sample:
    mem_total_kb: int
    mem_available_kb: int
    anon_pages_kb: int
    swap_total_kb: int
    swap_free_kb: int
    swap_cached_kb: int
    psi_some_avg10: float
    psi_full_avg10: float
    pswpin_pages: int
    pswpout_pages: int
    taken_at: float
    zram_total_kb: int = 0
    zram_free_kb: int = 0

    @property
    def available_ratio(self) -> float:
        if self.mem_total_kb <= 0:
            return 0.0
        return self.mem_available_kb / self.mem_total_kb

    @property
    def swap_free_ratio(self) -> float:
        if self.swap_total_kb <= 0:
            return 0.0
        return self.swap_free_kb / self.swap_total_kb


@dataclass(frozen=True)
class SwapRate:
    mb_per_s: float
    seconds: float


@dataclass(frozen=True)
class Decision:
    level: str
    swappiness: int
    vfs_cache_pressure: int
    page_cluster: int
    reason: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


_PSI_LINE = re.compile(
    r"^(some|full)\s+avg10=([0-9.]+)\s+avg60=([0-9.]+)\s+avg300=([0-9.]+)\s+total=([0-9]+)\s*$"
)


def parse_meminfo(text: str) -> dict[str, int]:
    """Découpe /proc/meminfo en kilo-octets."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        token = rest.strip().split(maxsplit=1)[0]
        if token.lstrip("-").isdigit():
            values[key.strip()] = int(token)
    return values


def parse_pressure(text: str) -> dict[str, float]:
    """Découpe /proc/pressure/memory. Les moyennes sont des pourcentages."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        match = _PSI_LINE.match(line.strip())
        if not match:
            continue
        kind = match.group(1)
        values[f"{kind}_avg10"] = float(match.group(2))
        values[f"{kind}_avg60"] = float(match.group(3))
        values[f"{kind}_avg300"] = float(match.group(4))
        values[f"{kind}_total"] = float(match.group(5))
    return values


def parse_vmstat(text: str) -> dict[str, int]:
    """Découpe les compteurs cumulés de /proc/vmstat."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            values[parts[0]] = int(parts[1])
    return values


def read_zram_kb(proc: Path) -> tuple[int, int]:
    """Espace zram libre, lu dans /proc/swaps. (0, 0) s'il n'y en a pas."""
    path = proc / "swaps"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return 0, 0
    total = 0
    free = 0
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 4 or "zram" not in parts[0]:
            continue
        size_kb = int(parts[2])
        used_kb = int(parts[3])
        total += size_kb
        free += max(0, size_kb - used_kb)
    return total, free


def read_sample(proc: Path = Path("/proc"), taken_at: float | None = None) -> Sample:
    mem = parse_meminfo((proc / "meminfo").read_text())
    pressure = parse_pressure((proc / "pressure" / "memory").read_text())
    vm = parse_vmstat((proc / "vmstat").read_text())
    required = ("MemTotal", "MemAvailable", "AnonPages", "SwapTotal", "SwapFree", "SwapCached")
    missing = [key for key in required if key not in mem]
    if missing:
        raise ValueError(f"champs meminfo absents: {', '.join(missing)}")
    zram_total, zram_free = read_zram_kb(proc)
    return Sample(
        mem_total_kb=mem["MemTotal"],
        mem_available_kb=mem["MemAvailable"],
        anon_pages_kb=mem["AnonPages"],
        swap_total_kb=mem["SwapTotal"],
        swap_free_kb=mem["SwapFree"],
        swap_cached_kb=mem["SwapCached"],
        psi_some_avg10=pressure.get("some_avg10", 0.0),
        psi_full_avg10=pressure.get("full_avg10", 0.0),
        pswpin_pages=vm.get("pswpin", 0),
        pswpout_pages=vm.get("pswpout", 0),
        taken_at=time.monotonic() if taken_at is None else taken_at,
        zram_total_kb=zram_total,
        zram_free_kb=zram_free,
    )


def swap_rate(previous: Sample, current: Sample, page_size: int = PAGE_SIZE) -> SwapRate | None:
    """Débit de pages entrées et sorties du swap entre deux lectures."""
    elapsed = current.taken_at - previous.taken_at
    if elapsed <= 0:
        return None
    pages_in = max(0, current.pswpin_pages - previous.pswpin_pages)
    pages_out = max(0, current.pswpout_pages - previous.pswpout_pages)
    mb_per_s = (pages_in + pages_out) * page_size / elapsed / 1_000_000
    return SwapRate(mb_per_s=mb_per_s, seconds=elapsed)


def _below(ratio: float, threshold: float, hold: bool) -> bool:
    """Sous le seuil. La strate déjà choisie ne lâche que 5 points plus haut."""
    if hold:
        threshold += HYSTERESIS
    return ratio < threshold


def page_cluster_for(sample: Sample) -> int:
    """4 avec un swap disque (lectures de 64 Ko). 0 si seul zram est là."""
    disk_kb = sample.swap_total_kb - sample.zram_total_kb
    if disk_kb > 0:
        return PAGE_CLUSTER_DISK
    if sample.zram_total_kb > 0:
        return PAGE_CLUSTER_ZRAM_ONLY
    return PAGE_CLUSTER_DISK


def assess(
    sample: Sample,
    rate: SwapRate | None = None,
    previous: Decision | None = None,
) -> Decision:
    """Place les pages froides dans la strate la moins chère qui a de la place.

    La RAM reste sur le jeu de travail. Sans zram, swappiness ne dépasse
    pas 40 : le noyau (MGLRU) n'évince que les pages les plus anciennes.
    Une strate déjà engagée attend 5 points de RAM disponible de plus
    avant de rendre la main, pour ne pas osciller à chaque lecture.
    """
    cluster = page_cluster_for(sample)
    if sample.swap_total_kb <= 0:
        return Decision(
            level="sans-swap",
            swappiness=SWAPPINESS_MIN,
            vfs_cache_pressure=100,
            page_cluster=cluster,
            reason="aucun swap n'est actif, le réglage ne change rien",
        )

    zram_ready = sample.zram_free_kb >= ZRAM_READY_KB
    hold_urgent = previous is not None and previous.level == "urgent"
    hold_tight = previous is not None and (
        previous.level == "archive"
        or (previous.level == "cooled" and previous.swappiness == SWAPPINESS_COOLED)
    )
    hold_quiet = (
        previous is not None
        and previous.level == "cooled"
        and previous.swappiness == SWAPPINESS_COOLED_QUIET
    )
    emergency = _below(sample.available_ratio, EMERGENCY_AVAILABLE, hold_urgent) or (
        sample.psi_full_avg10 >= 5.0
    )
    stalled = sample.psi_some_avg10 >= 8.0 or sample.psi_full_avg10 >= 1.0
    thrashing = (
        rate is not None
        and rate.mb_per_s >= THRASH_MBPS
        and sample.psi_full_avg10 >= 1.0
        and not emergency
    )

    if emergency:
        level = "urgent"
        swappiness = SWAPPINESS_URGENT
        cache_pressure = 150
        reason = "RAM presque pleine : seules les pages les plus froides partent"
    elif thrashing and rate is not None:
        level = "sature"
        swappiness = SWAPPINESS_THROTTLE
        cache_pressure = 100
        reason = (
            f"le swap déplace déjà {rate.mb_per_s:.0f} Mo/s et des tâches sont bloquées"
        )
    elif _below(sample.available_ratio, ARCHIVE_AVAILABLE, hold_tight) or stalled:
        if zram_ready:
            level = "cooled"
            swappiness = SWAPPINESS_COOLED
            cache_pressure = 80
            reason = "strate tiède : les pages froides vont dans zram"
        else:
            level = "archive"
            swappiness = SWAPPINESS_ARCHIVE
            cache_pressure = 100
            reason = "strate archive : les pages froides descendent sur le swap disque"
    elif zram_ready and _below(sample.available_ratio, CALM_AVAILABLE, hold_quiet):
        level = "cooled"
        swappiness = SWAPPINESS_COOLED_QUIET
        cache_pressure = 80
        reason = "strate tiède : zram absorbe les pages peu utilisées"
    else:
        level = "active"
        swappiness = SWAPPINESS_MIN
        cache_pressure = 50
        reason = "strate active : la RAM conserve ce qui est utile dans l'immédiat"

    chosen = swappiness
    notes: list[str] = []
    if sample.swap_free_kb < SWAP_RESERVE_KB:
        swappiness = min(swappiness, SWAPPINESS_MIN)
        notes.append("le swap a moins de 256 Mio de libre")
    if sample.anon_pages_kb < LOW_ANON_KB and not emergency:
        swappiness = min(swappiness, SWAPPINESS_MIN)
        notes.append("peu de pages anonymes peuvent partir en swap")
    # Un plafond qui ramène swappiness au plancher annule la strate :
    # le journal doit décrire le bouton réellement écrit. La pression
    # de cache reste celle du manque de RAM.
    if swappiness < chosen:
        level = "active"
        reason = "strate active : la RAM conserve ce qui est utile dans l'immédiat"
    if notes:
        reason = f"{reason}; {'; '.join(notes)}"
    return Decision(level, swappiness, cache_pressure, cluster, reason)


def _read_sysctl(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except OSError:
        return None


def _write_sysctl(path: Path, value: int) -> bool:
    current = _read_sysctl(path)
    if current == value:
        return False
    path.write_text(f"{value}\n")
    return True


def apply_decision(decision: Decision) -> list[str]:
    changes: list[str] = []
    if _write_sysctl(SWAPPINESS_PATH, decision.swappiness):
        changes.append(f"swappiness={decision.swappiness}")
    if _write_sysctl(CACHE_PRESSURE_PATH, decision.vfs_cache_pressure):
        changes.append(f"vfs_cache_pressure={decision.vfs_cache_pressure}")
    if _write_sysctl(PAGE_CLUSTER_PATH, decision.page_cluster):
        changes.append(f"page-cluster={decision.page_cluster}")
    return changes


def _sample_from(raw: object) -> Sample | None:
    if not isinstance(raw, dict):
        return None
    try:
        return Sample(**raw)
    except TypeError:
        return None


def _decision_from(raw: object) -> Decision | None:
    if not isinstance(raw, dict):
        return None
    try:
        return Decision(**raw)
    except TypeError:
        return None


def load_state(path: Path = STATE_PATH) -> tuple[Sample | None, Decision | None]:
    """Relit l'échantillon et la décision précédente.

    L'ancien fichier ne contient que l'échantillon. Il sert encore au
    débit de swap ; l'hystérésis attend la décision du format nouveau.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    if "sample" in raw:
        return _sample_from(raw.get("sample")), _decision_from(raw.get("decision"))
    return _sample_from(raw), None


def save_state(sample: Sample, decision: Decision, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sample": asdict(sample), "decision": asdict(decision)}
    path.write_text(json.dumps(payload, ensure_ascii=False))


def run_once(proc: Path, state_path: Path, dry_run: bool) -> Decision:
    sample = read_sample(proc)
    previous, previous_decision = load_state(state_path)
    rate = swap_rate(previous, sample) if previous else None
    decision = assess(sample, rate, previous_decision)
    if not dry_run:
        apply_decision(decision)
        save_state(sample, decision, state_path)
    return decision


def format_line(sample: Sample, decision: Decision) -> str:
    swap_used = sample.swap_total_kb - sample.swap_free_kb
    return (
        f"{decision.level} swappiness={decision.swappiness} "
        f"cache={decision.vfs_cache_pressure} cluster={decision.page_cluster} "
        f"dispo={sample.mem_available_kb // 1024} Mio "
        f"swap={swap_used // 1024}/{sample.swap_total_kb // 1024} Mio "
        f"— {decision.reason}"
    )


def watch(proc: Path, state_path: Path, interval: float) -> None:
    stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    last_key: tuple[str, int, int, int] | None = None
    while not stop:
        sample = read_sample(proc)
        previous, previous_decision = load_state(state_path)
        rate = swap_rate(previous, sample) if previous else None
        decision = assess(sample, rate, previous_decision)
        apply_decision(decision)
        save_state(sample, decision, state_path)
        key = (decision.level, decision.swappiness, decision.vfs_cache_pressure, decision.page_cluster)
        if key != last_key:
            print(format_line(sample, decision), flush=True)
            last_key = key
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Règle le swap à partir de la pression RAM.")
    parser.add_argument("--dry-run", action="store_true", help="affiche la décision sans l'appliquer")
    parser.add_argument("--json", action="store_true", help="affiche la décision en JSON")
    parser.add_argument("--watch", action="store_true", help="recalcule en boucle")
    parser.add_argument("--interval", type=float, default=5.0, help="secondes entre deux lectures")
    parser.add_argument("--proc", type=Path, default=Path("/proc"))
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    args = parser.parse_args(argv)

    if args.interval <= 0:
        parser.error("--interval doit être positif")

    if args.watch:
        if args.dry_run:
            parser.error("--watch et --dry-run ne vont pas ensemble")
        watch(args.proc, args.state, args.interval)
        return 0

    sample = read_sample(args.proc)
    previous, previous_decision = load_state(args.state)
    rate = swap_rate(previous, sample) if previous else None
    decision = assess(sample, rate, previous_decision)
    if not args.dry_run:
        apply_decision(decision)
        save_state(sample, decision, args.state)
    if args.json:
        print(decision.to_json())
    else:
        print(format_line(sample, decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
