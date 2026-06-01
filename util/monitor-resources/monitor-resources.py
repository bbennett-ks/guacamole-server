#!/usr/bin/env python3

"""
Periodically sample system and/or per-process resource usage and write a
fixed-width log file (and optionally JSON) for later analysis.

Cross-platform: system/process metrics use psutil and run anywhere psutil
does. Thread metrics, kernel stacks, and per-task page faults / context
switches are sourced from /proc and are silent no-ops on platforms without
/proc (Windows, macOS); the corresponding flags (-t, -s) still parse but
produce empty data. FD counts fall back to handle counts on Windows. The
root check uses euid 0; on Windows it's a no-op-by-absence (geteuid is
POSIX only).

What it samples
  System-wide (-S/--system, default when no -p):
    CPU%, USR%, SYS%, BUSY (cores >= RES_MON_CPU_HIGH_THRESH), CTX_SW/s,
    LOAD1, IOWAIT%, MEM%, MEM_GB, SWP_GB, disk read/write MB/s, net
    receive/send MB/s, absolute used GB per filesystem in RES_MON_FILESYSTEMS.
  Per-process (-p PID, repeatable):
    CPU%/USR%/SYS%, USER_SEC/SYS_SEC, RSS_MB, VMS_MB, THREADS,
    VCTX/s, IVCTX/s, MINFLT/s, MAJFLT/s, READ_MB/s, WRITE_MB/s, FDs,
    plus a HEALTH column with stateless flags (CPU/ZMB/DSK/MJF/IVX) and
    trend flags (MLK/FDL/THD) that compare against the first sample after
    RES_MON_HEALTH_MIN_SAMPLES intervals.
  Per-thread (-t/--thread, requires -p): one row per thread under each
    process row, sorted by CPU% desc with idle threads filtered out.
  Descendants (-d/--descendants): recursively pick up new descendants of each -p
    target each interval, attributed flat under the root.
  Kernel stacks (-s/--stack): under each row whose CPU% >=
    RES_MON_CPU_HIGH_THRESH, dump /proc/<pid>/stack (or task/<tid>/stack).

Output (all three output streams are independent and may be combined)
  Log file (-l/--log): RES_MON_LOG_DIR/RES_MON_LOG_PREFIX_monres.log, with
    --timestamp adding -YYYYMMDD-HHMMSS to the prefix. Off by default; path
    is echoed to stderr at start and end when enabled.
  stdout: silent by default; -D/--display mirrors rows; -T/--top redraws
    the screen each iteration in a top(1) style (double-buffered, atomic
    per frame). --top implies --display; the log file is unaffected.
  JSON (-j/--json): writes <log>.detail.json (per-sample arrays) and
    <log>.summary.json (min/avg/p50/p95/p99/max stats), suitable for visualization.
    Uses the same path basename as -l would, so JSON works without -l.
  --detail-only / --summary-only restrict what's written (text and JSON);
    they are mutually exclusive.

Sampling
  -i/--interval <sec> overrides RES_MON_INTERVAL_SEC (default 60). The
  sleep is interruptible when -I/--interactive is on so keystrokes are
  responsive; psutil counters are read instantly after the sleep window.

Interactive (-I, requires a TTY)
  Single-key controls dispatched while sampling sleeps; changes take
  effect at the next sample:
    d = toggle --descendants   t = toggle --thread   s = toggle --stack
    + / - = interval +/- 5s (floor 5s)   ? = re-print this banner

Requirements
  POSIX: requires sudo/root. Without CAP_SYS_PTRACE / matching uid the
  kernel may block /proc/<pid>/io reads, /proc/<pid>/stack reads, and
  descendant discovery or per-process sampling for processes owned by
  other users. If sudo is unavailable or authentication fails, the script
  exits with an error explaining why elevated access may be necessary.
  Windows: no EUID concept; the root check is skipped. Features that
  depend on /proc (thread metrics, kernel stacks, per-task page faults
  and context switches) silently produce no data.

Environment variables (override defaults at startup)
  RES_MON_LOG_DIR, RES_MON_LOG_PREFIX, RES_MON_INTERVAL_SEC,
  RES_MON_CPU_HIGH_THRESH, RES_MON_MEM_HIGH_THRESH,
  RES_MON_IOWAIT_HIGH_THRESH, RES_MON_FLUSH_EACH_LINE, RES_MON_FILESYSTEMS,
  RES_MON_IVX_HIGH_THRESH, RES_MON_HEALTH_MIN_SAMPLES, RES_MON_DETAIL_ONLY,
  RES_MON_SUMMARY_ONLY, RES_MON_STACK, RES_MON_JSON, RES_MON_LOG, RES_MON_COLUMNS.
Run with -h/--help for the full per-flag reference and current values.
"""

import datetime
from dataclasses import dataclass, field
import json
import os
import platform
import psutil
import select
import shutil
import signal
import statistics
import sys
import time
from typing import Any, Dict, List, Tuple

# Single-key interactive input requires POSIX TTY APIs (termios/tty) and select() on a
# non-socket file descriptor. Windows lacks termios/tty entirely and its select() doesn't
# accept console stdin, so --interactive degrades to a plain time.sleep there.
if os.name != "nt":
    import termios
    import tty
    _INTERACTIVE_SUPPORTED = True
else:
    _INTERACTIVE_SUPPORTED = False

# Open-handle column label: POSIX exposes file descriptors via num_fds(); Windows exposes
# kernel handles via num_handles(). Render the appropriate name in the text/log output so
# the column header is accurate on each platform. JSON field name stays "fds" either way.
_FD_COL_LABEL = "HND" if os.name == "nt" else "FDs"

RES_MON_LOG_DIR            = os.getenv("RES_MON_LOG_DIR", os.getcwd())
RES_MON_LOG_PREFIX         = os.getenv("RES_MON_LOG_PREFIX", "resource-monitor")

RES_MON_INTERVAL_SEC       = float(os.getenv("RES_MON_INTERVAL_SEC", 60))
RES_MON_CPU_HIGH_THRESH    = float(os.getenv("RES_MON_CPU_HIGH_THRESH", 80))
RES_MON_MEM_HIGH_THRESH    = float(os.getenv("RES_MON_MEM_HIGH_THRESH", 90))
RES_MON_IOWAIT_HIGH_THRESH = float(os.getenv("RES_MON_IOWAIT_HIGH_THRESH", 10))
RES_MON_FLUSH_EACH_LINE    = os.getenv("RES_MON_FLUSH_EACH_LINE", "1") == "1"
RES_MON_FILESYSTEMS        = os.getenv("RES_MON_FILESYSTEMS", "/ /home")
RES_MON_IVX_HIGH_THRESH    = float(os.getenv("RES_MON_IVX_HIGH_THRESH", 500))
RES_MON_HEALTH_MIN_SAMPLES = int(os.getenv("RES_MON_HEALTH_MIN_SAMPLES", 3))
RES_MON_DETAIL_ONLY        = os.getenv("RES_MON_DETAIL_ONLY", "0") == "1"
RES_MON_SUMMARY_ONLY       = os.getenv("RES_MON_SUMMARY_ONLY", "0") == "1"
RES_MON_STACK              = os.getenv("RES_MON_STACK", "0") == "1"
RES_MON_JSON               = os.getenv("RES_MON_JSON", "0") == "1"
RES_MON_LOG                = os.getenv("RES_MON_LOG", "0") == "1"
RES_MON_COLUMNS            = os.getenv("RES_MON_COLUMNS")  # comma/space list of metric column keys; None = all

@dataclass
class MonitorConfig:
    # Runtime configuration: fields are normally set once at startup, but a few
    # (system, threads, children, interval_sec) are mutated by --interactive at runtime.
    system: bool       # monitor system-wide stats
    pids: List[int]    # target PIDs; empty = disabled
    interval_sec: float
    log_file: str
    logfile_timestamp: bool
    cpu_high_thresh: float
    mem_high_thresh: float
    iowait_high_thresh: float
    flush_each_line: bool
    display: bool
    threads: bool
    children: bool
    filesystems: List[Tuple[str, str]]
    ivx_high_thresh: float
    health_min_samples: int
    detail_only: bool
    summary_only: bool
    interactive: bool
    stack: bool
    top: bool
    json: bool
    log: bool
    columns: set      # enabled metric-column keys, or None for "all"

@dataclass
class MonitorStats:
    # Accumulated time-series data used to produce the final summary statistics.
    cpu_usage: List[float] = field(default_factory=list)
    busy_cores: List[float] = field(default_factory=list)
    cpu_usr_usage: List[float] = field(default_factory=list)
    cpu_sys_usage: List[float] = field(default_factory=list)
    load_average: List[float] = field(default_factory=list)
    cpu_iowait_usage: List[float] = field(default_factory=list)
    mem_usage_percent: List[float] = field(default_factory=list)
    mem_usage_gb: List[float] = field(default_factory=list)
    disk_read_mb: List[float] = field(default_factory=list)
    disk_write_mb: List[float] = field(default_factory=list)
    network_receive_mb: List[float] = field(default_factory=list)
    network_send_mb: List[float] = field(default_factory=list)
    swap_used_gb: List[float] = field(default_factory=list)
    ctx_switches: List[float] = field(default_factory=list)
    filesystem_usage_gb: Dict[str, List[float]] = field(default_factory=dict)
    sample_dicts: List[Dict[str, Any]] = field(default_factory=list)  # per-sample data for --json output

@dataclass
class MonitorState:
    # Mutable runtime state needed across polling iterations.
    last_disk: object
    last_net: object
    last_ctx: object
    start_monotonic: float
    start_local: datetime.datetime
    stats: MonitorStats
    running: bool = True

@dataclass
class Sample:
    # One fully computed monitoring sample ready for logging and aggregation.
    time_str: str
    elapsed_str: str
    cpu_usage: float
    busy_cores: int
    cpu_usr_usage: float
    cpu_sys_usage: float
    load_average: float
    cpu_iowait_usage: float
    mem_usage_percent: float
    mem_usage_gb: float
    disk_read_mb: float
    disk_write_mb: float
    network_receive_mb: float
    network_send_mb: float
    swap_used_gb: float
    ctx_switches: float
    filesystem_usage_gb: Dict[str, float]

@dataclass
class ProcessStats:
    # Accumulated time-series data for per-process monitoring.
    cpu_percent: List[float] = field(default_factory=list)
    usr_percent: List[float] = field(default_factory=list)
    sys_percent: List[float] = field(default_factory=list)
    user_sec: List[float] = field(default_factory=list)
    sys_sec: List[float] = field(default_factory=list)
    rss_mb: List[float] = field(default_factory=list)
    vms_mb: List[float] = field(default_factory=list)
    threads: List[int] = field(default_factory=list)
    vctx_per_sec: List[float] = field(default_factory=list)
    ivctx_per_sec: List[float] = field(default_factory=list)
    minflt_per_sec: List[float] = field(default_factory=list)
    majflt_per_sec: List[float] = field(default_factory=list)
    read_mb_per_sec: List[float] = field(default_factory=list)
    write_mb_per_sec: List[float] = field(default_factory=list)
    fds: List[int] = field(default_factory=list)
    sample_dicts: List[Dict[str, Any]] = field(default_factory=list)  # per-sample data for --json output

