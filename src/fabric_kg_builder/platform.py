"""Cross-platform host metadata and process resource measurements."""

from __future__ import annotations

import ctypes
import os
import platform as stdlib_platform
from dataclasses import dataclass
from pathlib import Path


class ResourceMetricsError(RuntimeError):
    """Raised when process resource metrics cannot be collected accurately."""


@dataclass(frozen=True)
class PlatformMetadata:
    system: str
    release: str
    machine: str


@dataclass(frozen=True)
class ProcessResourceUsage:
    cpu_seconds: float
    current_rss_bytes: int
    peak_rss_bytes: int


def platform_metadata() -> PlatformMetadata:
    """Return deterministic, normalized host metadata."""
    return PlatformMetadata(
        system=_normalized_platform_value(stdlib_platform.system(), "system"),
        release=_normalized_platform_value(stdlib_platform.release(), "release"),
        machine=_normalized_platform_value(stdlib_platform.machine(), "machine"),
    )


def process_resource_usage() -> ProcessResourceUsage:
    """Collect process CPU time plus current and peak resident memory."""
    system = platform_metadata().system
    if system == "windows":
        return _windows_process_resource_usage()
    if system in {"darwin", "linux"}:
        return _unix_process_resource_usage(system)
    raise ResourceMetricsError(
        f"process resource metrics are unsupported on platform {system!r}"
    )


def _normalized_platform_value(value: str, field_name: str) -> str:
    normalized = " ".join(value.split()).casefold()
    if not normalized:
        raise ResourceMetricsError(f"platform {field_name} is unavailable")
    return normalized


def _unix_process_resource_usage(system: str) -> ProcessResourceUsage:
    try:
        import resource
    except ImportError as exc:
        raise ResourceMetricsError(
            f"the resource module is required on {system}"
        ) from exc

    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak_rss = int(usage.ru_maxrss)
    if system != "darwin":
        peak_rss *= 1024
    if system == "linux":
        current_rss = _linux_current_rss()
    else:
        current_rss = _darwin_current_rss()
    return ProcessResourceUsage(
        cpu_seconds=max(0.0, float(usage.ru_utime) + float(usage.ru_stime)),
        current_rss_bytes=current_rss,
        peak_rss_bytes=max(current_rss, peak_rss),
    )


def _linux_current_rss() -> int:
    try:
        fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
        resident_pages = int(fields[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError) as exc:
        raise ResourceMetricsError(
            "failed to read current RSS from /proc/self/statm"
        ) from exc
    return resident_pages * page_size


def _darwin_current_rss() -> int:
    class ProcTaskInfo(ctypes.Structure):
        _fields_ = [
            ("pti_virtual_size", ctypes.c_uint64),
            ("pti_resident_size", ctypes.c_uint64),
            ("pti_total_user", ctypes.c_uint64),
            ("pti_total_system", ctypes.c_uint64),
            ("pti_threads_user", ctypes.c_uint64),
            ("pti_threads_system", ctypes.c_uint64),
            ("pti_policy", ctypes.c_int32),
            ("pti_faults", ctypes.c_int32),
            ("pti_pageins", ctypes.c_int32),
            ("pti_cow_faults", ctypes.c_int32),
            ("pti_messages_sent", ctypes.c_int32),
            ("pti_messages_received", ctypes.c_int32),
            ("pti_syscalls_mach", ctypes.c_int32),
            ("pti_syscalls_unix", ctypes.c_int32),
            ("pti_csw", ctypes.c_int32),
            ("pti_threadnum", ctypes.c_int32),
            ("pti_numrunning", ctypes.c_int32),
            ("pti_priority", ctypes.c_int32),
        ]

    try:
        proc_pidinfo = ctypes.CDLL(
            "/usr/lib/libproc.dylib", use_errno=True
        ).proc_pidinfo
    except OSError as exc:
        raise ResourceMetricsError("macOS libproc is unavailable") from exc
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    info = ProcTaskInfo()
    result = proc_pidinfo(
        os.getpid(),
        4,  # PROC_PIDTASKINFO
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if result != ctypes.sizeof(info):
        errno = ctypes.get_errno()
        raise ResourceMetricsError(
            f"proc_pidinfo failed to collect current RSS (errno={errno})"
        )
    return int(info.pti_resident_size)


def _windows_process_resource_usage() -> ProcessResourceUsage:
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    size_t = ctypes.c_size_t

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", size_t),
            ("WorkingSetSize", size_t),
            ("QuotaPeakPagedPoolUsage", size_t),
            ("QuotaPagedPoolUsage", size_t),
            ("QuotaPeakNonPagedPoolUsage", size_t),
            ("QuotaNonPagedPoolUsage", size_t),
            ("PagefileUsage", size_t),
            ("PeakPagefileUsage", size_t),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise ResourceMetricsError("Windows process metrics APIs are unavailable") from exc

    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    get_process_times.restype = wintypes.BOOL
    get_process_memory_info = psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL

    process = get_current_process()
    creation = FileTime()
    exit_time = FileTime()
    kernel = FileTime()
    user = FileTime()
    if not get_process_times(
        process,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise ResourceMetricsError(
            f"GetProcessTimes failed (error={ctypes.get_last_error()})"
        )

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not get_process_memory_info(
        process, ctypes.byref(counters), counters.cb
    ):
        raise ResourceMetricsError(
            f"GetProcessMemoryInfo failed (error={ctypes.get_last_error()})"
        )

    def filetime_seconds(value: FileTime) -> float:
        ticks = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
        return ticks / 10_000_000

    return ProcessResourceUsage(
        cpu_seconds=filetime_seconds(kernel) + filetime_seconds(user),
        current_rss_bytes=int(counters.WorkingSetSize),
        peak_rss_bytes=int(counters.PeakWorkingSetSize),
    )
