"""Small Windows taskbar progress bridge with a no-op non-Windows fallback."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class TaskbarProgress:
    TBPF_NOPROGRESS = 0x0
    TBPF_NORMAL = 0x2
    TBPF_ERROR = 0x4
    TBPF_PAUSED = 0x8

    def __init__(self, widget=None):
        self._object = None
        self._hwnd = 0
        self._widget = widget
        self._com_initialized = False
        if os.name != "nt" or widget is None:
            return
        try:
            # Qt may not have created the native window before the first
            # paint/show event.  Calling winId() here usually creates it,
            # but keep the widget around and refresh the handle lazily too.
            self._hwnd = int(widget.winId() or 0)
            self._create()
        except (OSError, TypeError, ValueError, AttributeError):
            self.close()

    def _create(self):
        ole32 = ctypes.windll.ole32
        # A Qt GUI thread is not guaranteed to have initialized COM.  Without
        # this call CoCreateInstance commonly returns CO_E_NOTINITIALIZED and
        # the taskbar bridge silently becomes a no-op.
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        com_result = int(ole32.CoInitializeEx(None, 0x2))
        if com_result in (0, 1):
            self._com_initialized = True
        elif com_result not in (0x80010106, -2147417850):  # RPC_E_CHANGED_MODE
            return
        ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]
        ole32.CLSIDFromString.restype = ctypes.c_long
        ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(_GUID),
                                           ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = ctypes.c_long
        clsid, iid = _GUID(), _GUID()
        if ole32.CLSIDFromString("{56FDF344-FD6D-11D0-958A-006097C9A090}", ctypes.byref(clsid)) < 0:
            return
        if ole32.CLSIDFromString("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EE7D4}", ctypes.byref(iid)) < 0:
            return
        instance = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(instance)) < 0:
            return
        self._object = instance
        vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        init = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[3])
        if init(instance) < 0:
            self.close()

    def _call(self, index, signature, *args):
        if self._object is None:
            return
        if not self._hwnd and self._widget is not None:
            try:
                self._hwnd = int(self._widget.winId() or 0)
            except (OSError, TypeError, ValueError, AttributeError):
                self._hwnd = 0
        if not self._hwnd:
            return
        vtable = ctypes.cast(self._object, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        signature(vtable[index])(self._object, *args)

    def set_value(self, value: int, total: int = 100):
        if self._object is None:
            return
        value = max(0, int(value)); total = max(1, int(total))
        state = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, wintypes.DWORD)
        set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND,
                                        ctypes.c_ulonglong, ctypes.c_ulonglong)
        self._call(10, state, self._hwnd, self.TBPF_NORMAL)
        self._call(9, set_value, self._hwnd, value, total)

    def set_paused(self):
        state = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, wintypes.DWORD)
        self._call(10, state, self._hwnd, self.TBPF_PAUSED)

    def clear(self):
        state = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, wintypes.DWORD)
        self._call(10, state, self._hwnd, self.TBPF_NOPROGRESS)

    def close(self):
        if self._object is not None:
            try:
                vtable = ctypes.cast(self._object, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
                release(self._object)
            except (OSError, TypeError, ValueError):
                pass
        self._object = None
        if self._com_initialized and os.name == "nt":
            try:
                ctypes.windll.ole32.CoUninitialize()
            except (OSError, AttributeError):
                pass
            self._com_initialized = False

    def __del__(self):
        self.close()
