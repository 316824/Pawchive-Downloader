"""Queue checkpoints commit atomically without filesystem work under the DB lock."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from download_journal import DownloadJournal


class JournalBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_journal_batch_')
        self.root=Path(self.temp.name)
        self.journal=DownloadJournal(self.root/'journal')
        self.config=dict(folder=str(self.root/'downloads'),
                         profile='https://pawchive.pw/fanbox/user/1234',by_title=False)

    def tearDown(self):
        self.journal.close();self.temp.cleanup()

    def jobs(self,count=3):
        return [self.journal.prepare_new({**self.config,'task_title':str(index)},status='paused')
                for index in range(count)]

    def test_complete_queue_is_one_full_durable_commit_before_reopen(self):
        jobs=self.jobs(300);statements=[]
        self.assertFalse(self.journal.list_recoverable())
        self.journal._db.set_trace_callback(statements.append)
        try:self.journal.save_many(jobs)
        finally:self.journal._db.set_trace_callback(None)
        self.assertEqual(sum(sql.strip().upper()=='COMMIT' for sql in statements),1)
        self.assertEqual(self.journal._db.execute('PRAGMA synchronous').fetchone()[0],2)
        self.assertEqual(self.journal._db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
        self.journal.close();self.journal=DownloadJournal(self.root/'journal')
        recovered=self.journal.list_recoverable()
        self.assertEqual({job['id'] for job in recovered},{job['id'] for job in jobs})
        self.assertTrue(all(job['status']=='paused' for job in recovered))

    def test_write_failure_rolls_back_new_jobs_and_existing_checkpoint_changes(self):
        old_id=self.journal.create(self.config,status='paused')
        original=self.journal.load(old_id);changed=copy.deepcopy(original)
        changed['config']['task_title']='uncommitted edit';changed['status']='failed'
        jobs=[changed,*self.jobs(2)];write=self.journal._write_prepared;calls=[]
        def fail_second(prepared):
            calls.append(prepared['header'][0])
            if len(calls)==2:raise OSError('simulated disk failure')
            return write(prepared)
        with patch.object(self.journal,'_write_prepared',side_effect=fail_second):
            with self.assertRaisesRegex(OSError,'disk failure'):self.journal.save_many(jobs)
        self.journal.close();self.journal=DownloadJournal(self.root/'journal')
        self.assertEqual(self.journal.load(old_id),original)
        self.assertEqual([job['id'] for job in self.journal.list_recoverable()],[old_id])

    def test_validation_is_outside_lock_and_only_sql_is_inside(self):
        jobs=self.jobs();validate=self.journal._validate;write=self.journal._write_prepared
        validations=[];writes=[]
        def check_paths(*args,**kwargs):
            self.assertFalse(self.journal._lock._is_owned())
            validations.append(True);return validate(*args,**kwargs)
        def write_sql(prepared):
            self.assertTrue(self.journal._lock._is_owned())
            writes.append(True);return write(prepared)
        with (patch.object(self.journal,'_validate',side_effect=check_paths),
              patch.object(self.journal,'_write_prepared',side_effect=write_sql)):
            self.journal.save_many(jobs)
        self.assertEqual(len(validations),len(jobs));self.assertEqual(len(writes),len(jobs))

    def test_interruption_during_sql_write_rolls_back_the_entire_batch(self):
        jobs=self.jobs();checks=[]
        def cancelled():
            checks.append(True)
            return len(checks)==len(jobs)+2
        with self.assertRaisesRegex(RuntimeError,'cancelled'):
            self.journal.save_many(jobs,cancelled=cancelled)
        self.assertFalse(self.journal.list_recoverable())

    def test_large_serialized_batch_spills_and_closes_owned_temporary_on_failure(self):
        jobs=self.jobs();spools=[];factory=tempfile.SpooledTemporaryFile
        def bounded_spool(*args,**kwargs):
            self.assertEqual(kwargs['max_size'],8*1024*1024)
            kwargs['max_size']=1
            spool=factory(*args,**kwargs);spools.append(spool);return spool
        def fail_write(_):
            self.assertTrue(spools[0]._rolled)
            raise OSError('simulated SQL failure after spill')
        with (patch('download_journal.tempfile.SpooledTemporaryFile',side_effect=bounded_spool),
              patch.object(self.journal,'_write_prepared',side_effect=fail_write)):
            with self.assertRaisesRegex(OSError,'SQL failure'):self.journal.save_many(jobs)
        self.assertTrue(spools[0].closed)
        self.assertFalse(self.journal.list_recoverable())

    def test_invalid_later_job_writes_nothing_and_duplicate_ids_are_rejected(self):
        jobs=self.jobs();jobs[1]['config']['profile']='https://foreign.example/user/123'
        with patch.object(self.journal,'_write_prepared') as write:
            with self.assertRaises(ValueError):self.journal.save_many(jobs)
            write.assert_not_called()
        self.assertFalse(self.journal.list_recoverable())
        job=self.jobs(1)[0]
        with self.assertRaisesRegex(ValueError,'Duplicate checkpoint'):
            self.journal.save_many([job,job])


if __name__=='__main__':unittest.main()
