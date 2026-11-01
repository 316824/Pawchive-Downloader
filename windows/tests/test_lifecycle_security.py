"""Offline lifecycle/activation integration. All state and signing keys are temporary."""
import base64
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import uuid

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

import activation
from app_state import AppState
import desktop_entry
from desktop_runtime import SingleInstance
from feature_dialogs import ActivationDialog


def b64(value):return base64.urlsafe_b64encode(value).decode().rstrip('=')


class LifecycleSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='pawchive_lifecycle_security_')
        self.root=Path(self.temporary.name)
        self.store=AppState(self.root)
        self.dialogs=[]; self.instances=[]
        self.private=Ed25519PrivateKey.generate()
        self.device=activation.device_code('isolated-lifecycle-security-machine')
        self.key_patch=patch.object(activation,'load_public_key',return_value=self.private.public_key())
        self.key_patch.start()
        self.device_patch=patch.object(activation,'device_code',return_value=self.device)
        self.device_patch.start()
        self.requests=patch('requests.sessions.Session.request',side_effect=AssertionError('Lifecycle tests must stay offline'))
        self.network=self.requests.start()

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close(); dialog.deleteLater()
        for instance in reversed(self.instances):
            instance.close(); instance.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.network.assert_not_called()
        self.requests.stop(); self.device_patch.stop(); self.key_patch.stop()
        self.store.close(); self.temporary.cleanup()

    def token(self,purpose='activate'):
        payload=dict(v=1,app=activation.APP_ID,device=self.device,purpose=purpose,
                     nonce=b64(secrets.token_bytes(16)),issued=int(time.time()))
        message=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
        return 'PWI1.'+b64(message)+'.'+b64(self.private.sign(message))

    def dialog(self):
        result=ActivationDialog(self.store)
        self.dialogs.append(result); result.show(); self.app.processEvents()
        return result

    def instance(self):
        result=SingleInstance(self.root)
        self.instances.append(result)
        return result

    def test_tenth_correct_code_is_accepted_by_the_real_activation_ui(self):
        dialog=self.dialog()
        for _ in range(9):
            dialog.token.setPlainText('wrong')
            QTest.mouseClick(dialog.submit,Qt.MouseButton.LeftButton)
        self.assertEqual(activation.remaining_attempts(self.store),1)
        self.assertTrue(dialog.submit.isEnabled())
        dialog.token.setPlainText(self.token())
        QTest.mouseClick(dialog.submit,Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.result(),QDialog.DialogCode.Accepted)
        self.store.close(); self.store=AppState(self.root)
        self.assertTrue(activation.is_activated(self.store))

    def test_ui_lock_survives_reopen_and_one_time_unlock_restores_the_input(self):
        dialog=self.dialog(); dialog.token.setPlainText('wrong')
        for _ in range(10):QTest.mouseClick(dialog.submit,Qt.MouseButton.LeftButton)
        self.assertEqual(activation.remaining_attempts(self.store),0)
        self.assertFalse(dialog.submit.isEnabled()); self.assertTrue(dialog.token.isReadOnly())
        dialog.close()
        self.store.close(); self.store=AppState(self.root)
        reopened=self.dialog()
        self.assertFalse(reopened.submit.isEnabled())
        unlock=self.token('unlock')
        with patch('feature_dialogs.QInputDialog.getMultiLineText',return_value=(unlock,True)):
            QTest.mouseClick(reopened.unlock_button,Qt.MouseButton.LeftButton)
        self.assertEqual(activation.remaining_attempts(self.store),10)
        self.assertFalse(reopened.token.isReadOnly())
        self.store.set_setting(activation.FAILURES_KEY,10); reopened.update_limit()
        with patch('feature_dialogs.QInputDialog.getMultiLineText',return_value=(unlock,True)):
            QTest.mouseClick(reopened.unlock_button,Qt.MouseButton.LeftButton)
        self.assertEqual(activation.remaining_attempts(self.store),0)
        self.assertFalse(reopened.submit.isEnabled())

    def test_empty_input_and_language_switches_do_not_spend_attempts(self):
        dialog=self.dialog()
        self.assertFalse(dialog.submit.isEnabled())
        QTest.mouseClick(dialog.submit,Qt.MouseButton.LeftButton)
        for language in ('English','日本語','繁體中文','简体中文'):
            dialog.languages.setCurrentText(language); self.app.processEvents()
            self.assertEqual(activation.remaining_attempts(self.store),10)
            self.assertFalse(dialog.submit.isEnabled())
        dialog.token.setPlainText('wrong'); QTest.mouseClick(dialog.submit,Qt.MouseButton.LeftButton)
        dialog.languages.setCurrentText('English')
        self.assertEqual(activation.remaining_attempts(self.store),9)
        self.assertIn('9 / 10',dialog.counter.text())

    def test_rejected_startup_gate_releases_store_and_single_instance_without_constructing_window(self):
        instance=self.instance(); gate=MagicMock()
        gate.exec.return_value=QDialog.DialogCode.Rejected; gate.DialogCode=QDialog.DialogCode
        window=MagicMock()
        with patch.object(desktop_entry,'QApplication',return_value=self.app), \
             patch.object(desktop_entry,'AppState',return_value=self.store), \
             patch.object(desktop_entry,'SingleInstance',return_value=instance), \
             patch.object(desktop_entry,'is_activated',return_value=False), \
             patch.object(desktop_entry,'ActivationDialog',return_value=gate), \
             patch.object(desktop_entry,'DesktopRuntime') as runtime:
            self.assertEqual(desktop_entry.run(window,[]),0)
        window.assert_not_called(); runtime.assert_not_called()
        self.assertTrue(self.store._closed)
        second=self.instance(); self.assertTrue(second.acquire())

    def test_window_construction_failure_releases_runtime_resources_and_lock(self):
        instance=self.instance(); window=MagicMock(side_effect=RuntimeError('offline startup fixture'))
        with patch.object(desktop_entry,'QApplication',return_value=self.app), \
             patch.object(desktop_entry,'AppState',return_value=self.store), \
             patch.object(desktop_entry,'SingleInstance',return_value=instance), \
             patch.object(desktop_entry,'is_activated',return_value=True), \
             patch.object(desktop_entry.QMessageBox,'critical') as error_box:
            self.assertEqual(desktop_entry.run(window,[]),1)
        error_box.assert_called_once(); self.assertTrue(self.store._closed)
        second=self.instance(); self.assertTrue(second.acquire())

    def send_local(self,instance,wire):
        socket=QLocalSocket()
        socket.connectToServer(instance.name)
        deadline=time.monotonic()+2
        while socket.state()!=QLocalSocket.LocalSocketState.ConnectedState and time.monotonic()<deadline:
            QTest.qWait(5)
        self.assertEqual(socket.state(),QLocalSocket.LocalSocketState.ConnectedState)
        socket.write(wire); socket.flush(); QTest.qWait(50)
        answer=bytes(socket.readAll()) if socket.bytesAvailable() else b''
        socket.abort(); socket.deleteLater(); self.app.processEvents()
        return answer

    def test_single_instance_and_local_commands_reject_untrusted_shapes(self):
        primary=self.instance(); self.assertTrue(primary.acquire())
        second=self.instance(); self.assertFalse(second.acquire()); second.close()
        received=[]; primary.message.connect(received.append)
        self.assertTrue(self.send_local(primary,b'{"command":"show","extra":"ignored"}\n').startswith(b'OK'))
        identifier=str(uuid.uuid4())
        self.assertTrue(self.send_local(primary,json.dumps({'command':'scheduled','id':identifier}).encode()+b'\n').startswith(b'OK'))
        for wire in (b'{"command":"execute","path":"untrusted"}\n',
                     b'{"command":"scheduled","id":"not-a-uuid"}\n',b'not json\n',b'x'*8193+b'\n'):
            self.assertFalse(self.send_local(primary,wire).startswith(b'OK'))
        self.assertEqual(received,[{'command':'show'},{'command':'scheduled','id':identifier}])
        primary.close(); self.assertFalse(primary.buffers)
        self.assertTrue(second.acquire())

    def test_client_packaging_mentions_only_public_activation_material(self):
        source=Path(__file__).resolve().parents[1]
        spec=(source/'PawchiveDownloader.spec').read_text(encoding='utf-8').lower()
        self.assertIn('activation_public.json',spec)
        for forbidden in ('owner-signing-key','private-invitation-tool','owner_keys.py','invitation_generator.py'):
            self.assertNotIn(forbidden,spec)
        manifest=json.loads((source/'activation_public.json').read_text(encoding='utf-8'))
        self.assertEqual(set(manifest),{'schema','app_id','key_id','public_key_ed25519'})


if __name__=='__main__':unittest.main()
