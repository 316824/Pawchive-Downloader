"""Exercise the real Windows COM interface as well as progress restoration."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_taskbar import TaskbarProgress


@unittest.skipUnless(os.name == 'nt', 'Windows taskbar API')
class TaskbarProgressTests(unittest.TestCase):
    def test_failed_native_creation_balances_com_before_every_retry(self):
        ole = SimpleNamespace(CoInitializeEx=MagicMock(return_value=0),
            CLSIDFromString=MagicMock(return_value=0),
            CoCreateInstance=MagicMock(return_value=-2147467262),
            CoUninitialize=MagicMock())
        with patch('windows_taskbar.ctypes.windll.ole32', ole), \
                patch.object(TaskbarProgress, '_install_shell_notifications'):
            bridge = TaskbarProgress(SimpleNamespace(winId=lambda: 1))
            bridge.ensure(); bridge.ensure()
            self.assertIsNone(bridge._object)
            self.assertFalse(bridge._com_initialized)
            self.assertEqual(ole.CoInitializeEx.call_count, 3)
            self.assertEqual(ole.CoUninitialize.call_count, 3)
            self.assertEqual(bridge.last_error, -2147467262)
            bridge.close()
            self.assertEqual(ole.CoUninitialize.call_count, 3)

    def test_shell_recreation_reapplies_last_progress_without_changing_pause(self):
        bridge = TaskbarProgress()
        bridge._object = object()
        bridge._hwnd = 123
        with patch.object(bridge, 'ensure'), patch.object(bridge, '_call', return_value=True) as call, \
                patch.object(bridge, '_release_native') as release:
            self.assertTrue(bridge.set_value(37, 215))
            self.assertTrue(bridge.set_paused())
            call.reset_mock()
            bridge._shell_button_created()
            release.assert_called_once()
            self.assertEqual(call.call_args_list[0].args[0], 10)
            self.assertEqual(call.call_args_list[0].args[-1], bridge.TBPF_PAUSED)
            self.assertEqual(call.call_args_list[1].args[0], 9)
            self.assertEqual(call.call_args_list[1].args[-2:], (37, 215))
            bridge.clear()
            self.assertEqual(call.call_args.args[-1], bridge.TBPF_NOPROGRESS)
        bridge._object = None
        bridge.close()

    def test_real_native_interface_initializes_and_accepts_progress_calls(self):
        user32 = ctypes.windll.user32
        user32.GetShellWindow.restype = wintypes.HWND
        if not user32.GetShellWindow():
            self.skipTest('No interactive Windows shell in this test session')
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        # A hidden native STATIC window gives the real API a valid HWND and
        # avoids exposing a test window or altering the user's running apps.
        handles = [user32.CreateWindowExW(0, 'STATIC', 'Pawchive taskbar test',
                   0x00CF0000, 0, 0, 320, 180, None, None, None, None) for _ in range(2)]
        self.assertTrue(all(handles), 'Could not create native test windows')
        current = [handles[0]]
        bridge = TaskbarProgress(SimpleNamespace(winId=lambda: current[0]))
        try:
            self.assertIsNotNone(bridge._object,
                f'ITaskbarList3 creation failed, HRESULT={bridge.last_error!r}')
            self.assertTrue(bridge.set_value(73, 215), f'HRESULT={bridge.last_error!r}')
            self.assertTrue(bridge.set_paused(), f'HRESULT={bridge.last_error!r}')
            # A new HWND must be picked up after reparent/frameless changes.
            current[0] = handles[1]
            self.assertTrue(bridge.set_value(99, 215))
            self.assertEqual(bridge._hwnd, handles[1])
            bridge._shell_button_created()
            self.assertIsNone(bridge.last_error)
            self.assertTrue(bridge.clear())
        finally:
            bridge.close()
            for hwnd in handles:
                user32.DestroyWindow(hwnd)
        self.assertIsNone(bridge._object)
        self.assertFalse(bridge._com_initialized)

    def test_native_qt_shell_message_restores_cached_value(self):
        from PySide6.QtWidgets import QApplication, QWidget
        application = QApplication.instance() or QApplication([])
        if application.platformName() != 'windows':
            self.skipTest('Native Windows Qt event delivery is unavailable on offscreen')
        user32 = ctypes.windll.user32
        user32.GetShellWindow.restype = wintypes.HWND
        if not user32.GetShellWindow():
            self.skipTest('No interactive Windows shell in this test session')
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        window = QWidget()
        window.resize(320, 180)
        window.show()
        application.processEvents()
        bridge = TaskbarProgress(window)
        try:
            self.assertIsNotNone(bridge._event_filter)
            self.assertTrue(bridge.set_value(37, 215))
            application.processEvents()
            with patch.object(bridge, '_apply', wraps=bridge._apply) as replay:
                self.assertTrue(user32.PostMessageW(int(window.winId()),
                    user32.RegisterWindowMessageW('TaskbarButtonCreated'), 0, 0))
                deadline = time.monotonic() + 2
                while not replay.called and time.monotonic() < deadline:
                    application.processEvents()
                    time.sleep(.005)
                self.assertTrue(replay.called, 'Native taskbar message did not restore progress')
                self.assertIsNone(bridge.last_error)
                self.assertEqual((bridge._value, bridge._total), (37, 215))
        finally:
            bridge.clear(); bridge.close()
            window.close(); window.deleteLater()
            application.processEvents()


if __name__ == '__main__':
    unittest.main()
