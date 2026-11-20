# ram-swap

A three-tier memory regulator for the Crostini container of a Chromebook. It reads RAM pressure every 5 seconds and writes three kernel knobs: `vm.swappiness`, `vm.vfs_cache_pressure` and `vm.page-cluster`.

Français : [README.md](README.md).

## Background

The machine this started on is an ASUS CM1405 Chromebook (MediaTek Kompanio 540, 8 GB RAM, 128 GB eMMC), picked for ultra-portability. Its Crostini container (Debian 13, arm64) exposes about 6.5 GiB of RAM and an 8 GiB swap file sitting on the eMMC. That container's kernel has no zram and no zswap.

The settings Debian ships (`swappiness = 60`, `vfs_cache_pressure = 100`, `page-cluster = 3`) are a general-purpose compromise: they evict anonymous pages even when the machine is fine, and their 32 KiB readahead does nothing for a wider swap disk. This project is the home-made optimization that replaces those values with a policy tuned to the machine's profile: few anonymous pages, a large file cache, and a swap disk that takes 64 KiB reads.

## The idea

Three tiers, modeled on Strata:

1. **RAM** keeps the working set, the pages touched recently.
2. **zram**, when it exists, is the warm tier: cold pages get compressed there. This kernel has none, so the tier stays idle.
3. **The swap file** is the disk archive: it only receives pages when RAM gets tight.

Tier names describe the chosen pressure, not the destination file: the kernel always writes to the highest-priority swap device.

### The six tiers

The level names are the daemon's own labels, in French, as are the reasons it logs.

| Order | Entry condition | Level | swappiness | cache | Reason |
|---|---|---|---|---|---|
| 1 | No swap at all | `sans-swap` | 10 | 100 | tweaking does nothing |
| 2 | Available RAM under 8 %, or PSI `full` `avg10` ≥ 5 | `urgent` | 40 | 150 | only the coldest pages leave |
| 3 | Swap throughput ≥ 40 MB/s and PSI `full` `avg10` ≥ 1, outside emergency | `sature` | 15 | 100 | the disk is already loaded and tasks are stalled |
| 4 | Available RAM under 20 %, or PSI `some` `avg10` ≥ 8, or PSI `full` `avg10` ≥ 1 | `cooled` if zram has ≥ 128 MiB free, else `archive` | 35 or 25 | 80 or 100 | cold pages to zram, or to disk swap |
| 5 | zram ready and available RAM under 40 % | `cooled` | 30 | 80 | zram absorbs lightly used pages |
| 6 | Otherwise | `active` | 10 | 50 | RAM keeps the working set |

First true condition wins. Comparisons are strict: a value exactly on a threshold stays on the calm side.

### Hysteresis

A tier already in effect waits for 5 extra points of available RAM before giving ground, so nothing oscillates at every reading:

| Held tier | Entry threshold | Exit threshold |
|---|---|---|
| `urgent` | available < 8 % | available < 13 % |
| `archive`, or `cooled` at swappiness 35 | available < 20 % | available < 25 % |
| `cooled` at swappiness 30 | available < 40 % | available < 45 % |

### Caps

After the tier is picked, two caps bring `swappiness` back to 10: free swap < 256 MiB (including during emergencies), or anonymous pages < 256 MiB outside emergencies. If a cap actually lowers the value, the announced level becomes `active` and the log shows the swappiness actually written; `vfs_cache_pressure` and `page-cluster` keep the chosen tier's values.

`page-cluster` is 4 whenever disk swap exists (64 KiB read-ahead), 0 if zram is the only swap.

## Install

```sh
install -m 755 ram_swap.py /home/<user>/ram-swap/ram_swap.py
ln -s /home/<user>/ram-swap/ram_swap.py /usr/local/bin/ram-swap-policy
cp ram-swap-policy.service /etc/systemd/system/
systemctl enable --now ram-swap-policy
```

Boot file `/etc/sysctl.d/99-swap-page-cluster.conf`, applied before the service takes over:

```ini
vm.swappiness = 10
vm.vfs_cache_pressure = 50
vm.page-cluster = 4
```

The unit runs `ram-swap-policy --watch --interval 5`, restarts on failure (`Restart=on-failure`, `RestartSec=2`) and starts after `swap.target`. Current state lives in `/run/ram-swap-policy.json`.

## Usage

```text
ram-swap-policy [--dry-run] [--json] [--watch] [--interval 5] [--proc /proc] [--state /run/ram-swap-policy.json]
```

