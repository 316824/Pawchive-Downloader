"""Isolated lifecycle integration checks. No real task registration or downloads."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QDialog
from app_state import AppState
from download_journal import DownloadJournal
from desktop_runtime import DesktopRuntime
from feature_dialogs import CheckInDialog, ScheduleDialog
import download_pawchive_images as desktop


class FakeScheduler:
    def __init__(self):self.installed=[]; self.removed=[]; self.fail=False
    def install(self,plan,action):
        if self.fail:raise OSError('fixture registration failure')
        self.installed.append((plan.id,action))
    def remove(self,identifier):self.removed.append(identifier)


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_lifecycle_test_')
        self.root=Path(self.temp.name); self.now=time.time()
        self.state=AppState(self.root/'state',clock=lambda:self.now-60)
        self.journal=DownloadJournal(self.root/'state')
        self.tasks=FakeScheduler()
        self.runtime=DesktopRuntime(self.app,self.state,self.journal,self.tasks,tray_enabled=False)
        self.window=desktop.App(settings=QSettings(str(self.root/'preferences.ini'),QSettings.Format.IniFormat),services=self.runtime)
        self.runtime.attach(self.window); self.runtime.timer.stop()
        self.config=dict(profile='https://pawchive.pw/fanbox/user/offline-test',folder=str(self.root/'saved'),
                         order='newest',by_title=True,rename_order=False,start_number=1)
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('No network in lifecycle tests'))
        self.request=self.network.start()

    def tearDown(self):
        self.runtime.prepare_exit(); self.window.busy=False
        if hasattr(self.window,'worker'):del self.window.worker
        self.window.close(); self.runtime.close(); self.window.deleteLater()
        self.app.processEvents(); QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.request.assert_not_called(); self.network.stop(); self.temp.cleanup()

    def schedule(self,delta=-5):
        return self.state.add_schedule(self.config,self.now+delta)

    def test_due_plans_wait_for_modal_then_dispatch_serially_and_only_once(self):
        first=self.schedule(-6); second=self.schedule(-5)
        launched=[]
        def launch(profile,selected):
            launched.append(profile); self.window.busy=True
        with patch.object(self.window,'launch_worker',side_effect=launch),patch('desktop_runtime.boot_time',return_value=self.now-1000):
            with patch.object(self.app,'activeModalWidget',return_value=QDialog()):self.runtime.poll()
            self.assertEqual(launched,[])
            self.assertEqual(self.state.get_schedule(first.id).status,'queued')
            self.runtime.poll(); self.runtime.poll()
            self.assertEqual(len(launched),1)
            self.runtime.handle_command({'command':'scheduled','id':first.id})
            self.assertEqual(len(launched),1)
            self.runtime.task_finished(True)
            self.runtime.poll()
            self.assertEqual(len(launched),2)
            self.assertEqual(self.state.get_schedule(first.id).status,'completed')
            self.assertEqual(self.runtime.active_schedule,second.id)

    def test_overdue_and_old_queued_plans_are_missed_after_restart_without_os_trigger(self):
        pending=self.schedule(); queued=self.schedule(-4)
        self.state.set_schedule_status(queued.id,'queued')
        with patch('desktop_runtime.boot_time',return_value=self.now-100000):self.runtime.reconcile_schedules()
        self.assertEqual(self.state.get_schedule(pending.id).status,'missed')
        self.assertEqual(self.state.get_schedule(queued.id).status,'missed')

    def test_due_os_trigger_survives_reconciliation_and_repeated_commands(self):
        plan=self.schedule(); self.runtime.startup_schedule_ids.add(plan.id)
        self.window.busy=True
        with patch('desktop_runtime.boot_time',return_value=self.now-1000):
            self.runtime.reconcile_schedules()
            for _ in range(3):self.runtime.handle_command({'command':'scheduled','id':plan.id})
        self.assertEqual(self.state.get_schedule(plan.id).status,'queued')

    def test_registration_failure_rolls_back_reservation_and_cancel_removes_only_its_task(self):
        self.tasks.fail=True
        with self.assertRaises(OSError):self.runtime.add_schedule(self.config,self.now+100)
        self.assertEqual(self.state.list_schedules(),[])
        self.tasks.fail=False
        plan=self.runtime.add_schedule(self.config,self.now+100)
        self.runtime.cancel_schedule(plan.id)
        self.assertEqual(self.state.get_schedule(plan.id).status,'cancelled')
        self.assertEqual(self.tasks.removed[-1],plan.id)

    def test_dispatch_failure_restores_ui_and_does_not_block_future_plans(self):
        plan=self.schedule()
        with patch('desktop_runtime.boot_time',return_value=self.now-1000),patch.object(self.window,'launch_worker',side_effect=ValueError('bad checkpoint')):
            self.runtime.poll()
        self.assertEqual(self.state.get_schedule(plan.id).status,'failed')
        self.assertFalse(self.window.busy)
        self.assertTrue(self.window.select_posts_check.isChecked())
        self.assertIsNone(self.runtime.active_schedule)

    def test_cancelled_checkpoint_resumes_manually_when_all_schedule_slots_are_taken(self):
        previous=self.schedule(-6)
        checkpoint=self.journal.create(self.config)
        self.state.set_setting('schedule_for_journal:'+checkpoint,previous.id)
        self.state.set_schedule_status(previous.id,'cancelled')
        pending=[self.schedule(-5) for _ in range(10)]
        launched=[]

        class ResumeChoice:
            choice='resume'
            def __init__(self,*args):pass
            def exec(self):pass
            def deleteLater(self):pass

        def launch(profile,selected):
            launched.append((profile,selected,self.runtime.resume_job_id))
            self.window.busy=True

        with patch('feature_dialogs.ResumeChoice',ResumeChoice),patch.object(self.window,'launch_worker',side_effect=launch),patch('desktop_runtime.boot_time',return_value=self.now-1000):
            self.runtime.offer_resume()
            self.assertEqual(launched,[(self.config['profile'],None,checkpoint)])
            self.assertIsNone(self.runtime.active_schedule)
            self.assertEqual(self.state.get_schedule(previous.id).status,'cancelled')
            self.assertTrue(all(self.state.get_schedule(plan.id).status=='pending' for plan in pending))
            self.runtime.poll()
            self.assertEqual(len(launched),1)
            self.runtime.task_finished(True)
            self.runtime.poll()
            self.assertEqual(len(launched),2)
            self.assertIn(self.runtime.active_schedule,{plan.id for plan in pending})
            self.assertEqual(self.state.get_schedule(previous.id).status,'cancelled')

    def test_manual_worker_construction_failure_restores_idle_controls_and_logs_error(self):
        self.window.profile.setText(self.config['profile'])
        self.window.path.setText(self.config['folder'])
        with patch.object(desktop,'Worker',side_effect=OSError('fixture checkpoint disk failure')):
            self.window.launch_worker(self.config['profile'],None)
        self.assertFalse(self.window.busy)
        self.assertTrue(self.window.profile.isEnabled())
        self.assertIn('fixture checkpoint disk failure',self.window.log.toPlainText())
        self.assertEqual(self.journal.list_recoverable(),[])

    def test_scan_exit_interrupts_scanner_after_previous_worker_construction_failure(self):
        class Scanner:
            interrupted=False
            def isRunning(self):return True
            def requestInterruption(self):self.interrupted=True

        self.window.path.setText(self.config['folder'])
        with patch.object(desktop,'Worker',side_effect=OSError('fixture checkpoint disk failure')):
            self.window.launch_worker(self.config['profile'],None)
        self.assertIsNone(self.window.worker)
        self.window.scanner=Scanner()
        self.window.busy=True
        self.runtime.prepare_exit()
        event=QCloseEvent()
        self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(self.window.scanner.interrupted)
        self.assertTrue(self.window._close_when_worker_stops)

    def test_worker_binding_failure_does_not_start_and_retains_failed_checkpoint(self):
        self.window.profile.setText(self.config['profile'])
        self.window.path.setText(self.config['folder'])
        created=[]
        def binding_failed(worker):
            created.append(worker)
            raise OSError('fixture schedule binding failure')
        with patch.object(desktop.Worker,'start') as start,patch.object(self.runtime,'worker_created',side_effect=binding_failed):
            self.window.launch_worker(self.config['profile'],None)
        start.assert_not_called()
        self.assertEqual(len(created),1)
        job=self.journal.load(created[0].job_id)
        self.assertEqual(job['status'],'failed')
        self.assertEqual(job['config']['folder'],self.config['folder'])
        self.assertIn(job['id'],[item['id'] for item in self.journal.list_recoverable()])
        self.assertFalse(self.window.busy)
        self.assertIn('fixture schedule binding failure',self.window.log.toPlainText())

    def test_exit_marks_running_worker_for_preservation_and_pauses_its_plan(self):
        class Worker:
            preserve_on_interrupt=False
            def isRunning(self):return True
        worker=Worker(); self.window.worker=worker; self.window.busy=True
        plan=self.schedule(); self.runtime.active_schedule=plan.id
        self.state.set_schedule_status(plan.id,'running')
        self.runtime.prepare_exit()
        self.assertTrue(worker.preserve_on_interrupt)
        self.assertFalse(self.runtime.timer.isActive())
        self.runtime.task_finished(False,'interrupted',paused=True)
        self.assertEqual(self.state.get_schedule(plan.id).status,'paused')

    def test_calendar_records_today_once_and_schedule_dialog_retains_custom_checkboxes(self):
        calendar=CheckInDialog(self.window,self.state)
        calendar.check_in(); calendar.check_in()
        self.assertEqual(self.state.checkin_stats()['total'],1)
        self.assertFalse(calendar.button.isEnabled())
        schedule=ScheduleDialog(self.window,self.runtime)
        self.assertIsInstance(schedule.by_title,desktop.GlowCheckBox)
        self.assertIsInstance(schedule.rename,desktop.GlowCheckBox)
        for dialog in (calendar,schedule):dialog.grab(); dialog.close(); dialog.deleteLater()

    def test_network_titles_and_errors_are_literal_plain_text_in_logs(self):
        self.window.log.append('<b>title</b> & <img src="file:///secret">')
        self.assertEqual(self.window.log.toPlainText(),'<b>title</b> & <img src="file:///secret">')


if __name__=='__main__':unittest.main()
