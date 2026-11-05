"""Windows process ownership for the test-only installed-CLI supervisor.

The private job receives a suspended launcher before any child can execute.
Its descendants inherit membership. Durable registry PIDs are only compared
with membership; they never authorize a process signal or termination.
"""

from __future__ import annotations

import ctypes
import importlib
import math
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, BinaryIO


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _function(library: Any, name: str, arguments: list[object], result: object) -> None:
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result


def _api() -> Any:
    # WinDLL/get_last_error exist only on Windows. Keep Linux imports usable for
    # the shared fixture and strict static checks; constructing this class fails
    # before native API access on other systems.
    native: Any = ctypes
    library = native.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    handle, dword, boolean = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    _function(library, "CreateJobObjectW", [pointer, wintypes.LPCWSTR], handle)
    _function(
        library,
        "SetInformationJobObject",
        [handle, ctypes.c_int, pointer, dword],
        boolean,
    )
    _function(library, "AssignProcessToJobObject", [handle, handle], boolean)
    _function(library, "TerminateJobObject", [handle, wintypes.UINT], boolean)
    _function(
        library,
        "QueryInformationJobObject",
        [handle, ctypes.c_int, pointer, dword, pointer],
        boolean,
    )
    _function(
        library, "IsProcessInJob", [handle, handle, ctypes.POINTER(boolean)], boolean
    )
    _function(library, "OpenProcess", [dword, boolean, dword], handle)
    _function(library, "WaitForSingleObject", [handle, dword], dword)
    _function(library, "GetCurrentProcess", [], handle)
    _function(
        library,
        "DuplicateHandle",
        [handle, handle, handle, ctypes.POINTER(handle), dword, boolean, dword],
        boolean,
    )
    _function(library, "CloseHandle", [handle], boolean)
    _function(
        library,
        "InitializeProcThreadAttributeList",
        [pointer, dword, dword, ctypes.POINTER(ctypes.c_size_t)],
        boolean,
    )
    _function(
        library,
        "UpdateProcThreadAttribute",
        [pointer, dword, ctypes.c_size_t, pointer, ctypes.c_size_t, pointer, pointer],
        boolean,
    )
    _function(library, "DeleteProcThreadAttributeList", [pointer], None)
    _function(
        library,
        "CreateProcessW",
        [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            pointer,
            pointer,
            boolean,
            dword,
            pointer,
            wintypes.LPCWSTR,
            pointer,
            ctypes.POINTER(_ProcessInfo),
        ],
        boolean,
    )
    _function(library, "ResumeThread", [handle], dword)
    _function(library, "TerminateProcess", [handle, wintypes.UINT], boolean)
    return library


def _error(message: str) -> OSError:
    native: Any = ctypes
    return OSError(int(native.get_last_error()), message)


