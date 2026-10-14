"""Terminate only a call's own process tree when its job handle is closed."""
import ctypes
from ctypes import wintypes
import os


class Job:
    def __init__(self):
        self.handle=None
        if os.name!='nt': return
        size=ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_=[('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),
                ('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',size),('MaximumWorkingSetSize',size),
                ('ActiveProcessLimit',wintypes.DWORD),('Affinity',size),('PriorityClass',wintypes.DWORD),
                ('SchedulingClass',wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount',
                'ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_=[('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',size),
                ('JobMemoryLimit',size),('PeakProcessMemoryUsed',size),('PeakJobMemoryUsed',size)]
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype=wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.api.SetInformationJobObject.restype=wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype=wintypes.BOOL
        self.api.CloseHandle.argtypes=[wintypes.HANDLE]; self.api.CloseHandle.restype=wintypes.BOOL
        handle=self.api.CreateJobObjectW(None,None)
        if not handle: raise ctypes.WinError(ctypes.get_last_error())
        info=Extended(); info.BasicLimitInformation.LimitFlags=0x2000 # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            self.api.CloseHandle(handle); raise ctypes.WinError(ctypes.get_last_error())
        self.handle=handle

    def assign(self,process):
        if self.handle and not self.api.AssignProcessToJobObject(self.handle,int(process._handle)):
            if process.poll() is not None: return
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle); self.handle=None