@dataclass
class ProcessState:
    # Mutable runtime state for per-process polling.
    proc: object
    name: str              # captured at init so it remains available after the process exits
    last_cpu_times: object
    last_ctx_switches: object
    last_page_faults: tuple   # (minflt, majflt) cumulative counts
    last_io_counters: object
    start_monotonic: float
    start_local: datetime.datetime
    stats: ProcessStats
    running: bool = True
    thread_states: Dict = field(default_factory=dict)
    is_child: bool = False     # True if discovered via -d, not directly specified via -p
    parent_pid: int = 0        # PID of the -p parent that owns this child

@dataclass
class ProcessSample:
    # One fully computed per-process sample ready for logging and aggregation.
    time_str: str
    elapsed_str: str
    pid: int
    name: str
    state: str
    health: str
    cpu_percent: float
    usr_percent: float
    sys_percent: float
    user_sec: float        # cumulative user CPU time in seconds
    sys_sec: float         # cumulative kernel CPU time in seconds
    rss_mb: float
    vms_mb: float
    threads: int
    vctx_per_sec: float
    ivctx_per_sec: float
    minflt_per_sec: float
    majflt_per_sec: float
    read_mb_per_sec: float
    write_mb_per_sec: float
    fds: int

@dataclass
class ThreadState:
    # Mutable baseline state for one thread, tracked across polling iterations.
    last_user_time: float
    last_system_time: float
    last_vctx: int
    last_ivctx: int
    last_minflt: int
    last_majflt: int

@dataclass
class ThreadSample:
    # One fully computed per-thread sample ready for logging.
    time_str: str
    elapsed_str: str
    pid: int
    tid: int
    name: str
    state: str
    health: str
    cpu_percent: float
    usr_percent: float
    sys_percent: float
    vctx_per_sec: float
    ivctx_per_sec: float
    minflt_per_sec: float
    majflt_per_sec: float

def usage(out):
    # Print help, field descriptions, and current env var values.
    cpu_count = psutil.cpu_count(logical=True)
    mem_usage_gb = psutil.virtual_memory().total / (1024 ** 3)
    swap_total_gb = psutil.swap_memory().total / (1024 ** 3)

    out.write(f"Usage: {os.path.basename(sys.argv[0])} [options]\n")
    out.write(f"\n")
    out.write(f"Options:\n")
    out.write(f"  What to monitor:\n")
    out.write(f"    -S|--system     Monitor system-wide resources.\n")
    out.write(f"    -p|--pid <pid>  Monitor a specific process by PID (may be repeated).\n")
    out.write(f"    -d|--descendants\n")
    out.write(f"                    Also monitor descendants of each -p PID, at any depth.\n")
    out.write(f"    -t|--thread     List each thread under its process (requires -p).\n")
    out.write(f"    -s|--stack      Show kernel stack under each process row (/proc/<pid>/stack) and, when -t\n")
    out.write(f"                    is also set, each thread row (/proc/<pid>/task/<tid>/stack) whose\n")
    out.write(f"                    CPU% >= RES_MON_CPU_HIGH_THRESH ({RES_MON_CPU_HIGH_THRESH:.0f}%).\n")
    out.write(f"                    Stack reads typically need CAP_SYS_PTRACE; missing/empty stacks are skipped.\n")
    out.write(f"  Sampling:\n")
    out.write(f"    -i|--interval <sec>\n")
    out.write(f"                    Sampling interval in seconds (overrides RES_MON_INTERVAL_SEC).\n")
    out.write(f"    -I|--interactive\n")
    out.write(f"                    Read single-key commands from stdin while running. Requires a POSIX\n")
    out.write(f"                    TTY (termios + select on console fd); silently falls back to plain\n")
    out.write(f"                    sleep on Windows or when stdin isn't a TTY.\n")
    out.write(f"                    Keys: d = toggle --descendants, t = toggle --thread, s = toggle --stack,\n")
    out.write(f"                          + = interval +5s, - = interval -5s (min 5s), ? = re-print keys.\n")
    out.write(f"                    Changes take effect at the next sample.\n")
    out.write(f"  Output streams:\n")
    out.write(f"    -D|--display    Also display log output to stdout.\n")
    out.write(f"    -T|--top        top(1)-style display: clear screen and re-emit the header before\n")
    out.write(f"                    each iteration's stdout output. Implies --display. The log file is\n")
    out.write(f"                    unaffected (clear codes are written to stdout only).\n")
    out.write(f"    -l|--log        Write the fixed-width log file at RES_MON_LOG_DIR/RES_MON_LOG_PREFIX_monres.log.\n")
    out.write(f"                    Off by default; same as RES_MON_LOG=1. -j/--json still emits its sibling\n")
    out.write(f"                    JSON files alongside the would-be log path even when -l is off.\n")
    out.write(f"    -j|--json       Also write <log>.detail.json (per-sample arrays) and\n")
    out.write(f"                    <log>.summary.json (min/avg/p50/p95/p99/max stats) for easy visualization.\n")
    out.write(f"                    Honors --detail-only / --summary-only. Same as RES_MON_JSON=1.\n")
    out.write(f"  Output content:\n")
    out.write(f"    --timestamp     Include a timestamp in the log file name (default: off).\n")
    out.write(f"    --columns <keys>\n")
    out.write(f"                    Comma/space-separated list of metric columns to display (or 'all').\n")
    out.write(f"                    Filters the detail rows and the end-of-run summary; the identity\n")
    out.write(f"                    columns (TIME, ELAPSED, PID, NAME) and per-filesystem *_GB columns\n")
    out.write(f"                    are always shown, and JSON output always carries every metric.\n")
    out.write(f"                    Overrides RES_MON_COLUMNS. See COLUMN SELECTION below for valid keys.\n")
    out.write(f"    --detail-only   Write only the periodic detail rows; omit the end-of-run summary.\n")
    out.write(f"                    Overrides RES_MON_DETAIL_ONLY; mutually exclusive with --summary-only.\n")
    out.write(f"    --summary-only  Write only the end-of-run summary; omit headers and periodic rows.\n")
    out.write(f"                    Overrides RES_MON_SUMMARY_ONLY; mutually exclusive with --detail-only.\n")
    out.write(f"  Other:\n")
    out.write(f"    -h|--help       Show this help message.\n")
    out.write(f"\n")
    out.write(f"On POSIX, the script requires sudo/root. Without elevated access, /proc/<pid>/io,\n")
    out.write(f"/proc/<pid>/stack, descendant discovery, and sampling of other users' processes may be\n")
    out.write(f"blocked by the kernel. If sudo is unavailable or denied, the script exits with an error\n")
    out.write(f"explaining why elevated access may be necessary.\n")
    out.write(f"On Windows there is no EUID concept; the root check is skipped, and /proc-dependent\n")
    out.write(f"features (thread metrics, kernel stacks) silently produce no data.\n")
    out.write(f"\n")
    out.write(f"If neither -S/--system nor -p/--pid is given, --system is assumed. Both may be combined;\n")
    out.write(f"when combined, system and process stats are written on one line separated by '|'.\n")
    out.write(f"With multiple -p, one row per process is written per interval.\n")
    out.write(f"\n")
    out.write(f"Examples:\n")
    out.write(f"    {os.path.basename(sys.argv[0])} --system                                          # system-wide stats only\n")
    out.write(f"    {os.path.basename(sys.argv[0])} --system --pid $(pidof guacd)                    # system + guacd process\n")
    out.write(f"    {os.path.basename(sys.argv[0])} --pid $(pidof guacd) --descendants --thread      # guacd + descendants + threads\n")
    out.write(f"    {os.path.basename(sys.argv[0])} --pid $(pidof guacd) --thread --stack            # threads with kernel stacks for hot ones\n")
    out.write(f"    {os.path.basename(sys.argv[0])} --system --pid $(pidof guacd) --descendants --thread --display  # all of the above\n")
    out.write(f"\n")

    out.write(f"HOST HARDWARE SUMMARY:\n")
    out.write(f"    Logical CPUs : {cpu_count:>5d}\n")
    out.write(f"    Total Memory : {mem_usage_gb:>5.1f} GB\n")
    out.write(f"    Total Swap   : {swap_total_gb:>5.1f} GB\n")

    out.write(f"\nCOLUMN SELECTION (--columns / RES_MON_COLUMNS):\n")
    out.write(f"    Pass a comma/space-separated list of the keys below to show only those metric\n")
    out.write(f"    columns (e.g. --columns cpu,mem,rss). 'all' or an empty value shows everything.\n")
    out.write(f"    The TIME, ELAPSED, PID, and NAME columns and the per-filesystem *_GB columns are\n")
    out.write(f"    always shown. Keys map to whichever active view defines them; some are shared.\n")
    sys_keys = "  ".join(f"{key}={label}" for key, label, _, _ in SYSTEM_METRIC_COLUMNS)
    proc_keys = "  ".join(f"{key}={label}" for key, label, _, _, _, _ in PROCESS_METRIC_COLUMNS)
    out.write(f"    System  (--system) : {sys_keys}\n")
    out.write(f"    Process (--pid)    : {proc_keys}\n")
    out.write(f"    Threads (--thread) reuse the process keys; columns with no per-thread meaning\n")
    out.write(f"    (user_sec, sys_sec, rss, vms, threads, read, write, fds) render as '-'.\n")

def percentile(sorted_vals, pct):
    # Linear-interpolation percentile (matching numpy's default 'linear' method).
    # sorted_vals must be non-empty and sorted ascending; pct is 0..100.
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (rank - lo)

def summary_line(name, vals):
    # Format one "min/avg/p50/p95/p99/max" stats line, or "n/a" if no data.
    if not vals:
        return f"{name:<15} n/a"
    sv = sorted(vals)
    return (f"{name:<15} min {sv[0]:8.2f} | avg {statistics.mean(vals):8.2f} | "
            f"p50 {percentile(sv, 50):8.2f} | p95 {percentile(sv, 95):8.2f} | "
            f"p99 {percentile(sv, 99):8.2f} | max {sv[-1]:8.2f}")

def stats_summary(values):
    # min/avg/p50/p95/p99/max + count, used as a stable shape in the JSON summary; empty when no samples.
    if not values:
        return {"n": 0}
    sv = sorted(values)
    return {"n": len(values), "min": sv[0], "avg": statistics.mean(values),
            "p50": percentile(sv, 50), "p95": percentile(sv, 95), "p99": percentile(sv, 99),
            "max": sv[-1]}

