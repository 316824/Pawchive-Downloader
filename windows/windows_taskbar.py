"""Small Windows taskbar progress bridge with a no-op non-Windows fallback."""
from __future__ import annotations

import ctypes
import os
import weakref
from ctypes import wintypes


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class TaskbarProgress:
    TBPF_NOPROGRESS = 0x0
    TBPF_NORMAL = 0x2
    TBPF_ERROR = 0x4
    TBPF_PAUSED = 0x8
    CLSID_TASKBAR_LIST = "{56FDF344-FD6D-11D0-958A-006097C9A090}"
    # Windows SDK ShObjIdl_core.h, ITaskbarList3. A different IID silently
    # returns E_NOINTERFACE even though initializing COM itself succeeded.
    IID_TASKBAR_LIST3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"

    def __init__(self, widget=None):
        self._object = None
        self._hwnd = 0
        self._widget = widget
        self._com_initialized = False
        self._closed = False
        self._state = self.TBPF_NOPROGRESS
        self._value, self._total = 0, 100
        self._event_filter = None
        self._reapply_timer = None
        self._application = None
        self.last_error = None
        if os.name != "nt" or widget is None:
            return
        try:
            # Qt may not have created the native window before the first
            # paint/show event.  Calling winId() here usually creates it,
            # but keep the widget around and refresh the handle lazily too.
            self._hwnd = int(widget.winId() or 0)
            self._create()
            self._install_shell_notifications()
        except (OSError, TypeError, ValueError, AttributeError):
            self.close()

    def _create(self):
        ole32 = ctypes.windll.ole32
        # A Qt GUI thread is not guaranteed to have initialized COM.  Without
        # this call CoCreateInstance commonly returns CO_E_NOTINITIALIZED and
        # the taskbar bridge silently becomes a no-op.
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        if not self._com_initialized:
            com_result = int(ole32.CoInitializeEx(None, 0x2))
            if com_result in (0, 1):
                self._com_initialized = True
            elif com_result not in (0x80010106, -2147417850):  # RPC_E_CHANGED_MODE
                self.last_error = com_result
                return False
        ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]
        ole32.CLSIDFromString.restype = ctypes.c_long
        ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(_GUID),
                                           ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = ctypes.c_long
        clsid, iid = _GUID(), _GUID()
        for value, target in ((self.CLSID_TASKBAR_LIST, clsid), (self.IID_TASKBAR_LIST3, iid)):
            result = int(ole32.CLSIDFromString(value, ctypes.byref(target)))
            if result < 0:
                self.last_error = result
                self._release_native()
                return False
        instance = ctypes.c_void_p()
        result = int(ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(instance)))
        if result < 0:
            self.last_error = result
            self._release_native()
            return False
        self._object = instance
        vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        init = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[3])
        result = int(init(instance))
        if result < 0:
            self.last_error = result
            self._release_native()
            return False
        self.last_error = None
        return True

    def _install_shell_notifications(self):
        # Explorer can create its taskbar button after the window's showEvent,
        # or recreate it after restarting. Reapply the cached progress when
        # Windows says the button is ready, without changing the main window.
        from PySide6.QtCore import QAbstractNativeEventFilter, QTimer
        from PySide6.QtWidgets import QApplication
        application = QApplication.instance()
        if application is None or application.platformName() != 'windows':
            return
        user32 = ctypes.windll.user32
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        message_id = int(user32.RegisterWindowMessageW('TaskbarButtonCreated'))
        if not message_id:
            return
        owner = weakref.ref(self)

        class ShellNotificationFilter(QAbstractNativeEventFilter):
            def nativeEventFilter(self, event_type, message):
                bridge = owner()
                if bridge is not None and not bridge._closed and event_type in (
                        b'windows_generic_MSG', b'windows_dispatcher_MSG'):
                    event = wintypes.MSG.from_address(int(message))
                    if event.message == message_id and int(event.hWnd or 0) == bridge._hwnd:
                        bridge._reapply_timer.start(0)
                return False, 0

        self._reapply_timer = QTimer(application)
        self._reapply_timer.setSingleShot(True)
        self._reapply_timer.timeout.connect(self._shell_button_created)
        self._event_filter = ShellNotificationFilter()
        self._application = application
        application.installNativeEventFilter(self._event_filter)

    def _shell_button_created(self):
        if self._closed:
            return
        self._release_native()
        self.ensure()
        self._apply()

    def _call(self, index, signature, *args):
        if self._object is None:
            return False
        if not self._hwnd and self._widget is not None:
            try:
                self._hwnd = int(self._widget.winId() or 0)
            except (OSError, TypeError, ValueError, AttributeError):
                self._hwnd = 0
        if not self._hwnd:
            return False
        vtable = ctypes.cast(self._object, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        result = int(signature(vtable[index])(self._object, *args))
        self.last_error = result if result < 0 else None
        return result >= 0

    def ensure(self):
        """Create the bridge after Qt has a visible native window handle.

        On some Windows builds the handle returned while a frameless window is
        being constructed is temporary.  The first progress update therefore
        used to become a silent no-op.  Rechecking the handle lazily keeps the
        bridge useful after show/reparent operations without affecting other
        platforms.
        """
        if self._closed or os.name != "nt" or self._widget is None:
            return
        try:
            hwnd = int(self._widget.winId() or 0)
        except (OSError, TypeError, ValueError, AttributeError):
            hwnd = 0
        if not hwnd:
            return
        if hwnd != self._hwnd and self._object is not None:
            self._release_native()
        self._hwnd = hwnd
        if self._object is None:
            try:
                self._create()
            except (OSError, TypeError, ValueError, AttributeError):
                self._release_native()

    def set_value(self, value: int, total: int = 100):
        self._total = max(1, int(total))
        self._value = max(0, min(int(value), self._total))
        self._state = self.TBPF_NORMAL
        self.ensure()
        return self._apply()

    def _apply(self):
        if os.name != 'nt' or self._closed or self._object is None:
            return False
        state = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, wintypes.DWORD)
        set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND,
                                        ctypes.c_ulonglong, ctypes.c_ulonglong)
        success = self._call(10, state, self._hwnd, self._state)
        if success and self._state in (self.TBPF_NORMAL, self.TBPF_PAUSED, self.TBPF_ERROR):
            success = self._call(9, set_value, self._hwnd, self._value, self._total)
        return success

    def set_paused(self):
        self._state = self.TBPF_PAUSED
        self.ensure()
        return self._apply()

    def clear(self):
        self._state = self.TBPF_NOPROGRESS
        self.ensure()
        return self._apply()

    def _release_native(self):
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

    def close(self):
        self._closed = True
        if self._application is not None and self._event_filter is not None:
            try:
                self._application.removeNativeEventFilter(self._event_filter)
            except RuntimeError:
                pass  # QApplication may already have been destroyed at exit.
        self._event_filter = None
        self._application = None
        if self._reapply_timer is not None:
            try:
                self._reapply_timer.stop()
                self._reapply_timer.deleteLater()
            except RuntimeError:
                pass
            self._reapply_timer = None
        self._release_native()

    def __del__(self):
        self.close()
