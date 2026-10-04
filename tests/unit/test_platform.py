from __future__ import annotations

import builtins
import ctypes
import subprocess
import sys
from types import SimpleNamespace

import pytest

import fabric_kg_builder.platform as host_platform
from fabric_kg_builder.platform import (
    PlatformMetadata,
    ProcessResourceUsage,
    ResourceMetricsError,
)


def test_platform_metadata_is_deterministically_normalized(monkeypatch) -> None:
    monkeypatch.setattr(
        host_platform.stdlib_platform, "system", lambda: " Windows "
    )
    monkeypatch.setattr(
        host_platform.stdlib_platform, "release", lambda: " 11  Pro "
    )
    monkeypatch.setattr(
        host_platform.stdlib_platform, "machine", lambda: " AMD64 "
    )

    assert host_platform.platform_metadata() == PlatformMetadata(
        system="windows",
        release="11 pro",
        machine="amd64",
    )


@pytest.mark.parametrize(
    ("system", "raw_peak_rss", "expected_peak_rss"),
    [
        ("darwin", 4096, 4096),
        ("linux", 4096, 4096 * 1024),
    ],
)
def test_unix_peak_rss_units(
    monkeypatch,
    system: str,
    raw_peak_rss: int,
    expected_peak_rss: int,
) -> None:
    fake_resource = SimpleNamespace(
        RUSAGE_SELF=0,
        getrusage=lambda _: SimpleNamespace(
            ru_maxrss=raw_peak_rss,
            ru_utime=1.25,
            ru_stime=0.75,
        ),
    )
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setattr(
        host_platform,
        "_linux_current_rss",
        lambda: 1024,
    )
    monkeypatch.setattr(
        host_platform,
        "_darwin_current_rss",
        lambda: 1024,
    )

    usage = host_platform._unix_process_resource_usage(system)

    assert usage.cpu_seconds == 2.0
    assert usage.current_rss_bytes == 1024
    assert usage.peak_rss_bytes == expected_peak_rss


def test_windows_resource_usage_dispatch_produces_valid_values(
    monkeypatch,
) -> None:
    expected = ProcessResourceUsage(
        cpu_seconds=2.5,
        current_rss_bytes=4_000_000,
        peak_rss_bytes=5_000_000,
    )
    monkeypatch.setattr(
        host_platform,
        "platform_metadata",
        lambda: PlatformMetadata("windows", "11", "amd64"),
    )
    monkeypatch.setattr(
        host_platform, "_windows_process_resource_usage", lambda: expected
    )

    assert host_platform.process_resource_usage() == expected


def test_windows_native_metrics_api(monkeypatch) -> None:
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    def set_filetime(pointer, ticks: int) -> None:
        value = ctypes.cast(pointer, ctypes.POINTER(FileTime)).contents
        value.dwLowDateTime = ticks & 0xFFFFFFFF
        value.dwHighDateTime = ticks >> 32

    def get_process_times(_process, _creation, _exit, kernel, user):
        set_filetime(kernel, 15_000_000)
        set_filetime(user, 25_000_000)
        return 1

    def get_process_memory_info(_process, pointer, _size):
        counters = ctypes.cast(
            pointer, ctypes.POINTER(ProcessMemoryCounters)
        ).contents
        counters.WorkingSetSize = 4_000_000
        counters.PeakWorkingSetSize = 5_000_000
        return 1

    kernel32 = SimpleNamespace(
        GetCurrentProcess=FakeFunction(lambda: 123),
        GetProcessTimes=FakeFunction(get_process_times),
    )
    psapi = SimpleNamespace(
        GetProcessMemoryInfo=FakeFunction(get_process_memory_info)
    )
    libraries = {"kernel32": kernel32, "psapi": psapi}
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **_: libraries[name],
        raising=False,
    )

    usage = host_platform._windows_process_resource_usage()

    assert usage == ProcessResourceUsage(
        cpu_seconds=4.0,
        current_rss_bytes=4_000_000,
        peak_rss_bytes=5_000_000,
    )


def test_missing_resource_module_is_an_explicit_collection_error(
    monkeypatch,
) -> None:
    original_import = builtins.__import__

    def import_without_resource(name, *args, **kwargs):
        if name == "resource":
            raise ImportError("simulated unavailable resource module")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_resource)

    with pytest.raises(
        ResourceMetricsError,
        match="resource module is required on linux",
    ):
        host_platform._unix_process_resource_usage("linux")


def test_unsupported_platform_is_an_explicit_collection_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        host_platform,
        "platform_metadata",
        lambda: PlatformMetadata("plan9", "1", "amd64"),
    )

    with pytest.raises(ResourceMetricsError, match="unsupported"):
        host_platform.process_resource_usage()


def test_cli_and_stage_import_without_resource_module() -> None:
    script = """
import builtins
original_import = builtins.__import__
def import_without_resource(name, *args, **kwargs):
    if name == "resource":
        raise ImportError("simulated unavailable resource module")
    return original_import(name, *args, **kwargs)
builtins.__import__ = import_without_resource
import fabric_kg_builder.cli.main
import fabric_kg_builder.domain.stage
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