def system_sample_to_dict(sample):
    # Map a Sample dataclass to a JSON-serializable dict. Names follow snake_case for stable downstream parsing.
    return {
        "time":                sample.time_str,
        "elapsed":             sample.elapsed_str,
        "cpu_percent":         sample.cpu_usage,
        "usr_percent":         sample.cpu_usr_usage,
        "sys_percent":         sample.cpu_sys_usage,
        "busy_cores":          sample.busy_cores,
        "ctx_switches":        sample.ctx_switches,
        "load1":               sample.load_average,
        "iowait_percent":      sample.cpu_iowait_usage,
        "mem_percent":         sample.mem_usage_percent,
        "mem_gb":              sample.mem_usage_gb,
        "swap_used_gb":        sample.swap_used_gb,
        "disk_read_mb":        sample.disk_read_mb,
        "disk_write_mb":       sample.disk_write_mb,
        "net_recv_mb":         sample.network_receive_mb,
        "net_send_mb":         sample.network_send_mb,
        "filesystem_usage_gb": dict(sample.filesystem_usage_gb),
    }

def process_sample_to_dict(sample):
    # Map a ProcessSample dataclass to a JSON-serializable dict.
    return {
        "time":            sample.time_str,
        "elapsed":         sample.elapsed_str,
        "pid":             sample.pid,
        "name":            sample.name,
        "state":           sample.state,
        "health":          sample.health,
        "cpu_percent":     sample.cpu_percent,
        "usr_percent":     sample.usr_percent,
        "sys_percent":     sample.sys_percent,
        "user_sec":        sample.user_sec,
        "sys_sec":         sample.sys_sec,
        "rss_mb":          sample.rss_mb,
        "vms_mb":          sample.vms_mb,
        "threads":         sample.threads,
        "vctx_per_sec":    sample.vctx_per_sec,
        "ivctx_per_sec":   sample.ivctx_per_sec,
        "minflt_per_sec":  sample.minflt_per_sec,
        "majflt_per_sec":  sample.majflt_per_sec,
        "read_mb_per_sec": sample.read_mb_per_sec,
        "write_mb_per_sec": sample.write_mb_per_sec,
        "fds":             sample.fds,
    }

def build_system_summary(state):
    # JSON summary block for system-wide metrics; mirrors the text SUMMARY shape.
    s = state.stats
    out = {
        "cpu_percent":     stats_summary(s.cpu_usage),
        "busy_cores":      stats_summary(s.busy_cores),
        "usr_percent":     stats_summary(s.cpu_usr_usage),
        "sys_percent":     stats_summary(s.cpu_sys_usage),
        "ctx_switches":    stats_summary(s.ctx_switches),
        "load1":           stats_summary(s.load_average),
        "iowait_percent":  stats_summary(s.cpu_iowait_usage),
        "mem_percent":     stats_summary(s.mem_usage_percent),
        "mem_gb":          stats_summary(s.mem_usage_gb),
        "swap_used_gb":    stats_summary(s.swap_used_gb),
        "disk_read_mb":    stats_summary(s.disk_read_mb),
        "disk_write_mb":   stats_summary(s.disk_write_mb),
        "net_recv_mb":     stats_summary(s.network_receive_mb),
        "net_send_mb":     stats_summary(s.network_send_mb),
        "filesystem_usage_gb": {label: stats_summary(vals) for label, vals in s.filesystem_usage_gb.items()},
    }
    return out

def build_process_summary(state):
    # JSON summary block for one monitored process; uses the captured display name.
    s = state.stats
    return {
        "pid":             state.proc.pid,
        "name":            state.name,
        "is_child":        state.is_child,
        "parent_pid":      state.parent_pid,
        "cpu_percent":     stats_summary(s.cpu_percent),
        "usr_percent":     stats_summary(s.usr_percent),
        "sys_percent":     stats_summary(s.sys_percent),
        "user_sec":        stats_summary(s.user_sec),
        "sys_sec":         stats_summary(s.sys_sec),
        "rss_mb":          stats_summary(s.rss_mb),
        "vms_mb":          stats_summary(s.vms_mb),
        "threads":         stats_summary(s.threads),
        "vctx_per_sec":    stats_summary(s.vctx_per_sec),
        "ivctx_per_sec":   stats_summary(s.ivctx_per_sec),
        "minflt_per_sec":  stats_summary(s.minflt_per_sec),
        "majflt_per_sec":  stats_summary(s.majflt_per_sec),
        "read_mb_per_sec": stats_summary(s.read_mb_per_sec),
        "write_mb_per_sec": stats_summary(s.write_mb_per_sec),
        "fds":             stats_summary(s.fds),
    }

def json_path(config, suffix):
    # Derive a sibling JSON path from config.log_file by replacing the `.log` extension.
    base = config.log_file
    if base.endswith(".log"):
        base = base[:-4]
    return f"{base}.{suffix}.json"

def write_json_outputs(config, sys_state, all_proc_states):
    # Write detail.json (per-sample arrays) and/or summary.json (min/avg/p50/p95/p99/max stats), respecting
    # --detail-only / --summary-only. Returns the list of paths written.
    written = []
    metadata = {
        "hostname":         platform.node(),
        "cpu_count":        psutil.cpu_count(logical=True),
        "total_memory_gb":  psutil.virtual_memory().total / (1024**3),
        "interval_sec":     config.interval_sec,
        "system":           config.system,
        "pids":             list(config.pids),
        "filesystems":      [label for _, label in config.filesystems],
        "cpu_high_thresh":  config.cpu_high_thresh,
    }

    if not config.summary_only:
        detail_path = json_path(config, "detail")
        detail_doc = {
            "metadata":  metadata,
            "system":    sys_state.stats.sample_dicts if sys_state else [],
            "processes": [
                {
                    "pid":        ps.proc.pid,
                    "name":       ps.name,
                    "is_child":   ps.is_child,
                    "parent_pid": ps.parent_pid,
                    "samples":    ps.stats.sample_dicts,
                }
                for ps in all_proc_states
            ],
        }
        with open(detail_path, "w", encoding="utf-8") as f:
            json.dump(detail_doc, f, indent=2)
        written.append(detail_path)

    if not config.detail_only:
        summary_path = json_path(config, "summary")
        summary_doc = {
            "metadata":  metadata,
            "system":    build_system_summary(sys_state) if sys_state else None,
            "processes": [build_process_summary(ps) for ps in all_proc_states],
        }
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary_doc, f, indent=2)
        written.append(summary_path)

    return written

_top_buffer = []  # accumulator for one frame's stdout output when --top is on; flushed atomically.

def emit(out, config, line):
    # Write one line to the log, mirror to stdout if -D, flush if configured.
    # Skipped under --summary-only: emit() carries the header and per-iteration
    # rows; the summary block writes directly to `out` and bypasses this gate.
    # Under --top, stdout writes are buffered into _top_buffer and flushed in one
    # atomic write per iteration to avoid visible progressive drawing.
    if config.summary_only:
        return
    out.write(line)
    if config.display:
        if config.top:
            _top_buffer.append(line)
        else:
            sys.stdout.write(line)
    if config.flush_each_line:
        out.flush()

def print_interactive_help():
    # Single source of truth for the interactive-keys banner; reused by setup and by `?`.
    print("[RES-MON] interactive keys: d=descendants  t=thread  s=stack  +/-=interval ±5s (min 5s)  ?=help",
          file=sys.stderr)

def setup_interactive_terminal():
    # Put stdin in cbreak mode so single keys are readable without Enter.
    # Returns the saved termios state for later restore, or None if the platform/stream
    # can't deliver single keys (Windows, or stdin redirected away from a TTY).
    if not _INTERACTIVE_SUPPORTED:
        print("[RES-MON] --interactive not supported on this platform (POSIX TTY APIs unavailable); falling back to plain sleep.", file=sys.stderr)
        return None
    if not sys.stdin.isatty():
        print("[RES-MON] --interactive requires a TTY on stdin; running without interactive controls.", file=sys.stderr)
        return None
    saved = termios.tcgetattr(sys.stdin.fileno())
    tty.setcbreak(sys.stdin.fileno())
    print_interactive_help()
    return saved

def restore_terminal(saved):
    # Restore stdin's termios state if it was saved.
    if saved is not None:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)

def handle_interactive_key(config, ch):
    # Apply an interactive keystroke to config; effects take hold at the next sample.
    if ch == '?':
        print_interactive_help()
        return
    changed = False
    if ch == 'd':
        config.children = not config.children
        changed = True
    elif ch == 't':
        config.threads = not config.threads
        changed = True
    elif ch == 's':
        config.stack = not config.stack
        changed = True
    elif ch == '+':
        config.interval_sec = float(config.interval_sec) + 5.0
        changed = True
    elif ch == '-':
        config.interval_sec = max(5.0, float(config.interval_sec) - 5.0)
        changed = True
    if changed:
        print(f"[RES-MON] interactive: interval={config.interval_sec:.0f}s "
              f"threads={config.threads} descendants={config.children} stack={config.stack}",
              file=sys.stderr)

def interruptible_sleep(config, seconds):
    # Sleep `seconds`, but when --interactive, poll stdin via select and dispatch keys to handle_interactive_key.
    # Falls back to plain time.sleep when stdin is not a TTY, interactive is off, or the platform
    # can't poll a console fd via select() (Windows).
    if not config.interactive or not _INTERACTIVE_SUPPORTED or not sys.stdin.isatty():
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            r, _, _ = select.select([sys.stdin], [], [], remaining)
        except InterruptedError:
            continue
        if r:
            try:
                ch = sys.stdin.read(1)
            except (OSError, ValueError):
                return
            if ch:
                handle_interactive_key(config, ch)

def filesystem_label(path):
    # Turn a filesystem path into a short uppercase column label (e.g. /var/tmp → VAR_TMP).
    if path == "/":
        return "ROOT"

    label = path.strip("/").replace("/", "_").replace("-", "_").upper()
    return label or "ROOT"

def parse_filesystems():
    # Parse RES_MON_FILESYSTEMS, deduplicate labels, and verify each path exists.
    filesystems = []
    seen_labels = set()

    for raw_path in RES_MON_FILESYSTEMS.split():
        label = filesystem_label(raw_path)
        if label in seen_labels:
            print(f"[RES-MON] ERROR: Duplicate filesystem label '{label}' from RES_MON_FILESYSTEMS. Exiting.", file=sys.stderr)
            sys.exit(1)
        if not os.path.exists(raw_path):
            print(f"[RES-MON] ERROR: Filesystem path '{raw_path}' does not exist. Exiting.", file=sys.stderr)
            sys.exit(1)
        filesystems.append((raw_path, label))
        seen_labels.add(label)

    if not filesystems:
        print(f"[RES-MON] ERROR: RES_MON_FILESYSTEMS must contain at least one path. Exiting.", file=sys.stderr)
        sys.exit(1)

    return filesystems

