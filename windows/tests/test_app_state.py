"""Persistent state checks use only disposable directories and an injected clock."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app_state import AppState, ScheduleLimitError, StateError, due_eligible


class AppStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_state_test_')
        self.root=Path(self.temp.name)
        self.now=[datetime(2031,4,15,12).timestamp()]
        self.store=AppState(self.root,clock=lambda:self.now[0])
        self.snapshot=dict(profile='https://pawchive.pw/fanbox/user/offline-test',folder=str(self.root/'downloads'),
                           order='newest',by_title=True,rename_order=False,start_number=1)

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_settings_and_schedule_snapshot_survive_reopen_without_download_files(self):
        self.store.set_setting('test_preferences',{'language':'日本語','enabled':True})
        job=self.store.add_schedule(self.snapshot,self.now[0]+60,timezone='Asia/Shanghai')
        self.snapshot['folder']='not-the-saved-value'
        self.store.close(); self.store=AppState(self.root,clock=lambda:self.now[0])
        self.assertEqual(self.store.get_setting('test_preferences')['language'],'日本語')
        saved=self.store.get_schedule(job.id)
        self.assertEqual(saved.snapshot['folder'],str(self.root/'downloads'))
        self.assertEqual(saved.timezone,'Asia/Shanghai')
        self.assertEqual(saved.as_dict()['id'],job.id)
        self.assertEqual([p.name for p in self.root.iterdir()],['state.sqlite3'])

    def test_limit_counts_paused_and_queued_but_frees_finished_appointments(self):
        jobs=[self.store.add_schedule(self.snapshot,self.now[0]+60+i) for i in range(10)]
        self.store.set_schedule_status(jobs[0].id,'paused')
        self.store.set_schedule_status(jobs[1].id,'queued')
        with self.assertRaises(ScheduleLimitError):self.store.add_schedule(self.snapshot,self.now[0]+100)
        self.store.set_schedule_status(jobs[2].id,'completed')
        replacement=self.store.add_schedule(self.snapshot,self.now[0]+100)
        self.assertEqual(len(self.store.list_schedules(active_only=True)),10)
        with self.assertRaises(ScheduleLimitError):self.store.set_schedule_status(jobs[2].id,'pending')
        self.assertEqual(self.store.get_schedule(replacement.id).status,'pending')

    def test_missed_shutdown_or_expired_times_never_catch_up(self):
        old=self.store.add_schedule(self.snapshot,self.now[0]+10)
        recent=self.store.add_schedule(self.snapshot,self.now[0]+200)
        overdue=self.store.add_schedule(self.snapshot,self.now[0]+20)
        self.store.set_schedule_status(old.id,'queued')
        self.now[0]+=210
        queued=self.store.enqueue_due(self.now[0],self.now[0]-100)
        self.assertEqual([p.id for p in queued],[recent.id])
        self.assertEqual(self.store.get_schedule(old.id).status,'missed')
        self.assertEqual(self.store.get_schedule(overdue.id).status,'missed')
        self.assertFalse(due_eligible(self.now[0]-121,self.now[0],self.now[0]-1000))

    def test_same_session_queue_can_wait_behind_busy_work_without_being_missed(self):
        first=self.store.add_schedule(self.snapshot,self.now[0]+10)
        second=self.store.add_schedule(self.snapshot,self.now[0]+11)
        boot=self.now[0]-100
        self.now[0]+=15
        self.store.enqueue_due(self.now[0],boot)
        self.assertEqual(self.store.claim_next_schedule().id,first.id)
        with self.assertRaises(StateError):self.store.set_schedule_status(second.id,'running')
        self.now[0]+=3600
        self.store.enqueue_due(self.now[0],boot)
        self.assertIsNone(self.store.claim_next_schedule())
        self.assertEqual(self.store.get_schedule(second.id).status,'queued')
        self.store.set_schedule_status(first.id,'completed')
        self.assertEqual(self.store.claim_next_schedule().id,second.id)

    def test_concurrent_claims_start_a_schedule_only_once_and_pause_is_not_auto_resumed(self):
        job=self.store.add_schedule(self.snapshot,self.now[0]+1)
        self.now[0]+=2; self.store.enqueue_due(self.now[0],self.now[0]-100)
        peer=AppState(self.root,clock=lambda:self.now[0]); claims=[]
        try:
            threads=[threading.Thread(target=lambda s=s:claims.append(s.claim_next_schedule())) for s in (self.store,peer)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(3)
            self.assertEqual([p.id for p in claims if p],[job.id])
            self.store.set_schedule_status(job.id,'paused')
            self.assertIsNone(peer.claim_next_schedule())
        finally:peer.close()

    def test_checkin_is_daily_local_and_streak_has_no_backfill(self):
        day=datetime.fromtimestamp(self.now[0]).date()
        self.assertTrue(self.store.check_in().created)
        self.assertFalse(self.store.check_in().created)
        self.now[0]+=86400; self.store.check_in()
        self.assertEqual(self.store.checkin_stats()['total'],2)
        self.assertEqual(self.store.checkin_stats()['streak'],2)
        self.assertEqual(self.store.checkin_dates(day.year,day.month),{day.isoformat(),(day+timedelta(days=1)).isoformat()})
        self.now[0]+=86400
        self.assertEqual(self.store.checkin_stats()['streak'],2)
        self.now[0]+=86400
        self.assertEqual(self.store.checkin_stats()['streak'],0)
        with self.assertRaises(ValueError):self.store.check_in(day)
        self.store.check_in()
        self.assertEqual(self.store.checkin_stats()['longest_streak'],2)

    def test_invalid_links_folders_and_relative_appointments_are_rejected(self):
        for change in ({'profile':'https://example.com/fanbox/user/x'}, {'folder':'relative'},
                       {'order':'invalid'},{'start_number':-1},{'start_number':True}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.store.add_schedule(dict(self.snapshot,**change),self.now[0]+60)
        with self.assertRaises(ValueError):self.store.add_schedule(self.snapshot,self.now[0]-1)
        with self.assertRaises(ValueError):self.store.add_schedule(self.snapshot,datetime(2031,4,15,13))


if __name__=='__main__':unittest.main()
