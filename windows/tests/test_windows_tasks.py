"""Task registration tests use a fake process runner; no OS tasks are created."""
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app_state import Schedule
from windows_tasks import (NS, PlanTaskError, TaskAction, WindowsTaskScheduler,
                           build_task_xml, task_name)


class WindowsTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_task_test_')
        self.root=Path(self.temp.name)
        self.id='a5ec4f34-aef0-4534-8b3a-9cc38366b537'
        self.sid='S-1-5-21-123-456-789-1001'
        self.plan=Schedule(self.id,datetime(2031,4,15,12,tzinfo=timezone.utc).timestamp(),
                           'Asia/Shanghai',{},'pending','','',1,1)

    def tearDown(self):self.temp.cleanup()

    def test_xml_uses_current_user_no_password_wake_catchup_or_elevation(self):
        exe=self.root/'space & Unicode 秋'/'app.exe'
        action=TaskAction.for_application(self.id,executable=exe)
        self.assertEqual(action.arguments,('--scheduled',self.id))
        node=ET.fromstring(build_task_xml(self.plan,action,self.sid))
        def text(path):return node.findtext(path,namespaces={'t':NS})
        self.assertEqual(text('t:Principals/t:Principal/t:UserId'),self.sid)
        self.assertEqual(text('t:Principals/t:Principal/t:LogonType'),'InteractiveToken')
        self.assertEqual(text('t:Principals/t:Principal/t:RunLevel'),'LeastPrivilege')
        self.assertEqual(text('t:Settings/t:WakeToRun'),'false')
        self.assertEqual(text('t:Settings/t:StartWhenAvailable'),'false')
        self.assertEqual(text('t:Settings/t:StopIfGoingOnBatteries'),'false')
        self.assertEqual(text('t:Settings/t:ExecutionTimeLimit'),'PT0S')
        self.assertEqual(text('t:Actions/t:Exec/t:Command'),str(exe))
        self.assertEqual(text('t:Actions/t:Exec/t:Arguments'),'--scheduled '+self.id)
        self.assertEqual(text('t:Triggers/t:TimeTrigger/t:StartBoundary'),'2031-04-15T12:00:00+00:00')
        self.assertNotIn('password',build_task_xml(self.plan,action,self.sid).decode('utf-16').lower())

    def test_source_action_quotes_only_script_and_known_id_without_shell(self):
        exe=self.root/'Python'/'python.exe'; script=self.root/'a b & c'/'download.py'
        action=TaskAction.for_application(self.id,executable=exe,script=script,frozen=False)
        self.assertEqual(action.arguments,(str(script),'--scheduled',self.id))
        node=ET.fromstring(build_task_xml(self.plan,action,self.sid))
        self.assertEqual(node.findtext(f'{{{NS}}}Actions/{{{NS}}}Exec/{{{NS}}}Arguments'),subprocess.list2cmdline(action.arguments))
        with self.assertRaises(ValueError):TaskAction(str(exe),('/C','do bad things'),str(self.root))
        with self.assertRaises(ValueError):task_name('../../Microsoft/Other',self.sid)
        self.assertNotEqual(task_name(self.id,self.sid),task_name(self.id,self.sid+'9'))

    def test_registration_and_deletion_only_use_owned_task_with_temporary_xml(self):
        calls=[]; xml_paths=[]
        def runner(args,**kwargs):
            calls.append((args,kwargs))
            self.assertFalse(kwargs.get('shell',False))
            if '/XML' in args:
                path=Path(args[args.index('/XML')+1]); xml_paths.append(path)
                ET.fromstring(path.read_bytes())
            return subprocess.CompletedProcess(args,0,b'',b'')
        tasks=WindowsTaskScheduler(user_sid=self.sid,runner=runner,executable='schtasks.exe')
        action=TaskAction.for_application(self.id,executable=self.root/'app.exe')
        name=tasks.install(self.plan,action)
        self.assertTrue(name.startswith('PawchiveDownloader-'))
        self.assertTrue(tasks.remove(self.id))
        self.assertEqual(calls[0][0][1:4],['/Create','/TN',name])
        self.assertEqual(calls[1][0][1:],['/Delete','/TN',name,'/F'])
        self.assertFalse(xml_paths[0].exists())
        with self.assertRaises(ValueError):tasks.remove('OtherVendorTask')
        self.assertEqual(len(calls),2)

    def test_failed_registration_is_visible_and_missing_delete_is_idempotent(self):
        action=TaskAction.for_application(self.id,executable=self.root/'app.exe')
        fail=lambda args,**kwargs:subprocess.CompletedProcess(args,1,b'',b'ERROR: Service unavailable')
        tasks=WindowsTaskScheduler(user_sid=self.sid,runner=fail,executable='schtasks.exe')
        with self.assertRaises(PlanTaskError):tasks.install(self.plan,action)
        with self.assertRaises(PlanTaskError):tasks.remove(self.id)
        missing=lambda args,**kwargs:subprocess.CompletedProcess(args,1,b'',b'ERROR: The system cannot find the file specified.')
        tasks=WindowsTaskScheduler(user_sid=self.sid,runner=missing,executable='schtasks.exe')
        self.assertFalse(tasks.remove(self.id))


if __name__=='__main__':unittest.main()