def validate_and_prepare(system, pids, display, threads, children, logfile_timestamp=False,
                         detail_only=False, summary_only=False, interval_sec=None, interactive=False,
                         stack=False, top=False, json_output=False, log=False, columns=None):
    # Validate startup configuration and prepare filesystem state before sampling begins.
    if interval_sec is None:
        interval_sec = RES_MON_INTERVAL_SEC
    if interval_sec <= 0:
        print(f"[RES-MON] ERROR: interval must be greater than 0 (got {interval_sec}). "
              f"Set -i/--interval or RES_MON_INTERVAL_SEC. Exiting.", file=sys.stderr)
        sys.exit(1)

    for pid in pids:
        if not psutil.pid_exists(pid):
            print(f"[RES-MON] ERROR: No process with PID {pid}. Exiting.", file=sys.stderr)
            sys.exit(1)

    if logfile_timestamp:
        ts_part = f"-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    else:
        ts_part = ""
    log_file = os.path.join(RES_MON_LOG_DIR, f"{RES_MON_LOG_PREFIX}{ts_part}_monres.log")

    monitored_filesystems = parse_filesystems() if system else []
    log_dir = os.path.dirname(log_file)
    if log_dir and (log or json_output):
        os.makedirs(log_dir, exist_ok=True)

    return MonitorConfig(
        system=system,
        pids=pids,
        interval_sec=interval_sec,
        log_file=log_file,
        cpu_high_thresh=RES_MON_CPU_HIGH_THRESH,
        mem_high_thresh=RES_MON_MEM_HIGH_THRESH,
        iowait_high_thresh=RES_MON_IOWAIT_HIGH_THRESH,
        flush_each_line=RES_MON_FLUSH_EACH_LINE,
        display=display,
        threads=threads,
        children=children,
        filesystems=monitored_filesystems,
        ivx_high_thresh=RES_MON_IVX_HIGH_THRESH,
        health_min_samples=RES_MON_HEALTH_MIN_SAMPLES,
        logfile_timestamp=logfile_timestamp,
        detail_only=detail_only,
        summary_only=summary_only,
        interactive=interactive,
        stack=stack,
        top=top,
        json=json_output,
        log=log,
        columns=parse_columns_spec(columns),
    )

def compute_health(config, cpu_percent, state, majflt_per_sec, ivctx_per_sec,
                   stats=None, rss_mb=None, fds=None, threads=None):
    # Build a health flag string; stateless threshold checks first, then trend flags if history exists.
    flags = []

    # Stateless: current snapshot is enough to decide.
    # NOTE: both process and thread CPU use the same threshold (cpu_high_thresh).
    # Future: RES_MON_PROC_CPU_HIGH_THRESH / RES_MON_THREAD_CPU_HIGH_THRESH if one value doesn't fit both.
    if cpu_percent >= config.cpu_high_thresh:
        flags.append("CPU")
    if state == "zombie":
        flags.append("ZMB")
    if state == "disk-sleep":
        flags.append("DSK")
    if majflt_per_sec > 0:
        flags.append("MJF")
    if ivctx_per_sec >= config.ivx_high_thresh:
        flags.append("IVX")

    # Trend flags are process-only by design; threads have no per-thread history.
    # Require health_min_samples before flagging, then compare against a baseline + margin
    # derived from the first sample: avoids false positives from normal startup growth.
    n = config.health_min_samples
    if stats and len(stats.rss_mb) >= n and rss_mb is not None:
        base = stats.rss_mb[0]
        if rss_mb > max(base * 1.10, base + 50.0):
            flags.append("MLK")
    if stats and len(stats.fds) >= n and fds is not None:
        base = stats.fds[0]
        if fds > max(base * 1.25, base + 16):
            flags.append("FDL")
    if stats and len(stats.threads) >= n and threads is not None:
        base = stats.threads[0]
        if threads > max(base * 1.25, base + 8):
            flags.append("THD")

    return ",".join(flags) if flags else "-"

# --- Metric column registries -------------------------------------------------
# Single source of truth for the selectable metric columns. The identity/time
# columns (TIME, ELAPSED, PID, NAME) are intentionally NOT here: they anchor the
# process/thread tree rendering and the stack-indent offset constants, so they
# are always present and handled directly in the *_header_str/*_row_str builders.
# Each entry's first field is the lowercase key accepted by RES_MON_COLUMNS /
# --columns; column_enabled() filters by it. Keeping header and row layout in one
# table keeps their widths in lockstep.

# (key, label, width, value_fn) -- system columns are all right-aligned.
SYSTEM_METRIC_COLUMNS = [
    ("cpu",    "CPU%",     8,  lambda s: f"{s.cpu_usage:6.1f}"),
    ("usr",    "USR%",     8,  lambda s: f"{s.cpu_usr_usage:6.1f}"),
    ("sys",    "SYS%",     8,  lambda s: f"{s.cpu_sys_usage:6.1f}"),
    ("busy",   "BUSY",     6,  lambda s: f"{int(s.busy_cores):>2d}"),
    ("ctx",    "CTX_SW/s", 10, lambda s: f"{s.ctx_switches:8.0f}"),
    ("load1",  "LOAD1",    8,  lambda s: f"{s.load_average:6.2f}"),
    ("iowait", "IOWAIT%",  9,  lambda s: f"{s.cpu_iowait_usage:6.2f}"),
    ("mem",    "MEM%",     8,  lambda s: f"{s.mem_usage_percent:6.1f}"),
    ("mem_gb", "MEM_GB",   10, lambda s: f"{s.mem_usage_gb:8.2f}"),
    ("swp",    "SWP_GB",   10, lambda s: f"{s.swap_used_gb:8.2f}"),
    ("dsk_r",  "DSK_R/s",  10, lambda s: f"{s.disk_read_mb:8.2f}"),
    ("dsk_w",  "DSK_W/s",  10, lambda s: f"{s.disk_write_mb:8.2f}"),
    ("net_r",  "NET_R/s",  10, lambda s: f"{s.network_receive_mb:8.2f}"),
    ("net_s",  "NET_S/s",  10, lambda s: f"{s.network_send_mb:8.2f}"),
]

# (key, label, width, align, proc_fn, thread_fn) -- thread_fn=None renders "-"
# for columns that have no per-thread meaning (cumulative times, RSS/VMS, FDs, ...).
PROCESS_METRIC_COLUMNS = [
    ("state",    "STATE",       12, "<", lambda s: s.state,                    lambda s: s.state),
    ("health",   "HEALTH",      15, "<", lambda s: s.health,                   lambda s: s.health),
    ("cpu",      "CPU%",         8, ">", lambda s: f"{s.cpu_percent:.1f}",     lambda s: f"{s.cpu_percent:.1f}"),
    ("usr",      "USR%",         8, ">", lambda s: f"{s.usr_percent:.1f}",     lambda s: f"{s.usr_percent:.1f}"),
    ("sys",      "SYS%",         8, ">", lambda s: f"{s.sys_percent:.1f}",     lambda s: f"{s.sys_percent:.1f}"),
    ("user_sec", "USER_SEC",    10, ">", lambda s: f"{s.user_sec:.1f}",        None),
    ("sys_sec",  "SYS_SEC",     10, ">", lambda s: f"{s.sys_sec:.1f}",         None),
    ("rss",      "RSS_MB",      10, ">", lambda s: f"{s.rss_mb:.1f}",          None),
    ("vms",      "VMS_MB",      10, ">", lambda s: f"{s.vms_mb:.1f}",          None),
    ("threads",  "THREADS",      8, ">", lambda s: f"{s.threads:d}",           None),
    ("vctx",     "VCTX/s",       9, ">", lambda s: f"{s.vctx_per_sec:.1f}",    lambda s: f"{s.vctx_per_sec:.1f}"),
    ("ivctx",    "IVCTX/s",      9, ">", lambda s: f"{s.ivctx_per_sec:.1f}",   lambda s: f"{s.ivctx_per_sec:.1f}"),
    ("minflt",   "MINFLT/s",    10, ">", lambda s: f"{s.minflt_per_sec:.1f}",  lambda s: f"{s.minflt_per_sec:.1f}"),
    ("majflt",   "MAJFLT/s",    10, ">", lambda s: f"{s.majflt_per_sec:.1f}",  lambda s: f"{s.majflt_per_sec:.1f}"),
    ("read",     "READ_MB/s",   11, ">", lambda s: f"{s.read_mb_per_sec:.2f}", None),
    ("write",    "WRITE_MB/s",  11, ">", lambda s: f"{s.write_mb_per_sec:.2f}",None),
    ("fds",      _FD_COL_LABEL,  5, ">", lambda s: f"{s.fds:d}",               None),
]

SYSTEM_METRIC_KEYS  = [c[0] for c in SYSTEM_METRIC_COLUMNS]
PROCESS_METRIC_KEYS = [c[0] for c in PROCESS_METRIC_COLUMNS]
# Union preserving order, system keys first, for help text and validation messages.
ALL_METRIC_KEYS = SYSTEM_METRIC_KEYS + [k for k in PROCESS_METRIC_KEYS if k not in SYSTEM_METRIC_KEYS]

def parse_columns_spec(raw):
    # Parse a RES_MON_COLUMNS / --columns value into a set of enabled metric keys.
    # Accepts comma- and/or whitespace-separated tokens, case-insensitive. Returns
    # None (meaning "all columns") for an unset/empty value or the literal "all".
    # Exits with an error listing valid keys if any token is unrecognized.
    if raw is None:
        return None
    tokens = [t for t in raw.replace(",", " ").split()]
    tokens = [t.lower() for t in tokens]
    if not tokens or tokens == ["all"]:
        return None
    known = set(ALL_METRIC_KEYS)
    unknown = [t for t in tokens if t not in known]
    if unknown:
        print(f"[RES-MON] ERROR: unknown column key(s): {', '.join(unknown)}. "
              f"Valid keys: {', '.join(ALL_METRIC_KEYS)} (or 'all'). Exiting.", file=sys.stderr)
        sys.exit(1)
    return set(tokens)

def column_enabled(config, key):
    # A metric column is shown when no selection was made (columns is None) or its key was selected.
    return config.columns is None or key in config.columns

def system_header_str(config):
    # Build the fixed-width system stats header string (filesystem columns are dynamic).
    cols = [("TIME(LOCAL)", 11), ("ELAPSED", 10)]
    cols += [(label, width) for key, label, width, _ in SYSTEM_METRIC_COLUMNS if column_enabled(config, key)]
    cols += [(f"{label}_GB", 10) for _, label in config.filesystems]
    return " ".join(f"{name:>{width}}" for name, width in cols)

