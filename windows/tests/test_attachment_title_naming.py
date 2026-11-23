"""Flat attachment names are reserved once; workers and recovery never rename them."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from PySide6.QtWidgets import QApplication
from download_journal import DownloadJournal, valid_manifest_component
from queue_operations import QueuePreparationWorker
from static_download_plan import assign_plans
from test_download_resume import PROFILE, Response


ZIP = b'PK\x03\x04' + b'isolated attachment payload' * 12


def asset(name, index, *, direct=True):
    return dict(url=f'https://file.pawchive.pw/data/{index}/{name}',
                name=name, direct=direct, index=index)


def post(title, assets, key='one', date='2026-11-22'):
    return dict(url=PROFILE + '/post/' + key, date=date, title=title, assets=assets)


class AttachmentTitleNamingTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(by_title=False, queue_rename_order=True, content_mode='all')

    def names(self, entries, **changes):
        plans = assign_plans(entries, {**self.config, **changes})
        return [[item['save_name'] for item in entry['static_plan']] for entry in plans]

    def test_single_non_image_uses_title_even_without_image_numbering(self):
        entries = [post('日本画师:春/夜', [asset('source.PSD', 0)])]
        for numbering in (True, False):
            with self.subTest(numbering=numbering):
                self.assertEqual(self.names(entries, queue_rename_order=numbering),
                                 [['日本画师_春_夜.PSD']])

    def test_multiple_non_images_use_per_album_sequence_in_manifest_order(self):
        entries = [post('图集甲', [asset('source.CLIP', 8), asset('preview.png', 1, direct=False),
                                  asset('archive.zip', 4), asset('trailer.mp4', 6)])]
        plans = assign_plans(entries, self.config)[0]['static_plan']
        self.assertEqual([item['save_name'] for item in plans],
                         ['1.png', '图集甲_1.zip', '图集甲_2.mp4', '图集甲_3.CLIP'])
        self.assertEqual([item['number'] for item in plans], [1, None, None, None])
        self.assertTrue(all(item['relative_dir'] == '.' for item in plans))

    def test_title_folders_keep_original_attachment_names(self):
        entries = [post('图集甲', [asset('source.PSD', 0), asset('source.clip', 1)])]
        plans = assign_plans(entries, {**self.config, 'by_title': True})[0]['static_plan']
        self.assertEqual([item['save_name'] for item in plans], ['source.PSD', 'source.clip'])
        self.assertEqual([Path(item['relative_dir']).parts for item in plans],
                         [('图集甲', '附件文件')] * 2)

    def test_direct_images_keep_name_stream_and_do_not_consume_image_numbers(self):
        entries = [post('Album', [asset('direct.PNG', 0), asset('source.zip', 1),
                                  asset('art.jpg', 2, direct=False)])]
        plans = assign_plans(entries, self.config)[0]['static_plan']
        self.assertEqual([item['save_name'] for item in plans], ['direct.PNG', 'Album.zip', '1.jpg'])
        self.assertEqual([item['direct'] for item in plans], [True, True, False])
        self.assertEqual(self.names(entries, content_mode='attachments'), [['direct.PNG', 'Album.zip']])
        self.assertEqual(self.names(entries, content_mode='images'), [['1.jpg']])

    def test_sequence_counts_selected_unique_non_images_only(self):
        source = asset('source.zip', 1)
        entries = [post('Album', [asset('cover.png', 0, direct=False), source, dict(source)])]
        self.assertEqual(self.names(entries, content_mode='attachments'), [['Album.zip']])
        self.assertEqual(self.names(entries, content_mode='images'), [['1.png']])

    def test_duplicate_and_sanitized_titles_reserve_unique_names_case_insensitively(self):
        entries = [post(title, [asset('source.zip', index)], str(index))
                   for index, title in enumerate(['a/b', 'a\\b', 'A_B', 'a_b (2)'])]
        names = [group[0] for group in self.names(entries)]
        self.assertEqual(len(set(name.casefold() for name in names)), 4)
        self.assertTrue(all(valid_manifest_component(name) for name in names))

    def test_empty_device_and_long_unicode_titles_are_safe(self):
        for title in ('', 'NUL', 'LPT¹', '末尾点. ', '画师名字' * 200, '图集.' + '长标题' * 200):
            with self.subTest(title=title[:20]):
                name = self.names([post(title, [asset('source.zip', 0)])])[0][0]
                self.assertTrue(valid_manifest_component(name), name)
                self.assertLessEqual(len(name), 180)
                self.assertTrue(name.endswith('.zip'))

    def test_compound_archive_suffixes_survive_title_replacement(self):
        self.assertEqual(self.names([post('Album', [asset('source.TAR.GZ', 0)])]), [['Album.TAR.GZ']])

    def test_duplicate_compound_archives_keep_full_extension_after_collision(self):
        entries = [post('Album', [asset('source.tar.gz', 0)], 'first'),
                   post('Album', [asset('source.tar.gz', 1)], 'second')]
        self.assertEqual(self.names(entries), [['Album.tar.gz'], ['Album (2).tar.gz']])

    def test_max_length_original_name_collision_retains_distinguishing_marker(self):
        original = 'x' * 176 + '.png'
        entries = [post('Album', [asset(original, 0, direct=False)], 'first'),
                   post('Album', [asset(original, 1, direct=False)], 'second')]
        names = [group[0] for group in self.names(entries, queue_rename_order=False)]
        self.assertEqual(names[0], original)
        self.assertTrue(names[1].endswith(' (2).png'))
        self.assertLessEqual(len(names[1]), 180)
        self.assertNotEqual(names[0], names[1])

    def test_main_thread_reservation_does_not_mutate_source_manifest(self):
        entries = [post('Album', [asset('source.zip', 1), asset('source.mp4', 2)])]
        before = copy.deepcopy(entries)
        first = assign_plans(entries, self.config)
        second = assign_plans(entries, self.config)
        self.assertEqual(entries, before)
        self.assertEqual(first, second)


class AttachmentTitleRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_attachment_names_')
        self.root = Path(self.temp.name)
        self.folder = self.root / 'downloads'
        self.journal = DownloadJournal(self.root / 'records')
        self.config = dict(folder=str(self.folder), profile=PROFILE, by_title=False, order='newest',
                           content_mode='attachments', queue_task=True, queue_rename_order=True,
                           rename_order=False, layout_version=3)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def prepare(self, entries, config=None):
        worker = QueuePreparationWorker(self.journal, entries, config or self.config)
        results, errors = [], []
        worker.result.connect(results.append)
        worker.failed.connect(errors.append)
        worker.run()
        self.assertFalse(errors, errors)
        return results[0]

    def worker(self, entry):
        import download_pawchive_images as desktop
        worker = desktop.Worker(self.folder, PROFILE, 'newest', False, journal=self.journal,
                                resume_job_id=entry['metadata']['job_id'])
        worker.interrupted = False
        worker.isInterruptionRequested = lambda: worker.interrupted
        worker.wait_interruptibly = lambda seconds: None
        worker.errors, worker.success = [], []
        worker.failed.connect(worker.errors.append)
        worker.finished_ok.connect(worker.success.append)
        return worker

    def session(self, worker, assets, interrupt_url=None):
        class ZipSession:
            def __init__(self):
                self.headers, self.calls = {}, []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if url not in {item['url'] for item in assets}:
                    raise AssertionError('Unexpected network discovery for immutable plan: ' + url)
                offset = int(kwargs.get('headers', {}).get('Range', 'bytes=0-')[6:-1])
                headers = {'content-type': 'application/zip', 'content-length': str(len(ZIP) - offset),
                           'etag': '"attachment-fixture"'}
                if offset:
                    headers['content-range'] = f'bytes {offset}-{len(ZIP)-1}/{len(ZIP)}'
                callback = (lambda: setattr(worker, 'interrupted', True)) if url == interrupt_url else None
                return Response(ZIP[offset:], 206 if offset else 200, headers, interrupt=callback)

            def close(self):
                pass

        return ZipSession()

    def test_reopen_resume_keeps_reserved_title_names_and_range_offset(self):
        assets = [asset('one.zip', 0), asset('two.zip', 1)]
        entry = self.prepare(assign_plans([post('春夜', assets)], self.config))[0]
        first = self.worker(entry)
        first.preserve_on_interrupt = True
        with patch('resumable_worker.requests.Session', return_value=self.session(first, assets, assets[1]['url'])):
            first.run()
        job = self.journal.load(first.job_id)
        self.assertEqual(job['status'], 'paused')
        saved = copy.deepcopy(job['config']['static_plan'])
        self.assertEqual((self.folder / '春夜_1.zip').read_bytes(), ZIP)
        self.assertEqual(Path(job['files'][1]['part']).read_bytes(), ZIP[:40])
        self.journal.close()
        self.journal = DownloadJournal(self.root / 'records')
        # Current controls/metadata must not reinterpret a previously allocated plan.
        altered = dict(entry, title='Different title')
        recovered = self.prepare([altered], {**self.config, 'by_title': True})[0]
        self.assertEqual(self.journal.load(first.job_id)['config']['static_plan'], saved)
        resumed = self.worker(recovered)
        session = self.session(resumed, assets)
        with patch('resumable_worker.requests.Session', return_value=session):
            resumed.run()
        self.assertFalse(resumed.errors, resumed.errors)
        self.assertEqual(resumed.success, [2])
        self.assertEqual((self.folder / '春夜_2.zip').read_bytes(), ZIP)
        self.assertEqual([kwargs['headers']['Range'] for _, kwargs in session.calls], ['bytes=40-'])
        self.assertFalse(list(self.folder.glob('*.part')))

    def test_preexisting_v13_attachment_plan_keeps_original_filename(self):
        assets = [asset('original.zip', 0)]
        legacy = assign_plans([post('Album', assets)], self.config)
        legacy[0]['static_plan'][0]['save_name'] = 'original.zip'
        entry = self.prepare(legacy)[0]
        # A different new-version plan with the same persisted job is ignored.
        new = assign_plans([post('New title', assets)], self.config)[0]
        new['metadata'] = entry['metadata']
        recovered = self.prepare([new])[0]
        worker = self.worker(recovered)
        with patch('resumable_worker.requests.Session', return_value=self.session(worker, assets)):
            worker.run()
        self.assertFalse(worker.errors, worker.errors)
        self.assertEqual((self.folder / 'original.zip').read_bytes(), ZIP)
        self.assertFalse((self.folder / 'New title.zip').exists())


if __name__ == '__main__':
    unittest.main()