| Option | Effect |
|---|---|
| `--dry-run` | Prints the decision, writes neither the sysctls nor the state |
| `--json` | Prints `level`, `swappiness`, `vfs_cache_pressure`, `page_cluster`, `reason` |
| `--watch` | Loops until signaled. Incompatible with `--dry-run` |
| `--interval` | Seconds between two readings. Must be positive. Default: 5 |
| `--proc` | Root to read instead of `/proc` |
| `--state` | State file instead of `/run/ram-swap-policy.json` |

A log line only comes out when the level, the swappiness, the cache pressure or `page-cluster` changes:

```text
active swappiness=10 cache=50 cluster=4 dispo=2367 Mio swap=6/8191 Mio
```

The line ends with the tier's reason, in French.

`SIGTERM` and `SIGINT` end the loop cleanly, after the interval sleep.

## Tests

```sh
make test
```

Same as `python3 -m unittest test_ram_swap test_bench_reel`. 31 tests: for the tool, `/proc` parsing, the six tiers, both caps, hysteresis on the three RAM thresholds, re-reading an old state file and the round-trip of the new format; for the real-usage harness, quantiles, worker survival on partial memory spans, and the point measurements. Green suite in ~0.5 s.

## Measurement tools

The harnesses shipped with the project:

- `bench_ram_swap.py` measures one version of the module: cost of a read+decision iteration, parse breakdown, latency of a full invocation. Takes a module path to compare two versions: `python3 bench_ram_swap.py /path/to/other_ram_swap.py`
- `bench_systeme.py` measures one configuration under thrash: a churned 4 GiB anonymous hog, random 4 KiB refault latency on a region pushed to swap with `MADV_PAGEOUT` (calibrated against the same pass in RAM), sampling of effective swappiness and PSI at 0.5 s
- `bench_reel.py` measures real usage: background apps (a hot zone rewritten often, a cold zone left alone) fill RAM while the harness records perceived latencies: tool startup, small file save (fsync), disk read throughput
- `bench_sature.py` saturates on purpose: real web design tools (Photopea, Excalidraw) and loaded tabs in headless Chrome, in-RAM document models touched continuously, while three gcc builds have to pass
- `bench_recap.py` summarizes the runs in `resultats/`

## Benchmarks

### Reference machine