def system_row_str(config, sample):
    # Format one system stats row; column widths must match system_header_str.
    row = [(sample.time_str, 11), (sample.elapsed_str, 10)]
    row += [(fn(sample), width) for key, _, width, fn in SYSTEM_METRIC_COLUMNS if column_enabled(config, key)]
    row += [(f"{sample.filesystem_usage_gb[label]:8.2f}", 10) for _, label in config.filesystems]
    return " ".join(f"{val:>{w}}" for val, w in row)

def process_header_str(config, include_time=True):
    # Build the process stats header; drop time cols when combined with system output.
    cols = []
    if include_time:
        cols += [(">", "TIME(LOCAL)", 11), (">", "ELAPSED", 10)]
    cols += [(">", "PID", 8), ("<", "NAME", 45)]
    cols += [(align, label, width) for key, label, width, align, _, _ in PROCESS_METRIC_COLUMNS
             if column_enabled(config, key)]
    return " ".join(f"{label:{align}{width}}" for align, label, width in cols)

def process_row_str(config, sample, include_time=True, name_prefix=""):
    # Format one process stats row; column widths must match process_header_str.
    cols = []
    if include_time:
        cols += [(">", sample.time_str, 11), (">", sample.elapsed_str, 10)]
    cols += [(">", f"{sample.pid:d}", 8), ("<", (name_prefix + sample.name)[:45], 45)]
    cols += [(align, fn(sample), width) for key, _, width, align, fn, _ in PROCESS_METRIC_COLUMNS
             if column_enabled(config, key)]
    return " ".join(f"{val:{align}{width}}" for align, val, width in cols)

def thread_row_str(config, sample, include_time=True, name_prefix=""):
    # Uses the same column layout as process rows; TID fills PID, columns with no per-thread
    # meaning render "-" (thread_fn is None in the registry).
    cols = []
    if include_time:
        cols += [(">", "-", 11), (">", "-", 10)]  # TIME, ELAPSED not applicable for threads
    cols += [(">", f"{sample.tid:d}", 8), ("<", (name_prefix + sample.name)[:45], 45)]
    for key, _, width, align, _, thread_fn in PROCESS_METRIC_COLUMNS:
        if not column_enabled(config, key):
            continue
        cols.append((align, thread_fn(sample) if thread_fn else "-", width))
    return " ".join(f"{val:{align}{width}}" for align, val, width in cols)

def compute_log_header(config):
    # Build the column header string for the current mode (sys-only, proc-only, or combined).
    if config.system and config.pids:
        return f"{system_header_str(config)} | {process_header_str(config, include_time=False)}"
    elif config.system:
        return system_header_str(config)
    else:
        return process_header_str(config)

def write_log_header(out, config):
    # Write the column header for the current mode to the log (and to stdout via emit when --display).
    emit(out, config, f"\n{compute_log_header(config)}\n")

def top_frame_start(config):
    # Begin a new frame: reset the buffer and seed it with the column header.
    # Called once per iteration, after the sample window closes, before any row is emitted.
    if not config.top:
        return
    _top_buffer.clear()
    _top_buffer.append(compute_log_header(config) + "\n")

def top_frame_flush(config):
    # End of frame: clear the screen and write the buffered frame in one atomic stdout write.
    # No-op if --top is off or the buffer is empty (no rows produced this iteration).
    if not config.top or not _top_buffer:
        return
    sys.stdout.write("\033[2J\033[H" + "".join(_top_buffer))
    sys.stdout.flush()
    _top_buffer.clear()

def initialize_monitor_state(config):
    # Prime psutil and capture the initial counters, baselines, and timestamps for sampling.
    psutil.cpu_percent(interval=None, percpu=True)
    psutil.cpu_times_percent(interval=None)

    return MonitorState(
        last_disk=psutil.disk_io_counters(),
        last_net=psutil.net_io_counters(),
        last_ctx=psutil.cpu_stats(),
        start_monotonic=time.monotonic(),
        start_local=datetime.datetime.now().astimezone(),
        stats=MonitorStats(
            filesystem_usage_gb={label: [] for _, label in config.filesystems}
        ),
    )

def collect_sample(config, state):
    # Gather one monitoring interval of data, then fold the resulting sample into the running stats.
    # Sleep first so interactive keystrokes are responsive, then take instant readings; psutil's
    # cpu_percent/cpu_times_percent were primed in initialize_monitor_state, so interval=None returns
    # the average since the previous call (i.e. across the sleep window we just performed).
    loop_start_time = time.monotonic()

    interruptible_sleep(config, config.interval_sec)

    percpu = psutil.cpu_percent(interval=None, percpu=True) or []
    cpu_times = psutil.cpu_times_percent(interval=None)
    cpu_iowait_usage = getattr(cpu_times, "iowait", 0.0)
    cpu_usr_usage    = getattr(cpu_times, "user",   0.0)
    cpu_sys_usage    = getattr(cpu_times, "system", 0.0)

    load_average = read_loadavg_1min()
    vm = psutil.virtual_memory()
    cur_disk = psutil.disk_io_counters()
    cur_net = psutil.net_io_counters()
    cur_ctx = psutil.cpu_stats()
    swap = psutil.swap_memory()
    filesystem_usage_gb = {
        label: psutil.disk_usage(path).used / (1024**3)
        for path, label in config.filesystems
    }

    loop_duration = time.monotonic() - loop_start_time
    if loop_duration <= 0:
        loop_duration = 0.001

    elapsed_time = time.monotonic() - state.start_monotonic

    sample = Sample(
        time_str=datetime.datetime.now().astimezone().strftime("%H:%M:%S"),
        elapsed_str=str(datetime.timedelta(seconds=int(elapsed_time))),
        cpu_usage=sum(percpu) / len(percpu) if percpu else 0.0,
        busy_cores=sum(1 for x in percpu if x >= config.cpu_high_thresh),
        cpu_usr_usage=cpu_usr_usage,
        cpu_sys_usage=cpu_sys_usage,
        load_average=load_average,
        cpu_iowait_usage=cpu_iowait_usage,
        mem_usage_percent=vm.percent,
        mem_usage_gb=vm.used / (1024**3),
        disk_read_mb=(cur_disk.read_bytes - state.last_disk.read_bytes) / (1024**2) / loop_duration,
        disk_write_mb=(cur_disk.write_bytes - state.last_disk.write_bytes) / (1024**2) / loop_duration,
        network_receive_mb=(cur_net.bytes_recv - state.last_net.bytes_recv) / (1024**2) / loop_duration,
        network_send_mb=(cur_net.bytes_sent - state.last_net.bytes_sent) / (1024**2) / loop_duration,
        swap_used_gb=swap.used / (1024**3),
        ctx_switches=(cur_ctx.ctx_switches - state.last_ctx.ctx_switches) / loop_duration,
        filesystem_usage_gb=filesystem_usage_gb,
    )
    state.last_disk, state.last_net, state.last_ctx = cur_disk, cur_net, cur_ctx

    state.stats.cpu_usage.append(sample.cpu_usage)
    state.stats.busy_cores.append(sample.busy_cores)
    state.stats.cpu_usr_usage.append(sample.cpu_usr_usage)
    state.stats.cpu_sys_usage.append(sample.cpu_sys_usage)
    state.stats.load_average.append(sample.load_average)
    state.stats.cpu_iowait_usage.append(sample.cpu_iowait_usage)
    state.stats.mem_usage_percent.append(sample.mem_usage_percent)
    state.stats.mem_usage_gb.append(sample.mem_usage_gb)
    state.stats.disk_read_mb.append(sample.disk_read_mb)
    state.stats.disk_write_mb.append(sample.disk_write_mb)
    state.stats.network_receive_mb.append(sample.network_receive_mb)
    state.stats.network_send_mb.append(sample.network_send_mb)
    state.stats.swap_used_gb.append(sample.swap_used_gb)
    state.stats.ctx_switches.append(sample.ctx_switches)
    for label, value in sample.filesystem_usage_gb.items():
        state.stats.filesystem_usage_gb[label].append(value)
    if config.json:
        state.stats.sample_dicts.append(system_sample_to_dict(sample))

    return sample

def write_sample_row(out, config, sample):
    # Format and write one sampled datapoint using the same layout as the header.
    emit(out, config, system_row_str(config, sample) + "\n")

def write_summary_block(out, config, title, start_local, series, show_timing=False):
    # Shared summary writer: optional timing header + min/avg/p50/p95/p99/max for each (label, values) pair.
    lines = [f"\n{title}:\n"]
    if show_timing:
        end_local = datetime.datetime.now().astimezone()
        total_elapsed = end_local - start_local
        lines += [
            f"   Start Time (Local): {start_local.strftime('%Y-%m-%d %H:%M:%S %Z')}\n",
            f"   End Time (Local):   {end_local.strftime('%Y-%m-%d %H:%M:%S %Z')}\n",
            f"   Elapsed Time:       {str(total_elapsed).split('.')[0]}\n",
            "\n",
        ]
    for label, vals in series:
        lines.append(f"   {summary_line(label, vals)}\n")
    for line in lines:
        out.write(line)
    if config.display:
        sys.stdout.writelines(lines)
    out.flush()

def write_summary(out, config, state, show_timing=False):
    # Write system summary: timing + min/avg/p50/p95/p99/max for all system series.
    series = [
        ("cpu",    "CPU%",         state.stats.cpu_usage),
        ("busy",   "BUSY (cores)", state.stats.busy_cores),
        ("usr",    "USR%",         state.stats.cpu_usr_usage),
        ("sys",    "SYS%",         state.stats.cpu_sys_usage),
        ("ctx",    "CTX_SW/s",     state.stats.ctx_switches),
        ("load1",  "LOAD1",        state.stats.load_average),
        ("iowait", "IOWAIT%",      state.stats.cpu_iowait_usage),
        ("mem",    "MEM%",         state.stats.mem_usage_percent),
        ("mem_gb", "MEM (GB)",     state.stats.mem_usage_gb),
        ("swp",    "SWP_GB",       state.stats.swap_used_gb),
        ("dsk_r",  "DSK_R/s",      state.stats.disk_read_mb),
        ("dsk_w",  "DSK_W/s",      state.stats.disk_write_mb),
        ("net_r",  "NET_R/s",      state.stats.network_receive_mb),
        ("net_s",  "NET_S/s",      state.stats.network_send_mb),
    ]
    series = [(label, vals) for key, label, vals in series if column_enabled(config, key)]
    series += [(f"{label}_GB", state.stats.filesystem_usage_gb[label]) for _, label in config.filesystems]
    write_summary_block(out, config, "SUMMARY", state.start_local, series, show_timing=show_timing)