class WindowsProcess:
    """Retained native ownership of only this fixture's installed CLI tree."""

    _api: Any
    _job: int | None
    _process: int | None
    _thread: int | None
    _owner: int | None
    pid: int

    def __init__(
        self,
        command: list[str],
        cwd: Path,
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> None:
        if sys.platform != "win32":
            raise OSError("Windows process supervision requires Windows")
        if not command:
            raise ValueError("a process command is required")
        self._api = _api()
        self._job: int | None = None
        self._process: int | None = None
        self._thread: int | None = None
        self._owner: int | None = None
        self.pid = 0
        try:
            self._job = self._api.CreateJobObjectW(None, None) or None
            if not self._job:
                raise _error("could not create private fixture job")
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self._api.SetInformationJobObject(
                self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise _error("could not configure private fixture job")
            self._launch(command, cwd, stdout, stderr)
            if not self._api.AssignProcessToJobObject(self._job, self._process):
                raise _error("could not bind suspended fixture launcher to private job")
            if self._api.ResumeThread(self._thread) == 0xFFFFFFFF:
                raise _error("could not resume owned fixture launcher")
        except BaseException:
            # A launcher rejected by AssignProcessToJobObject is still suspended
            # and has no descendants. Its retained handle is safe to terminate.
            if self._process is not None:
                self._api.TerminateProcess(self._process, 125)
                self._api.WaitForSingleObject(self._process, 10000)
            self.close()
            raise

    def _launch(
        self, command: list[str], cwd: Path, stdout: BinaryIO, stderr: BinaryIO
    ) -> None:
        native_crt: Any = importlib.import_module("msvcrt")

        duplicated: list[int] = []
        attributes_initialized = False
        information = _ProcessInfo()
        current = self._api.GetCurrentProcess()
        size = ctypes.c_size_t()
        self._api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise _error("could not size fixture process attributes")
        attributes = ctypes.create_string_buffer(size.value)
        try:
            if not self._api.InitializeProcThreadAttributeList(
                attributes, 1, 0, ctypes.byref(size)
            ):
                raise _error("could not initialize fixture process attributes")
            attributes_initialized = True
            with open(os.devnull, "rb") as standard_input:
                for stream in (standard_input, stdout, stderr):
                    copied = wintypes.HANDLE()
                    if not self._api.DuplicateHandle(
                        current,
                        native_crt.get_osfhandle(stream.fileno()),
                        current,
                        ctypes.byref(copied),
                        0,
                        True,
                        2,
                    ):
                        raise _error("could not prepare fixture standard handles")
                    if copied.value is None:
                        raise RuntimeError("fixture standard handle is unavailable")
                    duplicated.append(copied.value)
            handles = (wintypes.HANDLE * len(duplicated))(*duplicated)
            if not self._api.UpdateProcThreadAttribute(
                attributes, 0, 0x20002, handles, ctypes.sizeof(handles), None, None
            ):
                raise _error("could not restrict fixture inherited handles")
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = duplicated[0]
            startup.StartupInfo.hStdOutput = duplicated[1]
            startup.StartupInfo.hStdError = duplicated[2]
            startup.lpAttributeList = ctypes.addressof(attributes)
            arguments = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            # Suspended launch closes the assignment race: even a venv console
            # launcher cannot start its interpreter until it belongs to our job.
            flags = 0x4 | 0x200 | 0x80000
            if not self._api.CreateProcessW(
                command[0],
                arguments,
                None,
                None,
                True,
                flags,
                None,
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(information),
            ):
                raise _error("could not create suspended fixture launcher")
            self._process = information.hProcess
            self._thread = information.hThread
            self.pid = information.dwProcessId
        finally:
            # Register returned native handles even if Python interruption lands
            # immediately after CreateProcessW returns. Constructor cleanup must
            # still own the suspended child before releasing any resources.
            if information.hProcess:
                self._process = information.hProcess
                self._thread = information.hThread
                self.pid = information.dwProcessId
            if attributes_initialized:
                self._api.DeleteProcThreadAttributeList(attributes)
            for handle in duplicated:
                self._api.CloseHandle(handle)

    def _handle_exited(self, handle: int | None) -> bool:
        if handle is None:
            return True
        result = self._api.WaitForSingleObject(handle, 0)
        if result not in {0, 258}:
            raise _error("could not inspect owned fixture process")
        return result == 0

    @property
    def exited(self) -> bool:
        return self._handle_exited(self._process)

    def owns(self, pid: int) -> bool:
        """Retain a live verified member; numeric IDs never authorize signals."""
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or self._job is None:
            return False
        process: int | None = self._api.OpenProcess(0x1000 | 0x100000, False, pid)
        if not process:
            return False
        retained = False
        try:
            member = wintypes.BOOL()
            if not self._api.IsProcessInJob(process, self._job, ctypes.byref(member)):
                return False
            if not member.value or self._handle_exited(process):
                return False
            previous = self._owner
            self._owner = process
            retained = True
            if previous is not None:
                self._api.CloseHandle(previous)
            return True
        finally:
            if not retained:
                self._api.CloseHandle(process)

    def terminate(self, timeout: float = 10) -> None:
        """Terminate only the private job and wait for every owned process."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("termination timeout must be positive and finite")
        if self._job is None:
            return
        deadline = time.monotonic() + timeout
        if not self._api.TerminateJobObject(self._job, 125):
            raise _error("could not terminate private fixture job")
        while True:
            accounting = _Accounting()
            if not self._api.QueryInformationJobObject(
                self._job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            ):
                raise _error("could not inspect private fixture job completion")
            if (
                accounting.ActiveProcesses == 0
                and self.exited
                and self._handle_exited(self._owner)
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("private fixture processes did not exit")
            time.sleep(min(0.01, remaining))

    def close(self) -> None:
        """Close native ownership, cleaning only this job's processes."""
        try:
            if self._job is not None:
                self.terminate()
        finally:
            for name in ("_owner", "_thread", "_process", "_job"):
                handle = getattr(self, name)
                if handle is not None:
                    self._api.CloseHandle(handle)
                    setattr(self, name, None)