- Host: ASUS CM1405 Chromebook, MediaTek Kompanio 540, 8 GB RAM, 128 GB eMMC
- Container: Debian 13 arm64 in Crostini, kernel 6.6, ~6.5 GiB RAM exposed
- Swap: 8 GiB `/swapfile`, priority −2, on `vdb` (the container's disk image, hence the eMMC), shared with the root filesystem
- MGLRU `lru_gen` at `0x0033`, no zram or zswap
- Comparison: the solution (daemon active, adaptive settings) against stock Debian (daemon stopped, `60/100/3`)

### Tool micro-benchmark

On the reference machine, 300 iterations, 15 invocations:

| Measure | Value |
|---|---|
| `/proc` read + decision iteration (median) | ~500 µs, p95 719 µs |
| of which `meminfo` parse | 32 µs |
| of which `pressure` parse | 4 µs |
| of which `vmstat` parse | 66 µs |
| Full `--dry-run --json` invocation (median) | 137 ms, p95 147 ms |
| Bare Python startup (reference) | 43 ms |
| Service in production | 7.6 MiB RSS, ~0.11 % CPU over 2 h 20 |

The useful cost of a cycle is one two-thousandth of the 5 s interval; the 137 ms of a one-shot invocation is almost entirely Python imports, no impact in service mode.

### System benchmark: protocol

Each run, same conditions: cache purge, a 4 GiB anonymous hog touched then churned (2 passes: full write then full read; eviction to swap is the kernel's doing), then a 256 MiB region explicitly pushed to swap with `MADV_PAGEOUT` and re-read in page-by-page random order, calibrated against the same pass in RAM. `pswpin`/`pswpout` counters and PSI totals measured as deltas around each pass. Two runs per configuration, interleaved: solution → native → solution → native.

### Per-run results

**solution-run1** (sysctl start `10/50/4`, end `40/150/4`, tier `urgent`):

| Pass | Duration | Throughput | pswpin | pswpout | PSI some | PSI full |
|---|---|---|---|---|---|---|
| 1 | 221.1 s | 77 MiB/s | 8585 MiB | 8356 MiB | 6.6 s | 6.1 s |
| 2 | 240.5 s | 73 MiB/s | 8851 MiB | 8623 MiB | 6.8 s | 6.5 s |

4 KiB refault: 1.8 µs median, p95 3.2 µs. Ramp-up: 13.0 s.

**natif-run1** (constant sysctl `60/100/3`):

| Pass | Duration | Throughput | pswpin | pswpout | PSI some | PSI full |
|---|---|---|---|---|---|---|
| 1 | 256.2 s | 66 MiB/s | 8587 MiB | 8412 MiB | 5.5 s | 5.2 s |
| 2 | 256.2 s | 65 MiB/s | 8459 MiB | 8281 MiB | 6.9 s | 6.6 s |

4 KiB refault: 1.7 µs median, p95 3.1 µs. Ramp-up: 12.1 s.

**solution-run2** (sysctl start `10/50/4`, end `15/100/4`, tier `sature`):

| Pass | Duration | Throughput | pswpin | pswpout | PSI some | PSI full |
|---|---|---|---|---|---|---|
| 1 | 211.0 s | 80 MiB/s | 8598 MiB | 8291 MiB | 5.4 s | 5.1 s |
| 2 | 248.9 s | 68 MiB/s | 8719 MiB | 8312 MiB | 9.9 s | 9.5 s |

4 KiB refault: 1.8 µs median, p95 2.8 µs. Ramp-up: 12.3 s.

**natif-run2** (constant sysctl `60/100/3`):

| Pass | Duration | Throughput | pswpin | pswpout | PSI some | PSI full |
|---|---|---|---|---|---|---|
| 1 | 278.1 s | 61 MiB/s | 8752 MiB | 8269 MiB | 7.6 s | 7.3 s |
| 2 | 324.7 s | 54 MiB/s | 8948 MiB | 8580 MiB | 9.0 s | 8.6 s |

4 KiB refault: 1.8 µs median, p95 3.5 µs. Ramp-up: 9.8 s.

### Aggregate summary

Pass medians, 2 runs per configuration:

| Metric | Solution | Stock Debian | Gap |
|---|---|---|---|
| 4 GiB churn pass | **230.4 s** | 278.8 s | solution **21 % faster** |
| Effective throughput | **74.4 MiB/s** | 61.7 MiB/s | solution **+20 %** |
| PSI `full` per pass | 6.8 s | 6.9 s | even |
| PSI `some` per pass | 7.2 s | 7.3 s | even |
| 4 KiB refault median | 1.81 µs | 1.73 µs | even (±10 % = noise) |
| Ramp-up | 12.6 s | 10.9 s | native slightly ahead |

### Reading the results

1. **The solution's net gain is sustained throughput.** Both configurations move the same volumes (~8.5 GiB in/out per pass) but the solution moves them 17 to 21 % faster. The identified factor is `page-cluster`: 64 KiB read-ahead against 32 KiB stock. An earlier measurement on the same hardware gave ~220 MB/s sequential swap-in at cluster 4 against ~137 MB/s at cluster 3, same direction.
2. **Stock degrades, the solution holds.** Native passes slow down run after run (256 → 278 → 325 s) while the solution stays in a narrow band (211-249 s). The adaptive strategy varied with the storm's profile: tier `urgent` (40/150) on the first run, tier `sature` (15/100) on the second, when swap is already moving too much and tasks are stalled.
3. **Interactivity (PSI) is even** under this extreme thrash, and so is random refault: no invented gains where there are none. The daemon's calm value (swappiness 10 instead of 60 day to day) is not exercised by this test; it rests on the machine's profile (~133 MiB of anonymous pages, a cap that holds 10 outside emergencies).

### Caveats

2 runs × 2 passes per configuration: the pattern holds across all four pairings, but the sample is short. The swap disk is shared with the container's root, so host noise is possible. The test forces a thrash this machine rarely reaches in real use.

### Real usage: constrained desktop, perceived latencies

The forced thrash above says nothing about daily life. This second protocol fills RAM like open apps, each with a hot zone rewritten every 150 ms and a cold zone left alone, and takes 50 measurements, every 2 s, of what a user feels: starting a Python interpreter, saving a 64 KiB file with `fsync` on the eMMC, disk read throughput. The harness aborts if a worker dies: the load has to hold from start to finish. Two scenarios, two interleaved solution/native pairs, 50 samples per run.

**Moderate scenario**: 2 apps × 600 MiB, median available RAM 9-10 %. Note: this pair is order-biased (no swap reset between them; native inherited the margin freed by the solution's swap-out, which ran just before). The numbers are published for what they are:

| Metric (median) | Solution (`urgent` 40/150) | Native (60/100/3) |
|---|---|---|
| Tool startup | 52.1 ms | 51.0 ms |
| fsync save | 16.4 ms (p95 170, max 304) | 12.7 ms (p95 19.8, max 38) |
| Disk read | 584 MiB/s | 725 MiB/s |
| Swap in / out | 42 / **387 MiB** | 18 / 0 MiB |
| PSI some / full | 1.8 / 1.4 s | 0.1 / 0.1 s |

**Loaded scenario**: 3 apps × 500 MiB, median available RAM 2.5-4 %, swap reset before each run (clean pair):

| Metric (median) | Solution (`urgent` 40/150) | Native (60/100/3) |
|---|---|---|
| Tool startup | 50.2 ms | 49.7 ms |
| fsync save | **12.8 ms** (p95 24.5) | 15.6 ms (p95 27.5, max 137) |
| Disk read | **777 MiB/s** | 667 MiB/s |
| Swap in / out | 0 / **176 MiB** | 0 / 241 MiB |
| PSI some / full | 0.1 / 0.1 s | 0.1 / 0.1 s |

**Reading.**

1. The clean pair (loaded scenario) has the solution better or equal on everything perceptible: saves 18 % faster, reads +16 %, 27 % fewer pages moved to swap, same near-absence of stalls. Tool startup is even (1 %).
2. The moderate pair, order-biased, still says one true thing: the daemon's `urgent` mode (swappiness 40, cache 150) can pay fsync spikes (p95 170 ms) when aggressive eviction and eMMC I/O overlap. That is the real cost of its responsiveness, published as is.
3. In both solution runs, the daemon detected real pressure and climbed to `urgent` (10→40, cache 150), then held by hysteresis. On native, nothing reacts: values stay 60/100/3 from start to finish.
4. Method correction, published for honesty's sake: the first version of this protocol measured a near-idle machine. Workers died ~150 ms in (an `mmap` partial-span bug), the load never held, and the "neither config swaps" numbers described nothing. The harness is fixed (exact-length writes, cross-process stop, survival guard) and covered by 9 dedicated tests.

### Creative saturation: web design + tabs + a dev build

The question: can the machine and the swap hold a demanding creative workload? Scenario: 6 headless Chrome tabs (Photopea, two Excalidraw, three loaded Wikipedia articles), three 600 MiB "document models" touched continuously (a design file's layers), and meanwhile three gcc builds of 80 files that have to pass. Swap reset before each run, 8 GB RAM against 8 GiB of swap.

| Metric | Solution (`urgent` 40 → `archive` 25) | Native (60/100/3) |
|---|---|---|
| Builds passing | 3/3 | 3/3 |
| Build durations | 19.6 / 10.6 / **8.3 s** | 20.1 / 9.9 / 10.9 s |
| fsync save (median / p95) | **62.1 ms / 205 ms** | 68.4 ms / 319 ms |
| Tool startup | 85.5 ms | 85.6 ms |
| Disk read | 402 MiB/s | 437 MiB/s |
| Swap in / out | 179 / 2,850 MiB | 93 / 2,811 MiB |
| PSI some / full | 10.4 / 4.4 s | 7.5 / 4.1 s |
| OOM | 0 | 0 |
| Minimum available RAM | 9.4 % | 9.4 % |

Both configurations hold the scenario: no OOM, every build passes, every process survives. The solution keeps two small advantages deep in the saturation: the third build (8.3 s against 10.9 s) and the save tail (p95 36 % better), consistent with the 64 KiB read-ahead measured above; native keeps slightly lower accumulated micro-stalls. The daemon followed the pressure curve (`urgent` 40 at the peak, back to `archive` 25 after) while native stayed frozen at 60.

One pair of runs only: indicative reading, not statistics.

### Where the three regimes land

| Regime | Verdict |
|---|---|
| Calm to moderate daily use (real usage, available > 9 %) | Even: neither config swaps much, perceived latencies indistinguishable. The solution degrades nothing and stays at swappiness 10 while the machine breathes. |
| Shortage (forced churn, thrash) | **Solution +20 % sustained throughput** (74.4 against 61.7 MiB/s) thanks to 64 KiB read-ahead; native degrades pass after pass, the solution holds its band. |
| Creative saturation (web design + tabs + builds) | **Both hold**: zero OOM, all builds pass, ~2.8 GiB pushed into swap. Small solution edges deep in (build 8.3 s against 10.9 s, fsync p95 36 % better), two small native edges (PSI some, disk read). |
| Known cost of the solution | `urgent` mode (swappiness 40, cache 150) can pay fsync spikes under concurrent eMMC I/O. Published as is. |

The daemon changes nothing while RAM is enough. Once the margin tightens, it adapts the values (10, 25 or 40 depending on tier, an anti-thrash guard at 15) where Debian always keeps 60.

## License

MIT, see [LICENSE](LICENSE).