def parse_args():
    # Parse command-line arguments; return the same flag tuple ordering as the destructure in main():
    # (system, pids, display, threads, children, logfile_timestamp, detail_only, summary_only,
    #  interval_sec, interactive, stack, top, json_output, log, columns).
    args_iter = iter(sys.argv[1:])
    system             = False
    pids               = []
    display            = False
    threads            = False
    children           = False
    logfile_timestamp  = False
    detail_only        = RES_MON_DETAIL_ONLY
    summary_only       = RES_MON_SUMMARY_ONLY
    interval_sec       = RES_MON_INTERVAL_SEC
    interactive        = False
    stack              = RES_MON_STACK
    top                = False
    json_output        = RES_MON_JSON
    log                = RES_MON_LOG
    columns            = RES_MON_COLUMNS

    for arg in args_iter:
        if arg in ('-h', '--help'):
            usage(sys.stdout)
            sys.exit(0)
        elif arg in ('-p', '--pid'):
            try:
                pids.append(int(next(args_iter)))
            except (StopIteration, ValueError):
                print("Error: -p/--pid requires a numeric PID argument.", file=sys.stderr)
                sys.exit(1)
        elif arg in ('-i', '--interval'):
            try:
                interval_sec = float(next(args_iter))
            except (StopIteration, ValueError):
                print("Error: -i/--interval requires a numeric seconds argument.", file=sys.stderr)
                sys.exit(1)
        elif arg in ('-S', '--system'):
            system = True
        elif arg in ('-s', '--stack'):
            stack = True
        elif arg in ('-D', '--display'):
            display = True
        elif arg in ('-t', '--thread'):
            threads = True
        elif arg in ('-d', '--descendants'):
            children = True
        elif arg in ('-I', '--interactive'):
            interactive = True
        elif arg in ('-T', '--top'):
            top = True
            display = True  # --top requires stdout output to be meaningful
        elif arg in ('-j', '--json'):
            json_output = True
        elif arg in ('-l', '--log'):
            log = True
        elif arg == '--columns':
            try:
                columns = next(args_iter)
            except StopIteration:
                print("Error: --columns requires a comma-separated list of column keys (or 'all').", file=sys.stderr)
                sys.exit(1)
        elif arg == '--timestamp':
            logfile_timestamp = True
        elif arg == '--detail-only':
            detail_only = True
        elif arg == '--summary-only':
            summary_only = True
        else:
            print(f"Error: Unknown argument '{arg}'.", file=sys.stderr)
            sys.exit(1)

    if detail_only and summary_only:
        print("Error: --detail-only and --summary-only are mutually exclusive "
              "(also applies to RES_MON_DETAIL_ONLY / RES_MON_SUMMARY_ONLY).", file=sys.stderr)
        sys.exit(1)

    if not system and not pids:
        system = True  # default to system-wide monitoring if nothing specified

    return system, pids, display, threads, children, logfile_timestamp, detail_only, summary_only, interval_sec, interactive, stack, top, json_output, log, columns

def read_open_handle_count(proc):
    # Number of open kernel handles for a process. Tries POSIX num_fds(), falls back to
    # Windows num_handles(); returns 0 on any platform that exposes neither.
    for attr in ("num_fds", "num_handles"):
        fn = getattr(proc, attr, None)
        if fn is None:
            continue
        try:
            return fn()
        except (psutil.AccessDenied, NotImplementedError):
            return 0
    return 0

def read_loadavg_1min():
    # 1-minute load average, or 0.0 on platforms (e.g. Windows) that don't expose it.
    if hasattr(psutil, "getloadavg"):
        try:
            return psutil.getloadavg()[0]
        except (OSError, NotImplementedError):
            pass
    if hasattr(os, "getloadavg"):
        try:
            return os.getloadavg()[0]
        except OSError:
            pass
    return 0.0

def read_proc_page_faults(pid, tid=None):
    # Read minor and major page fault counts from /proc/<pid>/stat (process) or
    # /proc/<pid>/task/<tid>/stat (thread). Field indices: minflt=9, majflt=11 (0-indexed,
    # per proc(5)). Returns (0, 0) when /proc isn't available or the task exited.
    path = f"/proc/{pid}/task/{tid}/stat" if tid is not None else f"/proc/{pid}/stat"
    try:
        with open(path) as f:
            fields = f.read().split()
        return int(fields[9]), int(fields[11])
    except (OSError, IndexError, ValueError):
        return 0, 0

