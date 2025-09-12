#!/usr/bin/env python3
"""Elevated trim helper for Ram Gobbler.

Listens on a named pipe and trims processes on request. This script is
expected to run with Administrator privileges. The GUI connects via the
"\\.\\pipe\\RamGobblerHelper" pipe and sends JSON messages of the form::

    {"action": "trim", "pid": 1234, "strategy": "standard"}

The helper responds with::

    {"ok": bool, "delta_mb": float}

Only the ``trim`` action with ``standard`` or ``reset`` strategies is accepted.
"""

import json
import os
import psutil
import ctypes
from ctypes import wintypes
import win32file  # type: ignore
import win32pipe  # type: ignore
import win32con  # type: ignore
import win32security  # type: ignore

# reuse trimming helpers ------------------------------------------------------
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100
ACCESS = PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA
EmptyWorkingSet = ctypes.windll.psapi.EmptyWorkingSet
SetProcessWorkingSetSizeEx = ctypes.windll.kernel32.SetProcessWorkingSetSizeEx
QUOTA_FLAGS = 0x0001 | 0x0002
OpenProcess = ctypes.windll.kernel32.OpenProcess
CloseHandle = ctypes.windll.kernel32.CloseHandle

def empty_working_set(pid: int) -> bool:
    h = OpenProcess(ACCESS, False, pid)
    if not h:
        return False
    try:
        return bool(EmptyWorkingSet(h))
    finally:
        CloseHandle(h)

def reset_working_set(pid: int) -> bool:
    h = OpenProcess(ACCESS, False, pid)
    if not h:
        return False
    try:
        return bool(SetProcessWorkingSetSizeEx(h, ctypes.c_size_t(-1 & 0xFFFFFFFFFFFFFFFF), ctypes.c_size_t(-1 & 0xFFFFFFFFFFFFFFFF), QUOTA_FLAGS))
    finally:
        CloseHandle(h)

def rss_mb(pid: int) -> float:
    try:
        return psutil.Process(pid).memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0

def is_system_process(p: psutil.Process) -> bool:
    try:
        name = p.name()
        return name.lower() in {"system", "registry"}
    except Exception:
        return True

PIPE_NAME = r"\\.\pipe\RamGobblerHelper"


def main():
    # Allow local users to connect; deny remote/exec by design
    sa = win32security.SECURITY_ATTRIBUTES()
    sd = win32security.SECURITY_DESCRIPTOR()
    sd.Initialize()
    sid = win32security.CreateWellKnownSid(win32security.WinLocalSid, None)
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32con.FILE_GENERIC_READ | win32con.FILE_GENERIC_WRITE, sid)
    sd.SetSecurityDescriptorDacl(True, acl, False)
    sa.SECURITY_DESCRIPTOR = sd

    pipe = win32pipe.CreateNamedPipe(
        PIPE_NAME,
        win32con.PIPE_ACCESS_DUPLEX,
        win32con.PIPE_TYPE_MESSAGE | win32con.PIPE_READMODE_MESSAGE | win32con.PIPE_WAIT,
        1,
        65536,
        65536,
        0,
        sa,
    )

    while True:
        win32pipe.ConnectNamedPipe(pipe, None)
        try:
            raw = win32file.ReadFile(pipe, 65536)[1]
            req = json.loads(raw.decode("utf-8"))
            res = {"ok": False}
            if req.get("action") == "trim" and req.get("strategy") in ("standard", "reset"):
                pid = int(req.get("pid", 0))
                if pid:
                    try:
                        proc = psutil.Process(pid)
                        if not is_system_process(proc):
                            before = rss_mb(pid)
                            ok = False
                            if req["strategy"] == "reset":
                                ok = reset_working_set(pid) or empty_working_set(pid)
                            else:
                                ok = empty_working_set(pid)
                            after = rss_mb(pid)
                            res = {"ok": ok, "delta_mb": max(0.0, before - after) if ok else 0.0}
                    except Exception:
                        pass
            win32file.WriteFile(pipe, json.dumps(res).encode("utf-8"))
        except Exception:
            try:
                win32file.WriteFile(pipe, json.dumps({"ok": False}).encode("utf-8"))
            except Exception:
                pass
        finally:
            win32pipe.DisconnectNamedPipe(pipe)

if __name__ == "__main__":
    main()
