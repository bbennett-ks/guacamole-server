# monitor-resources.py

> **Tip:** This is Markdown. For nicer reading (rendered headings, tables, a
> clickable TOC), view it on GitHub or upload it into an online Markdown viewer
> like <https://markdown.readlocal.app/>.

## Contents

- [Summary](#summary)
- [Usage: command-line arguments](#usage-command-line-arguments)
- [Overview of environment variables](#overview-of-environment-variables)
- [Column selection](#column-selection)
- [Interactive mode](#interactive-mode)
- [Metric definitions](#metric-definitions)
  - [System metrics (`--system`)](#system-metrics---system)
  - [Process metrics (`--pid`)](#process-metrics---pid)
  - [Health flags](#health-flags)
- [Summary statistics](#summary-statistics)
  - [Understanding percentiles](#understanding-percentiles)
- [JSON output](#json-output)
  - [File layout and naming](#file-layout-and-naming)
  - [System samples (detail)](#system-samples-detail)
  - [Process samples (detail)](#process-samples-detail)
  - [Summary file](#summary-file)
  - [Extracting and visualizing data](#extracting-and-visualizing-data)
- [Examples](#examples)
- [Debugging performance issues](#debugging-performance-issues)
  - [A workflow](#a-workflow)
  - [CPU](#cpu)
  - [Memory](#memory)
  - [I/O and network](#io-and-network)
  - [Threads, descriptors, and process state](#threads-descriptors-and-process-state)
- [guacd thread names](#guacd-thread-names)
  - [guacd (core daemon)](#guacd-core-daemon)
  - [libguac (shared client library)](#libguac-shared-client-library)
  - [Protocol clients](#protocol-clients)

## Summary

`monitor-resources.py` samples system and/or per-process resource usage at a
fixed interval and writes a fixed-width log file (and optionally JSON) you can
dig into later. Use it to track down CPU, memory, I/O, file-descriptor, and
thread problems over time.

It's *mostly* **cross-platform**: system and process metrics go through `psutil`,
so they work anywhere `psutil` does. Thread metrics, kernel stacks, and per-task
page faults / context switches come from `/proc`, so they quietly do nothing
where there's no `/proc` (Windows, macOS) — the `-t` and `-s` flags still parse,
they just produce no data. On Windows, FD counts become handle counts.

**Note:** Not actually tested on macOS or Windows.

What it can sample:

- **System-wide** (`-S`/`--system`, the default when you don't pass `-p`):
  CPU%, USR%, SYS%, BUSY core count, context switches/s, 1-minute load average,
  IOWAIT%, memory % and GB, swap GB, disk read/write MB/s, network receive/send
  MB/s, and used GB per filesystem in `RES_MON_FILESYSTEMS`.
- **Per-process** (`-p PID`, repeatable): CPU%/USR%/SYS%, cumulative user/system
  seconds, RSS/VMS MB, thread count, voluntary/involuntary context switches/s,
  minor/major page faults/s, disk read/write MB/s, open FDs, plus a `HEALTH`
  column with stateless flags (CPU/ZMB/DSK/MJF/IVX) and trend flags
  (MLK/FDL/THD) that compare against an early baseline sample.
- **Descendants** (`-d`/`--descendants`): picks up new descendants of each `-p`
  target every interval, attributed flat under the root.
- **Per-thread** (`-t`/`--thread`, needs `-p`): one row per thread under each
  process, sorted by CPU% with idle threads filtered out.
- **Kernel stacks** (`-s`/`--stack`): dumps `/proc/<pid>/stack` (or the per-task
  stack) under any row whose CPU% is at or above `RES_MON_CPU_HIGH_THRESH`.

Output goes to three independent streams you can mix and match: a fixed-width log
file, stdout (a plain mirror or a `top(1)`-style redraw), and JSON
(`detail`/`summary`). On POSIX you need sudo/root — without it the kernel may
block `/proc/<pid>/io`, `/proc/<pid>/stack`, descendant discovery, and sampling
other users' processes.

## Usage: command-line arguments

```
monitor-resources.py [options]
```

**What to monitor**

| Option | Description |
| --- | --- |
| `-S`, `--system` | Monitor system-wide resources. Assumed when neither `-S` nor `-p` is given. |
| `-p`, `--pid <pid>` | Monitor a specific process by PID. May be repeated for multiple processes. |
| `-d`, `--descendants` | Also monitor descendants of each `-p` PID, at any depth. |
| `-t`, `--thread` | List each thread under its process. Requires `-p`. |
| `-s`, `--stack` | Show the kernel stack under each process row (and, with `-t`, each thread row) whose CPU% ≥ `RES_MON_CPU_HIGH_THRESH`. Stack reads typically need `CAP_SYS_PTRACE`; missing/empty stacks are skipped. |

**Sampling**

| Option | Description |
| --- | --- |
| `-i`, `--interval <sec>` | Sampling interval in seconds (overrides `RES_MON_INTERVAL_SEC`). |
| `-I`, `--interactive` | Read single-key commands from stdin while running. Requires a POSIX TTY; silently falls back to plain sleep on Windows or when stdin isn't a TTY. Keys: `d` = toggle `--descendants`, `t` = toggle `--thread`, `s` = toggle `--stack`, `+` = interval +5s, `-` = interval −5s (min 5s), `?` = re-print keys. Changes take effect at the next sample. |

**Output streams**

| Option | Description |
| --- | --- |
| `-D`, `--display` | Also mirror log output to stdout. |
| `-T`, `--top` | `top(1)`-style display: clear the screen and re-emit the header before each iteration's stdout output. Implies `--display`; the log file is unaffected. |
| `-l`, `--log` | Write the fixed-width log file at `RES_MON_LOG_DIR/RES_MON_LOG_PREFIX_monres.log`. Off by default (same as `RES_MON_LOG=1`). `-j`/`--json` still emits its JSON files even when `-l` is off. |
| `-j`, `--json` | Also write `<log>.detail.json` (per-sample arrays) and `<log>.summary.json` (min/avg/p50/p95/p99/max stats). Honors `--detail-only`/`--summary-only` (same as `RES_MON_JSON=1`). |

**Output content**

| Option | Description |
| --- | --- |
| `--timestamp` | Include a `-YYYYMMDD-HHMMSS` timestamp in the log file name (default: off). |
| `--columns <keys>` | Comma/space-separated list of metric columns to display, or `all`. Filters both the periodic detail rows and the end-of-run summary. The identity columns (`TIME`, `ELAPSED`, `PID`, `NAME`) and the per-filesystem `*_GB` columns are always shown, and JSON output always carries every metric. Overrides `RES_MON_COLUMNS`. See [Column selection](#column-selection) for valid keys. |
| `--detail-only` | Write only the periodic detail rows; omit the end-of-run summary. Overrides `RES_MON_DETAIL_ONLY`; mutually exclusive with `--summary-only`. |
| `--summary-only` | Write only the end-of-run summary; omit headers and periodic rows. Overrides `RES_MON_SUMMARY_ONLY`; mutually exclusive with `--detail-only`. |

**Other**

| Option | Description |
| --- | --- |
| `-h`, `--help` | Show the full help message, field reference, host hardware summary, and current environment-variable values. |

Notes:

- No `-S`/`--system` and no `-p`/`--pid`? You get `--system`.
- You can combine them: system and process stats land on one line split by `|`.
  Multiple `-p` gives you one row per process per interval.
- Windows has no EUID, so the root check is skipped and `/proc`-dependent
  features (thread metrics, kernel stacks) produce no data.

## Overview of environment variables

These override the defaults at startup. Run `-h`/`--help` to see their current
values.

| Variable | Default | Purpose |
| --- | --- | --- |
| `RES_MON_LOG_DIR` | current working dir | Directory for the log/JSON output files. |
| `RES_MON_LOG_PREFIX` | `resource-monitor` | Filename prefix; the log becomes `<prefix>_monres.log`. |
| `RES_MON_INTERVAL_SEC` | `60` | Sampling interval in seconds (overridden by `-i`). |
| `RES_MON_CPU_HIGH_THRESH` | `80` | CPU% threshold for the `BUSY` core count, the `CPU` health flag, and stack dumps. |
| `RES_MON_MEM_HIGH_THRESH` | `90` | High memory-usage threshold (%). |
| `RES_MON_IOWAIT_HIGH_THRESH` | `10` | High IOWAIT threshold (%). |
| `RES_MON_FLUSH_EACH_LINE` | `1` | When `1`, flush the log after every line. |
| `RES_MON_FILESYSTEMS` | `/ /home` | Space-separated filesystem paths to report used-GB columns for. |
| `RES_MON_IVX_HIGH_THRESH` | `500` | Involuntary context switches/s threshold for the `IVX` health flag. |
| `RES_MON_HEALTH_MIN_SAMPLES` | `3` | Samples required before trend health flags (MLK/FDL/THD) can trigger. |
| `RES_MON_DETAIL_ONLY` | `0` | When `1`, skip the end-of-run summary (same as `--detail-only`). |
| `RES_MON_SUMMARY_ONLY` | `0` | When `1`, skip headers and periodic rows (same as `--summary-only`). |
| `RES_MON_STACK` | `0` | When `1`, show kernel stacks for hot threads (same as `-s`/`--stack`). |
| `RES_MON_JSON` | `0` | When `1`, also write `.detail.json`/`.summary.json` (same as `-j`/`--json`). |
| `RES_MON_LOG` | `0` | When `1`, write the fixed-width log file (same as `-l`/`--log`). |
| `RES_MON_COLUMNS` | _(all)_ | Comma/space-separated list of metric columns to display (same as `--columns`). Unset or `all` shows everything. See [Column selection](#column-selection). |

## Column selection

By default you get every column. `--columns` (or `RES_MON_COLUMNS`) trims both
the periodic detail rows **and** the end-of-run summary down to the keys you
pick. Pass a comma- and/or space-separated list of the keys below
(case-insensitive); `all` or an empty value means "everything". An unknown key
errors out and lists the valid ones.

Always shown no matter what: `TIME`, `ELAPSED`, `PID`, `NAME`, and the
per-filesystem `*_GB` columns (set by `RES_MON_FILESYSTEMS`). JSON (`-j`/`--json`)
always has every metric — `--columns` only touches the text/log display.

Keys apply to whichever view defines them; a few (`cpu`, `usr`, `sys`, `vctx`,
`ivctx`, `minflt`, `majflt`) are shared between the system and process views.

**System view (`--system`):**
`cpu`, `usr`, `sys`, `busy`, `ctx`, `load1`, `iowait`, `mem`, `mem_gb`, `swp`,
`dsk_r`, `dsk_w`, `net_r`, `net_s`

**Process view (`--pid`):**
`state`, `health`, `cpu`, `usr`, `sys`, `user_sec`, `sys_sec`, `rss`, `vms`,
`threads`, `vctx`, `ivctx`, `minflt`, `majflt`, `read`, `write`, `fds`

**Thread view (`--thread`)** reuses the process keys; columns that don't make
sense per-thread (`user_sec`, `sys_sec`, `rss`, `vms`, `threads`, `read`,
`write`, `fds`) show up as `-`.

## Interactive mode

With `-I`/`--interactive`, the tool reads single keypresses from stdin while it
runs, so you can change what's shown without stopping and restarting. Pair it
with `--display` or `--top` to watch the effect live; the keystrokes don't touch
the log or JSON streams.

Keys are read in cbreak mode (no Enter needed) and handled while the sampler
sleeps between intervals, so a change **kicks in at the next sample**, not
mid-interval.

| Key | Action |
| --- | --- |
| `d` | Toggle descendant monitoring (`--descendants`) on/off. |
| `t` | Toggle per-thread rows (`--thread`) on/off. |
| `s` | Toggle kernel-stack dumps for hot rows (`--stack`) on/off. |
| `+` | Increase the sampling interval by 5 seconds. |
| `-` | Decrease the sampling interval by 5 seconds (floor 5s). |
| `?` | Re-print the key banner. |

A banner of the keys prints to stderr when interactive mode starts, and again
whenever you press `?`. After each accepted change the tool echoes the new state
(interval, threads, descendants, stack) to stderr.

**Requirements and fallback.** Interactive mode needs a POSIX TTY on stdin
(`termios` plus `select()` on the console fd). On Windows, or when stdin comes
from a file or pipe, the tool prints a notice and falls back to a plain timed
sleep — sampling keeps going, just without the live controls. Your terminal's
original mode is restored on exit.

## Metric definitions

Each selectable metric's `--columns` key is in parentheses. `TIME(LOCAL)`,
`ELAPSED`, `PID`, and `NAME` are identity/timestamp columns and are always
present.

### System metrics (`--system`)

| Column | Key | Description |
| --- | --- | --- |
| `TIME(LOCAL)` |: | Sample's absolute wall-clock time in the local timezone. |
| `ELAPSED` |: | Time since monitoring started (`HH:MM:SS`). |
| `CPU%` | `cpu` | Average CPU utilization across all logical cores (0–100%). |
| `USR%` | `usr` | Share of CPU time spent in user space. |
| `SYS%` | `sys` | Share of CPU time spent in kernel space (syscalls, locking, I/O paths). |
| `BUSY` | `busy` | Count of logical cores at or above `RES_MON_CPU_HIGH_THRESH` (default 80%) this interval: how many cores are saturated, not just the average. |
| `CTX_SW/s` | `ctx` | System-wide context switches per second. Very high rates suggest scheduling churn or oversubscription. |
| `LOAD1` | `load1` | 1-minute load average. Roughly equal to the logical core count means full utilization; higher means runnable work is queuing. |
| `IOWAIT%` | `iowait` | Share of CPU time idle while waiting on outstanding disk I/O. Sustained high values indicate an I/O bottleneck. |
| `MEM%` | `mem` | Percentage of physical RAM in use. |
| `MEM_GB` | `mem_gb` | Absolute physical RAM in use, in GB. |
| `SWP_GB` | `swp` | Swap space currently in use, in GB. Growth here while `MEM%` is high indicates memory pressure. |
| `DSK_R/s` | `dsk_r` | System-wide disk read throughput (MB/s) over the interval. |
| `DSK_W/s` | `dsk_w` | System-wide disk write throughput (MB/s) over the interval. |
| `NET_R/s` | `net_r` | Network receive throughput (MB/s) over the interval, summed across interfaces. |
| `NET_S/s` | `net_s` | Network send throughput (MB/s) over the interval, summed across interfaces. |
| `<FS>_GB` |: | Absolute used disk space (GB) for each filesystem in `RES_MON_FILESYSTEMS` (e.g. `ROOT_GB`, `HOME_GB`). Matches `df` usage. Always shown. |

### Process metrics (`--pid`)

| Column | Key | Description |
| --- | --- | --- |
| `TIME(LOCAL)` |: | Sample's absolute wall-clock time in the local timezone. |
| `ELAPSED` |: | Time since monitoring started (`HH:MM:SS`). |
| `PID` |: | Process ID. For thread rows (`--thread`) this column holds the thread's TID instead. |
| `NAME` |: | Process name plus a truncated copy of its command-line arguments (helps tell apart children that share an executable name). Tree connectors (`┣━`/`┗━`) prefix descendants when `--descendants` is used. |
| `STATE` | `state` | OS run state: `running`, `sleeping`, `disk-sleep` (uninterruptible, usually blocked in I/O), `zombie`, `stopped`, etc. |
| `HEALTH` | `health` | Comma-separated diagnostic flags, or `-` when healthy. See [Health flags](#health-flags). |
| `CPU%` | `cpu` | Total CPU usage of the process across all cores (can exceed 100% for multithreaded processes). |
| `USR%` | `usr` | Process CPU time spent in user space. |
| `SYS%` | `sys` | Process CPU time spent in kernel space. |
| `USER_SEC` | `user_sec` | Cumulative user-space CPU time consumed since the process started, in seconds. |
| `SYS_SEC` | `sys_sec` | Cumulative kernel-space CPU time consumed since the process started, in seconds. |
| `RSS_MB` | `rss` | Resident set size: physical RAM the process occupies, in MB. |
| `VMS_MB` | `vms` | Virtual memory size: total address space mapped, in MB. |
| `THREADS` | `threads` | Number of threads in the process. |
| `VCTX/s` | `vctx` | Voluntary context switches per second (the process yielded: typically waiting on a lock, condition variable, or sleep). |
| `IVCTX/s` | `ivctx` | Involuntary context switches per second (the scheduler preempted it: high values suggest CPU oversubscription). |
| `MINFLT/s` | `minflt` | Minor page faults per second (memory mapped without disk I/O). High with stable `RSS_MB` indicates memory churn. |
| `MAJFLT/s` | `majflt` | Major page faults per second (faults requiring disk I/O). Any sustained value means the process is paging: not enough RAM. |
| `READ_MB/s` | `read` | Disk read throughput attributed to the process (MB/s). |
| `WRITE_MB/s` | `write` | Disk write throughput attributed to the process (MB/s). |
| `FDs` / `HND` | `fds` | Open file descriptors (POSIX) or kernel handles (Windows). A steady climb indicates a descriptor leak. |

Thread rows (`--thread`) reuse this layout: `PID` shows the TID, and columns
that don't make sense per-thread (`USER_SEC`, `SYS_SEC`, `RSS_MB`, `VMS_MB`,
`THREADS`, `READ_MB/s`, `WRITE_MB/s`, `FDs`) show up as `-`.

### Health flags

The `HEALTH` column is a comma-separated set of flags, or `-` when all's well.
Stateless flags come straight from the current sample; trend flags are
process-only and only kick in after `RES_MON_HEALTH_MIN_SAMPLES` samples
(default 3), comparing against the first sample so normal startup growth doesn't
trip them.

**Stateless flags** (also apply to thread rows):

| Flag | Meaning | Triggers when |
| --- | --- | --- |
| `CPU` | High CPU usage | `CPU%` ≥ `RES_MON_CPU_HIGH_THRESH` (default 80%). |
| `ZMB` | Zombie process | Process state is `zombie` (exited but not yet reaped by its parent). |
| `DSK` | Blocked on I/O | Process state is `disk-sleep` (uninterruptible sleep, usually stuck in disk I/O). |
| `MJF` | Major page faults | `MAJFLT/s` > 0: the process is paging from disk (memory pressure). |
| `IVX` | Scheduler pressure | `IVCTX/s` ≥ `RES_MON_IVX_HIGH_THRESH` (default 500): frequent involuntary preemption, a sign of CPU oversubscription. |

**Trend flags** (process rows only):

| Flag | Meaning | Triggers when |
| --- | --- | --- |
| `MLK` | Possible memory leak | `RSS_MB` grows past `max(baseline × 1.10, baseline + 50 MB)`: sustained resident-memory growth. |
| `FDL` | Possible FD leak | Open FDs grow past `max(baseline × 1.25, baseline + 16)`: file descriptors not being released. |
| `THD` | Thread growth | Thread count grows past `max(baseline × 1.25, baseline + 8)`: unbounded thread creation. |

## Summary statistics

When a run ends (exit, Ctrl-C, or all monitored processes gone) the tool prints
a summary block: a timing header (start, end, elapsed) followed by one line per
metric, each aggregating every sample from the run. You get one block for the
system and one per monitored process. `--columns` picks which metric lines show
up, and `--summary-only` / `--detail-only` decide whether you get the summary,
the periodic rows, or both.

Each metric line looks like:

```
CPU%            min     0.12 | avg     3.46 | p50     2.71 | p95     8.08 | p99     8.60 | max     8.73
```

| Statistic | Meaning |
| --- | --- |
| `min` | Smallest value observed across all samples. |
| `avg` | Arithmetic mean (average) of all samples. |
| `p50` | 50th percentile, i.e. the **median**. |
| `p95` | 95th percentile. |
| `p99` | 99th percentile. |
| `max` | Largest value observed across all samples. |

### Understanding percentiles

A percentile `pNN` is the value **at or below which NN% of the samples fall**.
So `p95 8.08` for `CPU%` means 95% of the CPU% samples were ≤ 8.08 and only the
worst 5% went higher. `p50` is just the median — half the samples below, half
above.

Percentiles aren't "the top NN% of values" and they aren't a percentage of
anything: `p95` of `CPU%` is itself a CPU% reading, `p95` of `RSS_MB` is an RSS
value in MB, etc. The unit always matches the metric.

Why bother alongside `avg`? Because the average hides spikes — a process can
average 3% CPU yet briefly peak near saturation. Percentiles expose that **tail
behavior**: `p95`/`p99` show how bad the typical worst case is, `max` shows the
single most extreme sample (maybe a one-off). A big gap between `p50` and `p99`
means the metric is bursty, not steady.

The same stats show up in `summary.json` under each metric, as the keys `min`,
`avg`, `p50`, `p95`, `p99`, `max`, plus `n` (the sample count).

## JSON output

With `-j`/`--json` (or `RES_MON_JSON=1`) the tool writes machine-readable JSON
next to the human-readable log — handy for archiving, diffing runs, or feeding
plotting/analysis tools. JSON doesn't depend on the log file: `-j` works without
`-l`, deriving its path from where the log *would* go. The paths are echoed to
stderr at exit (`Monitor JSON: ...`).

You get two files:

- **`<base>.detail.json`**: every sample (the time series).
- **`<base>.summary.json`**: the min/avg/p50/p95/p99/max aggregates.

`--detail-only` writes just the detail file; `--summary-only` just the summary
(same as for the text output). Unlike the text display, **JSON always has every
metric** — `--columns` doesn't filter it. Per-thread rows and kernel stacks
aren't serialized to JSON; only system and per-process metrics are.

### File layout and naming

`<base>` is `RES_MON_LOG_DIR/RES_MON_LOG_PREFIX[-timestamp]_monres` — the log
path with its `.log` extension swapped out. With the defaults that's
`./resource-monitor_monres.detail.json` and `…summary.json`; `--timestamp` tacks
`-YYYYMMDD-HHMMSS` onto the prefix.

Both files start with the same `metadata` block describing the run:

```json
{
  "metadata": {
    "hostname": "build01",
    "cpu_count": 16,
    "total_memory_gb": 31.1,
    "interval_sec": 5.0,
    "system": true,
    "pids": [4321],
    "filesystems": ["ROOT", "HOME"],
    "cpu_high_thresh": 80.0
  },
  "system": [ /* samples or summary, see below */ ],
  "processes": [ /* per-process objects */ ]
}
```

`system` is `null` (summary) or `[]` (detail) when `--system` wasn't active;
`processes` is empty when you didn't pass `-p`.

### System samples (detail)

In `detail.json`, `system` is an array with one object per interval. Each field
is the same metric you see in the text columns (see
[Metric definitions](#metric-definitions)); `time`/`elapsed` are strings and
`filesystem_usage_gb` is keyed by the filesystem labels from `metadata`:

```json
{
  "time": "12:00:01",
  "elapsed": "0:00:02",
  "cpu_percent": 23.4,
  "usr_percent": 15.0,
  "sys_percent": 8.4,
  "busy_cores": 1,
  "ctx_switches": 4210,
  "load1": 1.2,
  "iowait_percent": 0.5,
  "mem_percent": 37.0,
  "mem_gb": 7.25,
  "swap_used_gb": 0.0,
  "disk_read_mb": 0.8,
  "disk_write_mb": 2.1,
  "net_recv_mb": 0.3,
  "net_send_mb": 0.4,
  "filesystem_usage_gb": { "ROOT": 55.15, "HOME": 134.2 }
}
```

### Process samples (detail)

In `detail.json`, `processes` is an array of one object per monitored process
(each `-p` target and, with `--descendants`, every descendant found). Each object
carries identity fields plus a `samples` array of per-interval readings.
Descendants have `"is_child": true` and a `parent_pid` pointing at their real
parent:

```json
{
  "pid": 4321,
  "name": "guacd",
  "is_child": false,
  "parent_pid": 0,
  "samples": [
    {
      "time": "12:00:01",
      "elapsed": "0:00:02",
      "pid": 4321,
      "name": "guacd",
      "state": "running",
      "health": "CPU,MJF",
      "cpu_percent": 91.2,
      "usr_percent": 80.0,
      "sys_percent": 11.2,
      "user_sec": 12.5,
      "sys_sec": 3.3,
      "rss_mb": 512.0,
      "vms_mb": 1024.0,
      "threads": 8,
      "vctx_per_sec": 10.0,
      "ivctx_per_sec": 2.0,
      "minflt_per_sec": 5.0,
      "majflt_per_sec": 1.0,
      "read_mb_per_sec": 0.0,
      "write_mb_per_sec": 2.0,
      "fds": 42
    }
  ]
}
```

`health` is the same comma-separated flag string as the text column (or `"-"`) —
see [Health flags](#health-flags).

### Summary file

`summary.json` swaps the time series for aggregates. `system` is a single object
(or `null`) and `processes` is an array of per-process objects that also carry
`pid`/`name`/`is_child`/`parent_pid`. Every metric is a stats block:

```json
{
  "system": {
    "cpu_percent": { "n": 5, "min": 12.0, "avg": 39.74, "p50": 30.1, "p95": 79.44, "p99": 86.29, "max": 88.0 },
    "mem_gb":      { "n": 5, "min": 7.14, "avg": 7.30, "p50": 7.27, "p95": 7.38, "p99": 7.39, "max": 7.40 },
    "filesystem_usage_gb": {
      "ROOT": { "n": 5, "min": 55.15, "avg": 55.15, "p50": 55.15, "p95": 55.15, "p99": 55.15, "max": 55.15 }
    }
  },
  "processes": [
    {
      "pid": 4321, "name": "guacd", "is_child": false, "parent_pid": 0,
      "rss_mb": { "n": 5, "min": 480.0, "avg": 505.2, "p50": 510.0, "p95": 520.0, "p99": 521.0, "max": 521.0 }
    }
  ]
}
```

A metric with no samples collapses to `{ "n": 0 }`. See
[Summary statistics](#summary-statistics) for what the percentiles mean.

### Extracting and visualizing data

[`jq`](https://jqlang.github.io/jq/) is the quickest way to slice the files.
Assuming the defaults wrote `resource-monitor_monres.detail.json` and
`…summary.json`:

```sh
# Pretty-print / browse the whole document
jq . resource-monitor_monres.detail.json

# Run metadata at a glance
jq .metadata resource-monitor_monres.summary.json

# Peak system CPU% observed during the run
jq '[.system[].cpu_percent] | max' resource-monitor_monres.detail.json

# System CPU% summary block (min/avg/p50/p95/p99/max)
jq '.system.cpu_percent' resource-monitor_monres.summary.json

# Tail latency per process: name and p99 RSS, as CSV
jq -r '.processes[] | [.name, .rss_mb.p99] | @csv' resource-monitor_monres.summary.json

# Every sample where a process raised a health flag
jq -r '.processes[] | .name as $n
       | .samples[] | select(.health != "-")
       | [.time, $n, .health, .cpu_percent] | @csv' resource-monitor_monres.detail.json
```
Export to CSV format for other tools:
```
# One process's RSS time series → CSV for plotting
jq -r '.processes[] | select(.pid == 4321)
       | .samples[] | [.elapsed, .rss_mb] | @csv' \
   resource-monitor_monres.detail.json > rss.csv

# System CPU% time series → CSV (header + rows)
{ echo "elapsed,cpu_percent";
  jq -r '.system[] | [.elapsed, .cpu_percent] | @csv' resource-monitor_monres.detail.json
} > cpu.csv
```

## Examples

The examples below were captured against a live `guacd` daemon running as PID
`119479` (`-i 2` for a quick demo; the default interval is 60s). `guacd` forks
one child per connection, so `pidof guacd` returns *several* PIDs — grab the root
of the tree (the `guacd` whose parent isn't another `guacd`), e.g. the one
listening on port 4822, and pass that one as `--pid`:

```sh
sudo ss -lptnH 'sport = :4822' | grep -oP 'pid=\K[0-9]+'   # -> 119479
```

Sample output is trimmed to a few rows; rows are wide, so scroll right.

**System-wide stats only (the default).** The end-of-run summary (printed on
Ctrl-C/exit) is shown trimmed:

```sh
monitor-resources.py --system --interval 2
```
```
TIME(LOCAL)    ELAPSED     CPU%     USR%     SYS%   BUSY   CTX_SW/s    LOAD1   IOWAIT%     MEM%     MEM_GB     SWP_GB    DSK_R/s    DSK_W/s    NET_R/s    NET_S/s    ROOT_GB    HOME_GB
   12:05:58    0:00:02     11.9      5.2      6.3      0      17036     1.11      0.00     41.5       8.81       0.00       0.00       0.18       0.01       0.01      58.00     134.96
   12:06:00    0:00:04     15.0      6.1      8.5      0      16162     1.11      0.00     41.7       8.81       0.00       0.00       0.03       0.03       0.01      58.00     134.96
   12:06:02    0:00:06     13.4      5.8      7.0      0      16512     1.11      0.00     41.7       8.81       0.00       0.00       1.01       2.21       0.01      58.00     134.96

SUMMARY:
   Start Time (Local): 2026-06-01 12:05:56 EDT
   End Time (Local):   2026-06-01 12:06:03 EDT
   Elapsed Time:       0:00:06

   CPU%            min    11.90 | avg    13.43 | p50    13.40 | p95    14.84 | p99    14.97 | max    15.00
   MEM (GB)        min     8.81 | avg     8.81 | p50     8.81 | p95     8.81 | p99     8.81 | max     8.81
   ...
```

**System stats plus the guacd process.** Combined views put system and process
columns on one line, split by `|`:

```sh
monitor-resources.py --system --pid 119479 --interval 2
```
```
TIME(LOCAL)    ELAPSED     CPU%     USR% ...    HOME_GB |      PID NAME                                   STATE     HEALTH    CPU%     USR%     SYS%   USER_SEC    SYS_SEC     RSS_MB     VMS_MB  THREADS ...   FDs
   12:12:31    0:00:02     20.7      8.5 ...     134.96 |   119479 guacd -f -b 127.0.0.1 -l 4822 -L debug sleeping  -          0.0      0.0      0.0        0.3        3.2       17.2     2069.9       20 ...    24
   12:12:33    0:00:04     26.1     10.8 ...     134.96 |   119479 guacd -f -b 127.0.0.1 -l 4822 -L debug sleeping  -          0.0      0.0      0.0        0.3        3.2       17.2     2069.9       20 ...    24
```

**guacd, its descendants, and their threads.** Descendants are drawn as a tree
(`┣━`/`┗━`); thread rows hang under each process with their `comm` name (see
[guacd thread names](#guacd-thread-names)) and `-` in columns that have no
per-thread meaning:

```sh
monitor-resources.py --pid 119479 --descendants --thread --interval 2
```
```
TIME(LOCAL)    ELAPSED      PID NAME                                          STATE        HEALTH    CPU%     USR%     SYS%   USER_SEC    SYS_SEC     RSS_MB     VMS_MB  THREADS    VCTX/s   IVCTX/s ...   FDs
   12:13:01    0:00:02   119479 guacd -f -b 127.0.0.1 -l 4822 -L debug        sleeping     -          0.0      0.0      0.0        0.3        3.2       17.2     2069.9       20       0.0       0.0 ...    24
   12:13:01    0:00:02   122657 ┣━ vnc bbennett@192.168.68.50:5900            sleeping     -          0.5      0.5      0.0      191.5       10.6      152.4     2103.9       23       0.0       0.0 ...    24
   12:13:01    0:00:02   136308 ┣━ ssh bbennett@192.168.238.152:22            sleeping     -          0.0      0.0      0.0        0.2        0.3       37.7     2147.3        9       0.0       0.0 ...    32
   12:13:01    0:00:02   136332 ┣━ telnet bbennett@127.0.0.1:23               sleeping     -          0.0      0.0      0.0        0.2        0.3       33.9     2139.1        8       0.0       0.0 ...    35
   12:13:01    0:00:02   136394 ┗━ rdp bbennett@192.168.238.152:3389          sleeping     -          0.0      0.0      0.0        0.8        0.2       44.6     2301.8       57       0.0       0.0 ...    92
   ...
   12:13:03    0:00:04   122657 ┣━ vnc bbennett@192.168.68.50:5900            sleeping     -          1.0      1.0      0.0      191.5       10.6      152.4     2103.9       23       0.0       0.0 ...    24
          -          -   122662 ┃  ┣─ vnc-worker                              sleeping     -          1.0      1.0      0.0          -          -          -          -        -      15.9       0.5 ...     -
          -          -   122663 ┃  ┣─ user-pending                            sleeping     -          0.0      0.0      0.0          -          -          -          -        -       4.0       0.0 ...     -
          -          -   122682 ┃  ┗─ display-render                          sleeping     -          0.0      0.0      0.0          -          -          -          -        -       2.0       0.5 ...     -
```

**guacd threads, with kernel stacks for the hot ones.** Stacks are only dumped
for rows at/above `RES_MON_CPU_HIGH_THRESH` (80%); this idle daemon has no hot
rows, so none appear:

```sh
monitor-resources.py --pid 119479 --thread --stack --interval 2
```
```
TIME(LOCAL)    ELAPSED      PID NAME                                          STATE        HEALTH    CPU%     USR%     SYS% ...   FDs
   12:14:00    0:00:04   119479 guacd -f -b 127.0.0.1 -l 4822 -L debug        sleeping     -          0.5      0.5      0.0 ...    24
          -          -   136305 ┣─ conn-read                                  sleeping     -          0.0      0.0      0.0 ...     -
          -          -   136309 ┣─ conn-read                                  sleeping     -          0.0      0.0      0.0 ...     -
          -          -   136362 ┗─ conn-read                                  sleeping     -          0.0      0.0      0.0 ...     -
```

**Everything, also mirrored to stdout.** Same layout as the combined +
descendants + threads views above, just streamed to stdout as well as the log:

```sh
monitor-resources.py --system --pid 119479 --descendants --thread --display --interval 2
```

**Live `top(1)`-style view with interactive controls** (press `d`/`t`/`s`/`+`/`-`/`?`
while running). The screen clears and redraws each interval, so you see one
live-updating frame; with stdin not a TTY it just falls back to a plain timed
sleep:

```sh
monitor-resources.py --pid 119479 --top --interactive --interval 2
```
```
TIME(LOCAL)    ELAPSED      PID NAME                                          STATE        HEALTH    CPU%     USR%     SYS% ...   FDs
   12:15:00    0:00:06   119479 guacd -f -b 127.0.0.1 -l 4822 -L debug        sleeping     -          0.0      0.0      0.0 ...    24
```

**Sample every 5 seconds, write the log file and JSON outputs.** Paths are echoed
to stderr on exit:

```sh
monitor-resources.py --system --interval 5 --log --json
```
```
Monitor log:   ./resource-monitor_monres.log
Monitor JSON:  ./resource-monitor_monres.detail.json
Monitor JSON:  ./resource-monitor_monres.summary.json
```
```sh
$ jq '.system.cpu_percent' resource-monitor_monres.summary.json
{
  "n": 3,
  "min": 24.93125,
  "avg": 26.610416666666666,
  "p50": 25.875,
  "p95": 28.71,
  "p99": 28.962000000000003,
  "max": 29.025000000000002
}
```

**Show only a few metric columns (system view).** The identity and `*_GB`
columns are always kept:

```sh
monitor-resources.py --system --columns cpu,mem,mem_gb --interval 2
```
```
TIME(LOCAL)    ELAPSED     CPU%     MEM%     MEM_GB    ROOT_GB    HOME_GB
   12:13:30    0:00:02     24.8     41.4       8.79      58.00     134.96
   12:13:32    0:00:04     35.0     41.5       8.80      58.00     134.96
   12:13:34    0:00:06     24.5     41.5       8.79      58.00     134.96
```

**Trim a per-process view down to leak-hunting columns:**

```sh
monitor-resources.py --pid 119479 --columns cpu,rss,threads,fds --interval 2
```
```
TIME(LOCAL)    ELAPSED      PID NAME                                              CPU%     RSS_MB  THREADS   FDs
   12:13:37    0:00:02   119479 guacd -f -b 127.0.0.1 -l 4822 -L debug             0.0       17.2       20    24
   12:13:39    0:00:04   119479 guacd -f -b 127.0.0.1 -l 4822 -L debug             0.0       17.2       20    24
```

**Same selection via the environment** (identical output to the previous example):

```sh
RES_MON_COLUMNS=cpu,rss,threads,fds monitor-resources.py --pid 119479 --interval 2
```

**Custom log location/prefix via environment.** Same content as the `--log`
example above; only the output path changes (here `--timestamp` adds a
`-YYYYMMDD-HHMMSS` suffix):

```sh
RES_MON_LOG_DIR=/var/log/resmon RES_MON_LOG_PREFIX=guacd \
  monitor-resources.py --pid 119479 --log --timestamp
```
```
Monitor log:   /var/log/resmon/guacd-20260601-121500_monres.log
```

To exercise the metrics and health flags, use the bundled
`test-monitor-resources.sh`. It spawns one `stress-ng` process per scenario
(`cpu`, `mem`, `disk-read`, `disk-write`, `threads`, `ctx`, `ivctx`, `fd`,
`faults`, `net`, `leak`, `zombie`, `iomix`, or `all`), each with a distinct
signature, and prints every worker's PID so you can point a monitor at them.
Needs `stress-ng` on `PATH`.

Start the workloads in one terminal (runs until Ctrl-C, or use `-d SEC`):

```sh
# A few scenarios, until Ctrl-C
./test-monitor-resources.sh all

# Everything for 60 seconds
./test-monitor-resources.sh -d 60 cpu mem disk-write threads
```
Note the PID:
```
[stress] script pid=137169  duration=until-ctrl-c  workloads=cpu mem disk-read disk-write threads ctx ivctx fd faults net leak zombie iomix
[stress] equivalent: test-monitor-resources.sh  cpu mem disk-read disk-write threads ctx ivctx fd faults net leak zombie iomix
...
```

In another terminal point the monitor at the `stress-ng` PID. and add `-d` so
the forked workers get picked up too:

```sh
sudo ./monitor-resources.py -p <PID> -d -t --display
```

## Debugging performance issues

The sections below map common symptom patterns to a likely cause, grouped by
resource. The `HEALTH` flags (see [Health flags](#health-flags)) catch several
of these for you automatically.

### A workflow

1. **Start system-wide** (`--system`) to see if the pressure is global (high
   `LOAD1`, `CPU%`, `IOWAIT%`, `SWP_GB`) or not.
2. **Narrow to the process** (`-p PID`). If the target is a supervisor that forks
   workers (e.g. `guacd`, `make`, `gunicorn`), add `--descendants` so you sample
   the busy children, not an idle parent.
3. **Find the hot thread** with `--thread`: a process at moderate CPU% can hide a
   single thread pegged near 100% (a single-threaded bottleneck).
4. **See where it's stuck** with `--stack`: for any row over
   `RES_MON_CPU_HIGH_THRESH`, the kernel stack tells you whether it's spinning,
   blocked in a syscall, or waiting on I/O.
5. **Check the spread**: in the end-of-run summary, a big gap between `p50` and
   `p99` means the load is bursty, not steady — the average alone would hide the
   spikes. See [Summary statistics](#summary-statistics).

### CPU

| Pattern | Likely cause |
| --- | --- |
| High `CPU%` + low `VCTX/s` | Compute-bound: tight loop or spin-wait, not yielding. |
| High `CPU%` + high `SYS%` | Kernel-heavy work: syscalls, locking, or disk/network I/O paths. |
| High `CPU%` + high `IVCTX/s` | CPU oversubscription: more runnable threads than cores; the scheduler is preempting them (`IVX` flag). |
| Low `CPU%` + high `VCTX/s` | Blocking/waiting: locks, condition variables, sleeps; the thread keeps yielding. |
| `BUSY` near the core count | Most cores saturated, not just a high average: system-wide CPU exhaustion. |
| `LOAD1` ≫ core count, process `CPU%` low | Contention elsewhere: runnable work is queuing in other processes/threads. |
| One thread ≈ 100% while process `CPU%` modest | Single-threaded bottleneck; the work isn't parallelized (`--thread` to confirm). |

### Memory

| Pattern | Likely cause |
| --- | --- |
| `RSS_MB` steadily increasing | Memory leak or unbounded growth (`MLK` flag). |
| Non-zero `MAJFLT/s` | Paging from disk: not enough RAM for the working set (`MJF` flag). |
| `SWP_GB` increasing | Memory pressure; the kernel is swapping out pages. |
| High `MINFLT/s` + stable `RSS_MB` | Memory churn: frequent mapping/unmapping or allocator activity, but no net growth. |
| `VMS_MB` growing while `RSS_MB` stable | Address-space growth (large `mmap`s/reservations) without touching pages: usually benign, but can precede a leak. |
| System `MEM%` high + per-process `RSS` flat | The pressure is from another process; widen with `--system` or more `-p` targets. |

### I/O and network

| Pattern | Likely cause |
| --- | --- |
| Low `CPU%` + high `IOWAIT%` | I/O bound: the CPU is idle waiting on disk. |
| High `WRITE_MB/s` + low `CPU%` | Write-heavy workload (flushing, logging, spooling). |
| High `READ_MB/s` + high `MAJFLT/s` | Reads are faults/paging rather than application I/O: memory pressure, not throughput. |
| State `disk-sleep` (`DSK` flag) | Blocked in uninterruptible I/O: stuck on a slow/hung device or NFS mount. |
| High `NET_R/s` or `NET_S/s`, low CPU | Network-bound; throughput, not compute, is the limit. |

### Threads, descriptors, and process state

| Pattern | Likely cause |
| --- | --- |
| `THREADS` steadily increasing | Thread leak or unbounded pool growth (`THD` flag). |
| `FDs` steadily increasing | File-descriptor leak: sockets/files opened but never closed (`FDL` flag). |
| High system-wide `CTX_SW/s` | Lock contention or too many runnable threads thrashing the scheduler. |
| State `zombie` (`ZMB` flag) | Process exited but its parent hasn't reaped it; the parent is buggy or stuck. |
| Bursty metric (`p99` ≫ `p50` in the summary) | Intermittent spikes; sample faster (`-i`) around the event to catch it. |

## guacd thread names

When you're monitoring `guacd`, the per-thread view (`-t`/`--thread`) is way more
useful if you can tell what each thread *does*. `guacd` and its per-connection
child processes name their worker threads, so the `NAME` column shows a short
label (e.g. `display-wrk`, `rdp-worker`) instead of an anonymous TID.

These names are each thread's Linux `comm` value, set via `prctl(PR_SET_NAME)`
(`guac_thread_name_set()` in `libguac`). `monitor-resources.py` reads them
straight from `/proc/<pid>/task/<tid>/comm`, so the same labels show up in
`top -H`, `htop`, `ps -L`, and `gdb` too. A few gotchas:

- **Names are capped at 15 characters** (the kernel `comm` limit). The labels
  below all fit, so they show in full.
- **Unnamed threads inherit their creator's name** until they rename themselves;
  a thread that never calls `guac_thread_name_set()` shows the process name with
  its TID appended (e.g. `guacd[12345]`), which `monitor-resources.py` renders to
  keep it distinct.
- The **main thread's** `comm` tracks the *process title* (the active connection,
  e.g. `vnc user@host:5900`), not a worker name — that's set separately by
  `guac_process_title_set()`.

The threads below are the named workers as of the current source. To regenerate
the list from a checkout, run:

```sh
grep -rn -A1 "Thread name " src/
```

### guacd (core daemon)

These run in the main `guacd` daemon and its per-connection child processes,
which broker the connection between a user and the protocol client.

| Thread | Role |
| --- | --- |
| `conn-route` | Performs the protocol handshake for a new client connection and routes it to a connection process. |
| `conn-read` | Forwards data from the connection's child process back to the connected user. |
| `conn-write` | Forwards data from the connected user to the connection's child process. |
| `user-conn` | Manages a single user's connection lifecycle, from handshake through disconnect. |
| `client-free` | Frees a `guac_client` in the background, bounded by a timeout in case the free handler hangs. |

### libguac (shared client library)

These belong to `libguac`, the client library every protocol shares, and drive
the user/display plumbing common to all connection types.

| Thread | Role |
| --- | --- |
| `user-pending` | Periodically promotes pending users into the active connection. |
| `user-input` | Reads and parses instructions from a single user's socket. |
| `display-render` | Drives the display render loop, flushing completed frames to the client. |
| `display-wrk` | One worker in the display pool; encodes and sends graphical updates for dirty layer regions. (Expect several of these.) |
| `keep-alive` | Periodically sends keep-alive NOPs on an otherwise idle socket. |

### Protocol clients

These are specific to each remote-desktop / terminal protocol; you'll only see
the threads for the protocol(s) a given child process is serving.

| Thread | Role |
| --- | --- |
| `rdp-worker` | Main RDP client thread; runs the FreeRDP connection and event loop. |
| `rdp-audio` | Flushes buffered audio input to the RDP server at the negotiated rate. |
| `rdp-print` | Streams output from the RDP print filter process to the client as a downloadable file. |
| `vnc-worker` | Main VNC client thread; runs the libvncclient connection and message loop. |
| `ssh-worker` | Main SSH client thread; runs the SSH session and drives the terminal. |
| `ssh-stdin` | Reads terminal STDIN and forwards it to the SSH server. |
| `telnet-worker` | Main telnet client thread; runs the telnet session and event loop. |
| `telnet-stdin` | Reads terminal STDIN and forwards it to the telnet server. |
| `k8s-worker` | Main Kubernetes client thread; manages the websocket connection to the pod. |
| `k8s-input` | Reads user input and forwards it to the Kubernetes pod. |
| `terminal` | Renders the terminal emulator display and processes output from the remote (SSH, telnet, and Kubernetes connections). |