def read_thread_state(pid, tid):
    # Read thread run state from /proc/<pid>/task/<tid>/status; returns word form e.g. "sleeping".
    try:
        with open(f"/proc/{pid}/task/{tid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    parts = line.split()
                    if len(parts) >= 3:
                        return parts[2].strip("()")  # "State:\tS (sleeping)" → "sleeping"
                    elif len(parts) >= 2:
                        return parts[1]
    except (OSError, ValueError):
        pass
    return "?"

def read_thread_name(pid, tid):
    # Read thread name from /proc/<pid>/task/<tid>/comm; fall back to tid string.
    try:
        with open(f"/proc/{pid}/task/{tid}/comm") as f:
            return f.read().strip()
    except OSError:
        return str(tid)

def read_thread_stack(pid, tid):
    # Read kernel stack frames from /proc/<pid>/task/<tid>/stack; returns [] on permission error or absence.
    # Reading this file typically requires CAP_SYS_PTRACE or matching uid; empty result is normal otherwise.
    try:
        with open(f"/proc/{pid}/task/{tid}/stack") as f:
            return [line.rstrip() for line in f if line.strip()]
    except OSError:
        return []

def read_process_stack(pid):
    # Kernel stack of a process's main thread; equivalent to /proc/<pid>/task/<pid>/stack.
    try:
        with open(f"/proc/{pid}/stack") as f:
            return [line.rstrip() for line in f if line.strip()]
    except OSError:
        return []

_NAME_COL_OFFSET_WITH_TIME = 11 + 1 + 10 + 1 + 8 + 1   # TIME(11) + sp + ELAPSED(10) + sp + PID(8) + sp
_NAME_COL_OFFSET_NO_TIME   = 8 + 1                      # PID(8) + sp

def _stack_indent(name_prefix, include_time):
    # Spaces needed to position a stack frame just past the NAME column's name_prefix, plus a
    # 4-space inset so frames are clearly indented under the row they belong to.
    base = _NAME_COL_OFFSET_WITH_TIME if include_time else _NAME_COL_OFFSET_NO_TIME
    return " " * (base + len(name_prefix) + 4)

def emit_proc_stack_if_hot(out, config, sample, line_prefix="", name_prefix="", include_time=True):
    # Emit /proc/<pid>/stack frames indented under a process row when --stack is on and CPU >= threshold.
    # include_time mirrors the corresponding process_row_str(include_time=...) so frames align
    # under the NAME column whether or not the row carries TIME/ELAPSED prefix columns.
    if not config.stack or sample.cpu_percent < config.cpu_high_thresh:
        return
    frames = read_process_stack(sample.pid)
    stack_indent = _stack_indent(name_prefix, include_time)
    for frame in frames:
        emit(out, config, f"{line_prefix}{stack_indent}{frame}\n")

def build_display_name(proc):
    # Build a display name for a process: executable name + truncated cmdline args if any.
    # Appending args helps distinguish child processes that share the same executable name
    # (e.g., multiple guacd or cmake forks with different arguments).
    try:
        cmdline = proc.cmdline()
        if len(cmdline) > 1:
            args = " ".join(cmdline[1:])
            return f"{proc.name()} {args}"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return proc.name()

def read_thread_ctx_switches(pid, tid):
    # Read voluntary/involuntary context switches for a thread from /proc/<pid>/task/<tid>/status.
    try:
        vctx = ivctx = 0
        with open(f"/proc/{pid}/task/{tid}/status") as f:
            for line in f:
                if line.startswith("voluntary_ctxt_switches:"):
                    vctx = int(line.split()[1])
                elif line.startswith("nonvoluntary_ctxt_switches:"):
                    ivctx = int(line.split()[1])
        return vctx, ivctx
    except (OSError, ValueError):
        return 0, 0

def initialize_process_state(pid, is_child=False, parent_pid=0):
    # Prime psutil's CPU counter and capture baseline state for the target process.
    proc = psutil.Process(pid)
    proc.cpu_percent(interval=None)
    io = proc.io_counters()
    return ProcessState(
        proc=proc,
        name=build_display_name(proc),
        last_cpu_times=proc.cpu_times(),
        last_ctx_switches=proc.num_ctx_switches(),
        last_page_faults=read_proc_page_faults(pid),
        last_io_counters=io,
        start_monotonic=time.monotonic(),
        start_local=datetime.datetime.now().astimezone(),
        stats=ProcessStats(),
        is_child=is_child,
        parent_pid=parent_pid,
    )


def write_process_sample_row(out, config, sample, name_prefix=""):
    # Format and write one per-process datapoint using the same layout as the header.
    emit(out, config, process_row_str(config, sample, name_prefix=name_prefix) + "\n")
    emit_proc_stack_if_hot(out, config, sample, name_prefix=name_prefix, include_time=True)

def write_process_summary(out, config, state, show_timing=False):
    # Write per-process summary: timing + min/avg/p50/p95/p99/max for all process series.
    # Skip processes that exited before any sample was collected (all series empty).
    if not state.stats.cpu_percent:
        return
    series = [
        ("cpu",      "CPU%",       state.stats.cpu_percent),
        ("usr",      "USR%",       state.stats.usr_percent),
        ("sys",      "SYS%",       state.stats.sys_percent),
        ("user_sec", "USER_SEC",   state.stats.user_sec),
        ("sys_sec",  "SYS_SEC",    state.stats.sys_sec),
        ("rss",      "RSS_MB",     state.stats.rss_mb),
        ("vms",      "VMS_MB",     state.stats.vms_mb),
        ("threads",  "THREADS",    state.stats.threads),
        ("vctx",     "VCTX/s",     state.stats.vctx_per_sec),
        ("ivctx",    "IVCTX/s",    state.stats.ivctx_per_sec),
        ("minflt",   "MINFLT/s",   state.stats.minflt_per_sec),
        ("majflt",   "MAJFLT/s",   state.stats.majflt_per_sec),
        ("read",     "READ_MB/s",  state.stats.read_mb_per_sec),
        ("write",    "WRITE_MB/s", state.stats.write_mb_per_sec),
        ("fds",      _FD_COL_LABEL, state.stats.fds),
    ]
    series = [(label, vals) for key, label, vals in series if column_enabled(config, key)]
    write_summary_block(out, config, f"SUMMARY (PID {state.proc.pid} {state.name})", state.start_local, series, show_timing=show_timing)

def thread_is_notable(ts):
    # True if the thread shows any measurable activity or concerning state worth displaying.
    return (
        ts.cpu_percent  >= 0.1
        or ts.vctx_per_sec  >= 1
        or ts.ivctx_per_sec >= 1
        or ts.minflt_per_sec > 0
        or ts.majflt_per_sec > 0
        or ts.state in ("running", "disk-sleep", "zombie")
        or ts.health != "-"
    )

def filter_notable_threads(samples):
    # Drop idle/quiescent threads; keep any that cross an activity or health threshold.
    return [ts for ts in samples if thread_is_notable(ts)]

def write_thread_rows(out, config, thread_samples, include_time=True, line_prefix="", name_prefix="", more_follows=False):
    # Filter to notable threads (already sorted by CPU% desc), then write; last gets └─.
    # When --stack is on, each thread whose CPU% meets cpu_high_thresh is followed by its kernel
    # stack frames from /proc/<pid>/task/<tid>/stack, indented under the row.
    visible = filter_notable_threads(thread_samples)
    if not visible:
        return
    last_pfx = name_prefix if more_follows else name_prefix.replace("┣─", "┗─")
    for i, ts in enumerate(visible):
        pfx = last_pfx if i == len(visible) - 1 else name_prefix
        emit(out, config, line_prefix + thread_row_str(config, ts, include_time=include_time, name_prefix=pfx) + "\n")
        if config.stack and ts.cpu_percent >= config.cpu_high_thresh:
            frames = read_thread_stack(ts.pid, ts.tid)
            stack_indent = _stack_indent(pfx, include_time)
            for frame in frames:
                emit(out, config, f"{line_prefix}{stack_indent}{frame}\n")

def collect_thread_samples(config, proc_state, dt):
    # Collect per-thread CPU and context-switch rates; initialises new threads and removes dead ones.
    pid = proc_state.proc.pid
    samples = []
    try:
        threads = sorted(proc_state.proc.threads(), key=lambda t: t.id)
    except psutil.NoSuchProcess:
        return samples

    if len(threads) <= 1:
        return samples

    elapsed_time = time.monotonic() - proc_state.start_monotonic
    time_str     = datetime.datetime.now().astimezone().strftime("%H:%M:%S")
    elapsed_str  = str(datetime.timedelta(seconds=int(elapsed_time)))

    for t in threads:
        tid             = t.id
        raw_name        = read_thread_name(pid, tid)
        if tid == pid:
            name = f"[main] {raw_name}"
        elif raw_name == proc_state.proc.name():
            # Thread has no custom name (comm == process name); append TID to distinguish.
            name = f"{raw_name}[{tid}]"
        else:
            name = raw_name
        state           = read_thread_state(pid, tid)
        vctx, ivctx     = read_thread_ctx_switches(pid, tid)
        minflt, majflt  = read_proc_page_faults(pid, tid)  # /proc/<pid>/task/<tid>/stat

        if tid in proc_state.thread_states:
            ts = proc_state.thread_states[tid]
            usr_percent    = (t.user_time   - ts.last_user_time)   / dt * 100
            sys_percent    = (t.system_time - ts.last_system_time) / dt * 100
            cpu_percent    = usr_percent + sys_percent
            vctx_per_sec   = (vctx   - ts.last_vctx)   / dt
            ivctx_per_sec  = (ivctx  - ts.last_ivctx)  / dt
            minflt_per_sec = (minflt - ts.last_minflt)  / dt
            majflt_per_sec = (majflt - ts.last_majflt)  / dt
        else:
            cpu_percent = usr_percent = sys_percent = 0.0
            vctx_per_sec = ivctx_per_sec = minflt_per_sec = majflt_per_sec = 0.0

        proc_state.thread_states[tid] = ThreadState(
            last_user_time=t.user_time,
            last_system_time=t.system_time,
            last_vctx=vctx,
            last_ivctx=ivctx,
            last_minflt=minflt,
            last_majflt=majflt,
        )
        samples.append(ThreadSample(
            time_str=time_str,
            elapsed_str=elapsed_str,
            pid=pid,
            tid=tid,
            name=name,
            state=state,
            health=compute_health(config, cpu_percent, state, majflt_per_sec, ivctx_per_sec),
            cpu_percent=cpu_percent,
            usr_percent=usr_percent,
            sys_percent=sys_percent,
            vctx_per_sec=vctx_per_sec,
            ivctx_per_sec=ivctx_per_sec,
            minflt_per_sec=minflt_per_sec,
            majflt_per_sec=majflt_per_sec,
        ))

    # Remove state for threads that no longer exist.
    live_tids = {t.id for t in threads}
    for tid in list(proc_state.thread_states):
        if tid not in live_tids:
            del proc_state.thread_states[tid]

    return sorted(samples, key=lambda ts: ts.cpu_percent, reverse=True)

def collect_process_sample_instant(config, proc_state, dt):
    # Collect process metrics without blocking; uses cpu_percent(interval=None) primed at init.
    proc = proc_state.proc
    cpu_percent    = proc.cpu_percent(interval=None)
    cpu_times      = proc.cpu_times()
    mem_info       = proc.memory_info()
    threads        = proc.num_threads()
    ctx            = proc.num_ctx_switches()
    minflt, majflt = read_proc_page_faults(proc.pid)
    io             = proc.io_counters()
    fds            = read_open_handle_count(proc)

    if dt <= 0:
        dt = 0.001

    elapsed_time   = time.monotonic() - proc_state.start_monotonic
    usr_percent    = (cpu_times.user   - proc_state.last_cpu_times.user)   / dt * 100
    sys_percent    = (cpu_times.system - proc_state.last_cpu_times.system) / dt * 100
    proc_state_str = proc.status()
    ivctx_per_sec  = (ctx.involuntary  - proc_state.last_ctx_switches.involuntary) / dt
    majflt_per_sec = (majflt - proc_state.last_page_faults[1]) / dt
    rss_mb         = mem_info.rss / (1024**2)
    health         = compute_health(config, cpu_percent, proc_state_str, majflt_per_sec, ivctx_per_sec,
                                    stats=proc_state.stats, rss_mb=rss_mb, fds=fds, threads=threads)

    sample = ProcessSample(
        time_str       = datetime.datetime.now().astimezone().strftime("%H:%M:%S"),
        elapsed_str    = str(datetime.timedelta(seconds=int(elapsed_time))),
        pid            = proc.pid,
        name           = proc_state.name,
        state          = proc_state_str,
        health         = health,
        cpu_percent    = cpu_percent,
        usr_percent    = usr_percent,
        sys_percent    = sys_percent,
        user_sec       = cpu_times.user,
        sys_sec        = cpu_times.system,
        rss_mb         = rss_mb,
        vms_mb         = mem_info.vms / (1024**2),
        threads        = threads,
        vctx_per_sec   = (ctx.voluntary - proc_state.last_ctx_switches.voluntary) / dt,
        ivctx_per_sec  = ivctx_per_sec,
        minflt_per_sec = (minflt - proc_state.last_page_faults[0]) / dt,
        majflt_per_sec = majflt_per_sec,
        read_mb_per_sec  = (io.read_bytes  - proc_state.last_io_counters.read_bytes)  / (1024**2) / dt,
        write_mb_per_sec = (io.write_bytes - proc_state.last_io_counters.write_bytes) / (1024**2) / dt,
        fds = fds,
    )

    proc_state.last_cpu_times    = cpu_times
    proc_state.last_ctx_switches = ctx
    proc_state.last_page_faults  = (minflt, majflt)
    proc_state.last_io_counters  = io

    proc_state.stats.cpu_percent.append(sample.cpu_percent)
    proc_state.stats.usr_percent.append(sample.usr_percent)
    proc_state.stats.sys_percent.append(sample.sys_percent)
    proc_state.stats.user_sec.append(sample.user_sec)
    proc_state.stats.sys_sec.append(sample.sys_sec)
    proc_state.stats.rss_mb.append(sample.rss_mb)
    proc_state.stats.vms_mb.append(sample.vms_mb)
    proc_state.stats.threads.append(sample.threads)
    proc_state.stats.vctx_per_sec.append(sample.vctx_per_sec)
    proc_state.stats.ivctx_per_sec.append(sample.ivctx_per_sec)
    proc_state.stats.minflt_per_sec.append(sample.minflt_per_sec)
    proc_state.stats.majflt_per_sec.append(sample.majflt_per_sec)
    proc_state.stats.read_mb_per_sec.append(sample.read_mb_per_sec)
    proc_state.stats.write_mb_per_sec.append(sample.write_mb_per_sec)
    proc_state.stats.fds.append(sample.fds)
    if config.json:
        proc_state.stats.sample_dicts.append(process_sample_to_dict(sample))

    return sample

def write_combined_row(out, config, sys_sample, proc_sample):
    # Write one combined row: sys stats | proc stats (time shown once on the sys side).
    emit(out, config, f"{system_row_str(config, sys_sample)} | {process_row_str(config, proc_sample, include_time=False)}\n")
    sys_blank = " " * len(system_header_str(config))
    emit_proc_stack_if_hot(out, config, proc_sample, line_prefix=f"{sys_blank} | ", include_time=False)

_denied_child_pids = set()  # PIDs we couldn't initialize due to permission; skip on re-discovery.

def walk_process_tree(root_ps, child_map):
    # Recursively yield (proc_state, name_prefix, thread_indent) for `root_ps` and all its tracked
    # descendants in display order. name_prefix is the box-drawing string to prepend to the row's
    # NAME field; thread_indent is the corresponding prefix for thread rows under that node.
    # The root is yielded first with an empty name_prefix and "┣─ " thread_indent; deeper nodes
    # build their prefix from each ancestor's "more siblings to come" state so the tree renders
    # with proper vertical-line continuations at every depth.
    yield from _walk_subtree(root_ps, child_map, [], is_last=True, depth=0)

def _walk_subtree(node, child_map, ancestor_vertical, is_last, depth):
    children = child_map.get(node.proc.pid, [])
    if depth == 0:
        name_prefix = ""
        thread_indent = "┣─ "
    else:
        connector = "┗━ " if is_last else "┣━ "
        name_prefix = "".join(ancestor_vertical) + connector
        # Threads sit between this node's row and its first child; the vertical at this depth
        # must continue iff this node is not the last sibling at its depth.
        thread_indent = "".join(ancestor_vertical) + ("   " if is_last else "┃  ") + "┣─ "

    yield (node, name_prefix, thread_indent)

    # Descend: each child inherits ancestor_vertical extended by this node's continuation char,
    # except at depth 0 (the root has no rendered vertical to extend).
    if depth == 0:
        child_vertical = []
    else:
        child_vertical = ancestor_vertical + (["   "] if is_last else ["┃  "])
    for i, child in enumerate(children):
        child_is_last = (i == len(children) - 1)
        yield from _walk_subtree(child, child_map, child_vertical, child_is_last, depth + 1)

def discover_children(root_pids, proc_states, all_proc_states):
    # Find descendants of each root PID not yet tracked, at any depth. Needed when
    # the -p target uses a supervisor/worker model (e.g. stress-ng, gunicorn, make):
    # without recursion the supervisor shows 0% CPU and the busy workers are missed.
    # Each descendant records its actual immediate parent (child.ppid()) so the tree
    # renders with real depth instead of all descendants attributed flat under the root.
    # Descendants we can't read (psutil.AccessDenied -- typically when the child runs as a different
    # uid without CAP_SYS_PTRACE, e.g. /proc/<pid>/io is 0600 root) are recorded once and skipped on
    # future iterations to avoid log spam.
    known_pids = {ps.proc.pid for ps in proc_states}
    for root_pid in root_pids:
        try:
            for child in psutil.Process(root_pid).children(recursive=True):
                if child.pid in known_pids or child.pid in _denied_child_pids:
                    continue
                try:
                    actual_ppid = child.ppid()
                    new_ps = initialize_process_state(child.pid, is_child=True, parent_pid=actual_ppid)
                    proc_states.append(new_ps)
                    all_proc_states.append(new_ps)
                    known_pids.add(child.pid)
                except psutil.NoSuchProcess:
                    pass
                except psutil.AccessDenied:
                    _denied_child_pids.add(child.pid)
                    print(f"[RES-MON] Child PID {child.pid} not readable (permission denied); skipping.",
                          file=sys.stderr)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            print(f"[RES-MON] Cannot enumerate children of PID {root_pid} (permission denied).",
                  file=sys.stderr)

def is_root():
    # Windows has no geteuid/sudo model here; treat it as "no escalation available/needed".
    geteuid = getattr(os, "geteuid", None)
    return geteuid is None or geteuid() == 0

def require_sudo():
    # On POSIX, always run the monitor under sudo/root so per-process /proc reads and
    # cross-uid descendant discovery behave consistently. If sudo is unavailable or the
    # user cannot authenticate, exit with a concrete explanation instead of running with
    # partial/misleading data.
    if os.name == "nt" or is_root():
        return
    sudo = shutil.which("sudo")
    if not sudo:
        print("[RES-MON] ERROR: this monitor requires sudo/root on POSIX, but 'sudo' is not installed.", file=sys.stderr)
        print("[RES-MON]   Elevated access may be necessary because /proc/<pid>/io, /proc/<pid>/stack,", file=sys.stderr)
        print("[RES-MON]   descendant discovery, and sampling of processes owned by other users can be blocked", file=sys.stderr)
        print("[RES-MON]   by the kernel without CAP_SYS_PTRACE or matching uid.", file=sys.stderr)
        sys.exit(1)
    if os.getenv("_RES_MON_SUDO_ATTEMPTED") == "1":
        print("[RES-MON] ERROR: sudo/root access is required, but sudo did not start the monitor successfully.", file=sys.stderr)
        print("[RES-MON]   Elevated access may be necessary because /proc/<pid>/io, /proc/<pid>/stack,", file=sys.stderr)
        print("[RES-MON]   descendant discovery, and sampling of processes owned by other users can be blocked", file=sys.stderr)
        print("[RES-MON]   by the kernel without CAP_SYS_PTRACE or matching uid.", file=sys.stderr)
        sys.exit(1)

    env = os.environ.copy()
    env["_RES_MON_SUDO_ATTEMPTED"] = "1"
    argv = [sudo]
    if not sys.stdin.isatty():
        argv.append("-n")
    argv += ["-E", sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]]
    print("[RES-MON] Re-executing under sudo/root.", file=sys.stderr)
    os.execvpe(sudo, argv, env)

