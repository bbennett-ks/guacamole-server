#!/usr/bin/env bash
#
# Spawn stress-ng workloads, one dedicated process per scenario. Each worker
# has a distinct signature (CPU%, RSS, READ_MB/s, ...) so any external monitor
# can be pointed at the printed PIDs to observe individual behaviors.
#
# Examples:
#   ./test-monitor-resources.sh cpu mem                                                         # CPU + memory, until Ctrl-C
#   ./test-monitor-resources.sh -d 60 all                                                       # all workloads, 60s
#   ./test-monitor-resources.sh cpu mem disk-read disk-write threads ctx ivctx fd faults net leak zombie iomix   # explicit "all"
#

set -u -o pipefail

DURATION=""
declare -a WORKLOADS=()
declare -a STRESS_PIDS=()
declare -a STRESS_LABELS=()

ALL_WORKLOADS=(cpu mem disk-read disk-write threads ctx ivctx fd faults net leak zombie iomix)

usage() {
    cat <<EOF
Usage: $(basename "$0") [options] <workload>... | all

Workloads (each spawns a dedicated stress-ng process):
  cpu          CPU-bound (matrixprod)            → high CPU%, USR%
  mem          Memory hog                         → high RSS, VMS, MEM%
  disk-read    Sequential readahead               → high read MB/s, IOWAIT
  disk-write   Sustained writes                   → high write MB/s
  threads      Many pthreads                      → high thread count
  ctx          Rapid voluntary context switches   → high VCTX/s
  ivctx        CPU oversubscription               → high IVCTX/s, LOAD1
  fd           File descriptor churn              → high FDs
  faults       Page faults                        → high MINFLT/s, MAJFLT/s
  net          Loopback socket I/O                → high net MB/s, SYS%
  leak         Alloc → hold 30s → free, repeat    → RSS sawtooth
  zombie       Zombie process churn               → STATE=zombie
  iomix        Mixed I/O                          → mixed read/write, IOWAIT
  combo        All stressors inside a single stress-ng process (mem+leak merged)
  all          All of the above as separate processes

Options:
  -d, --duration SEC    How long each workload runs (default: run until Ctrl-C)
  -h, --help

Equivalent command line for "all":
  $(basename "$0") ${ALL_WORKLOADS[*]}
EOF
}

log() { printf '[stress] %s\n' "$*" >&2; }

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    if ((${#STRESS_PIDS[@]})); then
        log "stopping stress-ng workers: ${STRESS_PIDS[*]}"
        kill -TERM "${STRESS_PIDS[@]}" 2>/dev/null || true
        sleep 1
        kill -KILL "${STRESS_PIDS[@]}" 2>/dev/null || true
    fi
    exit "$rc"
}

spawn() {
    local label=$1; shift
    local timeout_args=()
    [[ -n $DURATION ]] && timeout_args=(--timeout "${DURATION}s")
    stress-ng "${timeout_args[@]}" "$@" >/dev/null 2>&1 &
    local pid=$!
    STRESS_PIDS+=("$pid")
    STRESS_LABELS+=("$label")
    log "spawned $label pid=$pid → stress-ng ${timeout_args[*]} $*"
}

launch_workload() {
    # One worker per stressor where the metric can still be produced. Exceptions:
    #   ivctx  – needs > nproc runnable threads to force preemption (involuntary cs)
    #   ctx    – stress-ng --switch always pairs a parent+child per worker
    #   net    – stress-ng --sock always pairs a client+server per worker
    case $1 in
        cpu)         spawn cpu        --cpu 1 --cpu-method matrixprod ;;
        mem)         spawn mem        --vm 1 --vm-bytes 1G --vm-keep --vm-method all ;;
        disk-read)   spawn disk-read  --readahead 1 --readahead-bytes 256M ;;
        disk-write)  spawn disk-write --hdd 1 --hdd-bytes 512M ;;
        threads)     spawn threads    --pthread 1 --pthread-max 8 ;;
        ctx)         spawn ctx        --switch 1 ;;
        ivctx)       spawn ivctx      --yield "$(( $(nproc) + 1 ))" ;;
        fd)          spawn fd         --open 1 ;;
        faults)      spawn faults     --fault 1 ;;
        net)         spawn net        --sock 1 ;;
        leak)        spawn leak       --vm 1 --vm-bytes 256M --vm-hang 30 ;;
        zombie)      spawn zombie     --zombie 1 --zombie-max 4 ;;
        iomix)       spawn iomix      --iomix 1 --iomix-bytes 256M ;;
        combo)       spawn combo \
                        --cpu 1 --cpu-method matrixprod \
                        --vm 1 --vm-bytes 1G --vm-hang 30 \
                        --readahead 1 --readahead-bytes 256M \
                        --hdd 1 --hdd-bytes 512M \
                        --pthread 1 --pthread-max 8 \
                        --switch 1 \
                        --yield "$(( $(nproc) + 1 ))" \
                        --open 1 \
                        --fault 1 \
                        --sock 1 \
                        --zombie 1 --zombie-max 4 \
                        --iomix 1 --iomix-bytes 256M ;;
        *) log "unknown workload: $1"; exit 2 ;;
    esac
}

while (($#)); do
    case $1 in
        -d|--duration)  DURATION=$2; shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        all)            WORKLOADS+=("${ALL_WORKLOADS[@]}"); shift ;;
        -*)             log "unknown option: $1"; usage >&2; exit 2 ;;
        *)              WORKLOADS+=("$1"); shift ;;
    esac
done

if ((${#WORKLOADS[@]} == 0)); then
    usage >&2
    exit 2
fi

if ! command -v stress-ng >/dev/null; then
    log "stress-ng not found on PATH"; exit 1
fi

trap cleanup EXIT INT TERM

duration_desc=${DURATION:+${DURATION}s}
duration_desc=${duration_desc:-until-ctrl-c}
duration_opt=${DURATION:+-d $DURATION}

log "script pid=$$  duration=${duration_desc}  workloads=${WORKLOADS[*]}"
log "equivalent: $(basename "$0") ${duration_opt} ${WORKLOADS[*]}"

for w in "${WORKLOADS[@]}"; do
    launch_workload "$w"
done

log "running ${#STRESS_PIDS[@]} workloads (${duration_desc}); pids: ${STRESS_PIDS[*]}"
wait