def main():
    system, pids, display, threads, children, logfile_timestamp, detail_only, summary_only, interval_sec, interactive, stack, top, json_output, log, columns = parse_args()

    require_sudo()

    config = validate_and_prepare(system, pids, display, threads, children, logfile_timestamp,
                                  detail_only=detail_only, summary_only=summary_only,
                                  interval_sec=interval_sec, interactive=interactive, stack=stack,
                                  top=top, json_output=json_output, log=log, columns=columns)
    if config.log:
        print(f"Monitor log:   {config.log_file}", file=sys.stderr)

    saved_term = setup_interactive_terminal() if config.interactive else None

    combined    = config.system and bool(config.pids)
    sys_state   = None
    proc_states = []
    # proc_states tracks only currently active processes; entries are removed when a process exits.
    # all_proc_states retains every state ever added (root + discovered children), including exited
    # ones, so the final summary can cover all monitored processes regardless of exit order.
    all_proc_states = []

    # When --log is off, route all `out.write` calls to /dev/null so emit() and the summary
    # block work unchanged; stdout (-D/-T) and JSON (-j) still produce their normal output.
    out_path = config.log_file if config.log else os.devnull
    with open(out_path, "w", encoding="utf-8") as out:
        if combined:
            sys_state   = initialize_monitor_state(config)
            proc_states = [initialize_process_state(pid) for pid in config.pids]
        elif config.system:
            sys_state = initialize_monitor_state(config)
        else:
            proc_states = [initialize_process_state(pid) for pid in config.pids]
        all_proc_states = list(proc_states)
        write_log_header(out, config)

        root_pids = list(config.pids)

        class _Stop(Exception):
            pass

        def signalHandler(sig, frame):
            # Raise an exception so any blocking call (sleep, psutil interval) unwinds immediately.
            # Setting a flag is insufficient: PEP 475 restarts interrupted syscalls in Python 3.
            raise _Stop()

        signal.signal(signal.SIGINT, signalHandler)
        signal.signal(signal.SIGTERM, signalHandler)

        try:
            # NOTE: row prefixes below use Unicode box-drawing chars (┣ ┗ ━ ─ ┃) to render the
            # process/thread tree. They render fine in any UTF-8 terminal and in the log file
            # (which is opened with encoding="utf-8"), but may corrupt or display as ? / mojibake
            # in non-UTF-8 environments: legacy Windows consoles, some CI log viewers, serial
            # consoles, downstream parsers that assume ASCII. Visual only: does not affect the
            # JSON output, which carries the parent_pid/is_child fields instead.
            while True:
                # pick up any new child processes spawned since last interval
                if config.children:
                    discover_children(root_pids, proc_states, all_proc_states)

                if combined:
                    # system collect blocks for the interval; processes sample instantly after
                    loop_start = time.monotonic()
                    sys_sample = collect_sample(config, sys_state)
                    dt = max(time.monotonic() - loop_start, 0.001)
                    top_frame_start(config)
                    sys_blank = " " * len(system_header_str(config))
                    lp = f"{sys_blank} | "  # line prefix for non-first rows in combined mode
                    # Map each tracked descendant to its actual immediate parent; child.ppid() was
                    # recorded at discovery time so child_map describes a real multi-level tree.
                    # When --descendants is off, an empty map keeps walk_process_tree from descending.
                    child_map = {}
                    if config.children:
                        for ps in proc_states:
                            if ps.is_child:
                                child_map.setdefault(ps.parent_pid, []).append(ps)
                    first = True
                    for root_ps in [ps for ps in list(proc_states) if not ps.is_child]:
                        for node_ps, name_prefix, thread_indent in walk_process_tree(root_ps, child_map):
                            try:
                                node_sample = collect_process_sample_instant(config, node_ps, dt)
                            except psutil.NoSuchProcess:
                                if node_ps in proc_states:
                                    proc_states.remove(node_ps)
                                continue
                            if first:
                                # The very first sample of the iteration shares its line with sys_sample.
                                # walk_process_tree always yields the root first, which has name_prefix="",
                                # matching write_combined_row's no-name-prefix layout.
                                write_combined_row(out, config, sys_sample, node_sample)
                                first = False
                            else:
                                emit(out, config, f"{lp}{process_row_str(config, node_sample, include_time=False, name_prefix=name_prefix)}\n")
                                emit_proc_stack_if_hot(out, config, node_sample, line_prefix=lp, name_prefix=name_prefix, include_time=False)
                            if config.threads:
                                node_has_children = bool(child_map.get(node_ps.proc.pid))
                                write_thread_rows(out, config, collect_thread_samples(config, node_ps, dt),
                                                  include_time=False, line_prefix=lp, name_prefix=thread_indent,
                                                  more_follows=node_has_children)
                    top_frame_flush(config)
                    if not any(not ps.is_child for ps in proc_states):
                        print("[RES-MON] All monitored processes exited, stopping.", file=sys.stderr)
                        break
                elif config.system:
                    # system-only: collect_sample handles the sleep internally
                    sample = collect_sample(config, sys_state)
                    top_frame_start(config)
                    write_sample_row(out, config, sample)
                    top_frame_flush(config)
                else:
                    # process-only: sleep once, then snapshot all PIDs with the same dt
                    loop_start = time.monotonic()
                    interruptible_sleep(config, config.interval_sec)
                    dt = max(time.monotonic() - loop_start, 0.001)
                    top_frame_start(config)
                    # Same multi-level tree walk as the combined branch, minus the sys_sample pairing.
                    child_map = {}
                    if config.children:
                        for ps in proc_states:
                            if ps.is_child:
                                child_map.setdefault(ps.parent_pid, []).append(ps)
                    for root_ps in [ps for ps in list(proc_states) if not ps.is_child]:
                        for node_ps, name_prefix, thread_indent in walk_process_tree(root_ps, child_map):
                            try:
                                node_sample = collect_process_sample_instant(config, node_ps, dt)
                            except psutil.NoSuchProcess:
                                if node_ps in proc_states:
                                    proc_states.remove(node_ps)
                                continue
                            write_process_sample_row(out, config, node_sample, name_prefix=name_prefix)
                            if config.threads:
                                node_has_children = bool(child_map.get(node_ps.proc.pid))
                                write_thread_rows(out, config, collect_thread_samples(config, node_ps, dt),
                                                  name_prefix=thread_indent, more_follows=node_has_children)
                    top_frame_flush(config)
                    if not any(not ps.is_child for ps in proc_states):
                        print("[RES-MON] All monitored processes exited, stopping.", file=sys.stderr)
                        break
        except _Stop:
            pass
        finally:
            restore_terminal(saved_term)
            if not config.detail_only:
                first = True
                if sys_state:
                    write_summary(out, config, sys_state, show_timing=True)
                    first = False
                for ps in all_proc_states:
                    write_process_summary(out, config, ps, show_timing=first)
                    first = False
            if config.log:
                print(f"Monitor log:   {config.log_file}", file=sys.stderr)
            if config.json:
                for path in write_json_outputs(config, sys_state, all_proc_states):
                    print(f"Monitor JSON:  {path}", file=sys.stderr)

if __name__ == "__main__":
    main()
