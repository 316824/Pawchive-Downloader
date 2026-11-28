"""Offline real-window artist round trips, with native failures isolated in a child.

Fast regression: python -m unittest discover -s windows/tests -p test_artist_workflow_soak.py
Extended soak: python windows/tests/test_artist_workflow_soak.py --soak --output METRICS.json
Larger artist: add --first-artist-albums 2500 (valid range: 1 through 10000).

The extended run defaults to 1,704 valid PNGs over 12 artist queues, including
1,000 albums in the first queue (configurable), then closes/reopens the same INI/SQLite state
and resumes an interrupted stream. Only HTTP and retry delays are substituted;
the window, discovery, preparation, journal, download threads/signals and queue
rows are production objects. No site, task registration, tray or user state is used.
"""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
import faulthandler
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import weakref
import zlib

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSettings, QThread, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from requests.structures import CaseInsensitiveDict
from shiboken6 import isValid

from app_state import AppState
from desktop_runtime import DesktopRuntime
from download_journal import DownloadJournal
from download_scheduler import DownloadScheduler
import download_pawchive_images as desktop


# A real decodable 1x1 PNG, rather than a magic-prefix-only download fixture.
def png_chunk(kind, payload):
    return struct.pack('!I', len(payload))+kind+payload+struct.pack('!I', zlib.crc32(kind+payload))


PNG = (b'\x89PNG\r\n\x1a\n'+png_chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 2, 0, 0, 0))+
       png_chunk(b'IDAT', zlib.compress(b'\0\x33\x99\xcc'))+png_chunk(b'IEND', b''))
PNG_HASH = hashlib.sha256(PNG).hexdigest()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def drain(app):
    for _ in range(3):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def native_alive(refs):
    return sum(ref() is not None and isValid(ref()) for ref in refs)


def process_resources():
    """Use documented Win32 counters without an optional psutil dependency."""
    if os.name != 'nt':
        return {}
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    *[(name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                      'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                      'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    process = kernel.GetCurrentProcess()
    memory = Memory(); memory.cb = ctypes.sizeof(memory)
    handles = wintypes.DWORD()
    result = {}
    if psapi.GetProcessMemoryInfo(process, ctypes.byref(memory), memory.cb):
        result.update(rss_bytes=int(memory.WorkingSetSize), private_bytes=int(memory.PrivateUsage))
    if kernel.GetProcessHandleCount(process, ctypes.byref(handles)):
        result['handles'] = int(handles.value)
    return result


class Response:
    def __init__(self, body=b'', status=200, headers=None, gate=None):
        self.body = body; self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.encoding = 'utf-8'
        self.gate = gate

    @property
    def text(self): return self.body.decode('utf-8')
    @property
    def content(self): return self.body
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))
    def iter_content(self, chunk_size):
        if self.gate:
            yield self.body[:24]
            self.gate[0].set()
            if not self.gate[1].wait(20):
                raise TimeoutError('Offline pause gate was never released')
            yield self.body[24:]
        else:
            for offset in range(0, len(self.body), 17):
                yield self.body[offset:offset+17]
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class OfflineSite:
    """Deterministic profile pagination and malformed original-file responses."""
    def __init__(self):
        self.artists = {}; self.calls = Counter(); self.events = Counter()
        self.lock = threading.Lock(); self.pause_url = ''; self.pause_gate = None

    def add(self, identifier, albums, images=1):
        profile = 'https://pawchive.pw/fanbox/user/' + str(identifier)
        self.artists[profile] = (albums, images)
        return profile

    @staticmethod
    def file_url(identifier, album, image):
        key = hashlib.sha256(f'{identifier}/{album}/{image}'.encode()).hexdigest()
        return f'https://file.pawchive.pw/data/ab/cd/{key}.png?f={identifier}-{album}-{image}.png'

    def session(self):
        site = self
        class Session:
            def __init__(self): self.headers = {}
            def get(self, url, **kwargs): return site.get(url, **kwargs)
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        return Session()

    def get(self, url, **kwargs):
        parsed = urlparse(url)
        with self.lock:
            self.calls[url] += 1; attempt = self.calls[url]
        if parsed.netloc == 'file.pawchive.pw':
            offset = int(kwargs.get('headers', {}).get('Range', 'bytes=0-')[6:-1])
            headers = {'content-length': str(len(PNG)), 'content-type': 'image/png', 'etag': '"soak-png-v1"'}
            with self.lock:
                self.events['transfer_requests'] += 1
                if offset: self.events['range_requests'] += 1
            if url == self.pause_url and attempt == 1:
                return Response(PNG, headers=headers, gate=self.pause_gate)
            # Pick faults by image name, so every artist exercises recovery.
            filename = parse_qs(parsed.query)['f'][0]
            album, image = map(int, filename[:-4].rsplit('-', 2)[1:])
            fault = album % 3
            if image == 0 and album < 3 and attempt == 1 and url != self.pause_url:
                if fault == 0:
                    self.events['truncated_responses'] += 1
                    return Response(PNG[:24], headers=headers)
                if fault == 1:
                    self.events['html_responses'] += 1
                    return Response(b'<html>temporary original error</html>', headers={'content-type': 'text/html'})
                self.events['truncated_responses'] += 1
                return Response(PNG[:24], headers=headers)
            if offset:
                if image == 0 and fault == 2 and attempt == 2 and url != self.pause_url:
                    self.events['malformed_ranges'] += 1
                    return Response(PNG[offset:], 206, {**headers, 'content-length': str(len(PNG)-offset),
                        'content-range': f'bytes {offset+1}-{len(PNG)-1}/{len(PNG)}'})
                require(kwargs['headers'].get('If-Range') == '"soak-png-v1"', 'Missing durable range validator')
                headers.update({'content-length': str(len(PNG)-offset),
                    'content-range': f'bytes {offset}-{len(PNG)-1}/{len(PNG)}'})
                self.events['valid_ranges'] += 1
                return Response(PNG[offset:], 206, headers)
            return Response(PNG, headers=headers)
        require(parsed.netloc == 'pawchive.pw', 'Unexpected offline host: ' + url)
        clean = url.split('?', 1)[0].rstrip('/')
        if '/post/' in clean:
            profile, post = clean.rsplit('/post/', 1)
            albums, images = self.artists[profile]; album = int(post)
            require(0 <= album < albums, 'Unexpected album URL: ' + url)
            identifier = profile.rsplit('/', 1)[-1]
            links = ''.join('<a class="fileThumb" href="'+self.file_url(identifier, album, image)+
                '" download="image-'+str(image)+'.png">image</a>' for image in range(images))
            return Response(('<h1 class="post__title"><span>Album '+str(album)+
                '</span></h1><div class="post__files">'+links+'</div>').encode())
        require(clean in self.artists, 'Unexpected offline profile: ' + url)
        albums, images = self.artists[clean]
        offset = int(parse_qs(parsed.query).get('o', ['0'])[0])
        cards = ''.join('<article class="post-card"><a href="'+clean+'/post/'+str(index)+
            '">post</a><div class="post-card__header">Album '+str(index)+
            '</div><time datetime="2026-11-01T12:00:00Z"></time></article>'
            for index in range(offset, min(albums, offset+desktop.PAGE_SIZE)))
        return Response(('<div class="user-header"><span itemprop="name">Artist '+
            clean.rsplit('/', 1)[-1]+'</span></div>'+cards).encode())


class ThreadCounter(QObject):
    def __init__(self): super().__init__(); self.finished = 0
    @Slot()
    def completed(self): self.finished += 1


class WorkflowHarness:
    def __init__(self, root, app, site):
        self.root = Path(root); self.app = app; self.site = site
        self.samples = []; self.worker_refs = []; self.scanner_refs = []; self.scheduler_refs = []
        self.row_refs = []; self.counter = ThreadCounter(); self.late_signal_probes = 0
        self.max_reserved_transfer_slots = 0; self.open()

    def open(self):
        self.state = AppState(self.root/'state')
        self.journal = DownloadJournal(self.root/'state')
        self.runtime = DesktopRuntime(self.app, self.state, self.journal, tray_enabled=False)
        settings = QSettings(str(self.root/'preferences.ini'), QSettings.Format.IniFormat)
        self.window = desktop.App(settings=settings, services=self.runtime)
        self.runtime.attach(self.window); self.runtime.timer.stop(); self.runtime.clipboard_timer.stop()
        self.window.update_controller.startup_timer.stop()
        self.window.path.setText(str(self.root/'downloads'))
        (self.root/'downloads').mkdir(exist_ok=True)
        self.window.select_posts_check.setChecked(False)
        self.window.artist_folder_check.setChecked(True)
        self.window.title_check.setChecked(True)
        self.window.rename_check.setChecked(True)
        self.window._next_record_artist = False
        original_factory = self.window._queue_worker_factory
        def factory(task):
            # The scheduler reserves a native slot before calling its factory.
            # Count that reservation, including this not-yet-started worker,
            # rather than confusing total worker creations with concurrency.
            scheduler = self.window.queue_scheduler
            reserved = len(scheduler._workers)
            self.max_reserved_transfer_slots = max(self.max_reserved_transfer_slots, reserved)
            require(reserved <= scheduler.MAX_IMAGE_WORKERS, 'Download concurrency exceeded the image slot limit')
            previous = getattr(self, '_previous_scheduler', None)
            if previous is not None and isValid(previous) and previous is not self.window.queue_scheduler:
                require(previous.signalsBlocked(), 'Retired artist signals are still enabled')
                # Probe while deleteLater is pending: stale artist A progress,
                # completion and idle signals must not change artist B's UI.
                if previous.tasks:
                    stale = previous.tasks[0].public_copy()
                    stale.state = 'running'; stale.label = 'STALE ARTIST A'; stale.current = 76543
                    stale.total = 98765; stale.completed_files = 12345
                    before = (self.window.status.text(), self.window._queue_completed_files,
                              self.window.file_name.text(), self.window.workflow_state, self.window.busy)
                    previous.task_updated.emit(stale)
                    stale.state = 'completed'; previous.task_finished.emit(stale)
                    previous.became_idle.emit(); previous.queue_changed.emit()
                    after = (self.window.status.text(), self.window._queue_completed_files,
                             self.window.file_name.text(), self.window.workflow_state, self.window.busy)
                    require(before == after, 'Stale artist A signals mutated artist B')
                    self.late_signal_probes += 1
                self._previous_scheduler = None
            worker = original_factory(task)
            self.worker_refs.append(weakref.ref(worker))
            worker.finished.connect(self.counter.completed)
            return worker
        self.window._queue_worker_factory = factory

    def pump_until(self, condition, timeout=90, label='workflow'):
        deadline = time.monotonic()+timeout
        while not condition():
            self.app.processEvents()
            splash = getattr(self.window, 'splash', None)
            if splash is not None and isValid(splash) and splash._completion_callback is not None:
                splash.finish_completion()
            if time.monotonic() > deadline:
                tasks = self.window.queue_scheduler.tasks if self.window.queue_scheduler else ()
                raise AssertionError(f'{label} timeout: busy={self.window.busy}, workflow={self.window.workflow_state}, '
                    f'tasks={Counter(t.state for t in tasks)}, log={self.window.log.toPlainText()[-1200:]}')
            time.sleep(.001)
        drain(self.app)

    def settled(self):
        w = self.window; scheduler = w.queue_scheduler
        return (scheduler is not None and not w.busy and not scheduler._workers and not scheduler.active_tasks
                and not getattr(w, '_queue_pending_entries', ()) and not w._queue_finalizing
                and not any(getattr(w, name, None) for name in
                    ('_queue_preparation', '_queue_finalizer', '_manifest_worker', '_destination_worker')))

    def snapshot(self, phase, artist):
        drain(self.app); gc.collect(); drain(self.app)
        result = dict(phase=phase, artist=artist, transfer_threads_created=len(self.worker_refs),
            transfer_threads_finished=self.counter.finished, native_transfer_workers=native_alive(self.worker_refs),
            native_scanners=native_alive(self.scanner_refs), native_schedulers=native_alive(self.scheduler_refs),
            native_old_rows=native_alive(self.row_refs),
            child_schedulers=len(self.window.findChildren(DownloadScheduler)),
            child_scan_dialogs=len(self.window.findChildren(desktop.ScanAnimation)),
            running_child_threads=sum(thread.isRunning() for thread in self.window.findChildren(QThread)),
            rows=len(self.window.download_queue._rows), **process_resources())
        self.samples.append(result)
        return result

    def begin_artist(self, profile):
        previous = self.window.queue_scheduler
        self._previous_scheduler = previous
        previous_rows = list(self.window.download_queue._rows.values())
        if previous is not None: self.scheduler_refs.append(weakref.ref(previous))
        self.row_refs.extend(weakref.ref(row) for row in previous_rows)
        self.window.path.setText(str(self.root/'downloads'))
        self.window.profile.setText(profile); self.window._next_record_artist = False
        self.window.download()
        scanner = getattr(self.window, 'scanner', None)
        require(isinstance(scanner, desktop.ProfileScanWorker), 'Real profile scan did not start')
        self.scanner_refs.append(weakref.ref(scanner))
        return previous

    def artist(self, profile, albums, images=1):
        started = time.monotonic(); before = len(self.worker_refs)
        previous = self.begin_artist(profile)
        self.pump_until(self.settled, timeout=max(90, albums*.5), label=profile)
        self.window._flush_queue_view()
        self.pump_until(lambda:not self.window.download_queue._pending_items, timeout=45, label='queue rows')
        w = self.window; tasks = w.queue_scheduler.tasks
        require(w.workflow_state == 'complete', 'Artist failed: '+w.log.toPlainText()[-1500:])
        require(len(tasks) == albums and all(t.state == 'completed' for t in tasks), 'Missing completed albums')
        require(w.files_saved == albums*images, f'Wrong displayed saved count: {w.files_saved}')
        require(len(self.worker_refs)-before == albums, 'Retry replaced an album unexpectedly')
        require(len(w.download_queue._rows) == albums, 'Real queue rows were not populated')
        require(all(row.item_id in {t.id for t in tasks} for row in w.download_queue._rows.values()), 'Stale row IDs')
        folder = Path(w.completed_path)
        paths = list(folder.rglob('*.png'))
        require(len(paths) == albums*images, f'Wrong file count under {folder}: {len(paths)}')
        require(all(hashlib.sha256(path.read_bytes()).hexdigest() == PNG_HASH for path in paths), 'Corrupt or appended PNG')
        for album in range(albums):
            require({p.name for p in (folder/('Album '+str(album))).glob('*.png')} ==
                    {str(i)+'.png' for i in range(1, images+1)}, 'Non-contiguous static image numbers')
        require(not list(folder.rglob('*.part')), 'Completed artist retained a partial')
        for task in tasks:
            require(self.journal.load(task.metadata['job_id'])['status'] == 'completed', 'Unsettled checkpoint')
        sample = self.snapshot('settled', profile)
        sample.update(albums=albums, files=albums*images, duration_seconds=round(time.monotonic()-started, 3))
        require(sample['native_schedulers'] == 0, 'Previous artist scheduler native object survived')
        require(sample['native_old_rows'] == 0, 'Previous artist queue rows survived')
        require(sample['native_scanners'] == 0, 'Finished profile scanner native object survived')
        require(sample['native_transfer_workers'] == 0, 'Finished download worker native object survived')
        require(sample['child_schedulers'] == 1, 'Old scheduler retained as App child')
        require(sample['child_scan_dialogs'] == 0, 'Closed scan dialog retained as App child')
        require(sample['running_child_threads'] == 0, 'Artist settled with a running child QThread')
        if previous is not None:
            require(not isValid(previous), 'Previous scheduler was not deleteLater-disposed')
        return sample

    def close(self):
        self.runtime.prepare_exit()
        self.window.close()
        self.pump_until(lambda:not self.window.isVisible() and not self.window._workspace_has_live_threads(),
                        timeout=30, label='close')
        self.runtime.close(); self.window.deleteLater(); self.runtime.deleteLater(); drain(self.app)
        gc.collect(); drain(self.app)

    def pause_and_restart(self):
        profile = self.site.add(999999, 1, 2)
        self.site.pause_url = self.site.file_url('999999', 0, 0)
        self.site.pause_gate = (threading.Event(), threading.Event())
        self.begin_artist(profile)
        self.pump_until(self.site.pause_gate[0].is_set, label='stream partial')
        # prepare_exit follows the actual exit policy: preserve active queue
        # partials, request interruption, and wait for native finished signals.
        self.runtime.prepare_exit(); self.window.close(); self.site.pause_gate[1].set()
        self.pump_until(lambda:not self.window._workspace_has_live_threads() and not self.window.busy,
                        label='paused native worker teardown')
        jobs = self.journal.list_recoverable()
        require(len(jobs) == 1 and jobs[0]['status'] == 'paused', 'Interrupted queue was not durably paused')
        part = Path(jobs[0]['files'][0]['part'])
        require(part.read_bytes() == PNG[:24], 'Unexpected durable stream offset')
        old_window = weakref.ref(self.window)
        self.close(); self.open()
        require(old_window() is None or not isValid(old_window()), 'Previous App native object survived restart')
        jobs = self.journal.list_recoverable()
        require(len(jobs) == 1, 'Paused journal missing after reopening SQLite')
        before_ranges = self.site.events['valid_ranges']
        require(self.window.resume_recoverable_jobs(jobs), 'Actual resume scan did not start')
        self.scanner_refs.append(weakref.ref(self.window._resume_scan_worker))
        self.pump_until(self.settled, label='restart recovery')
        require(self.window.workflow_state == 'complete', 'Recovery failed: '+self.window.log.toPlainText())
        require(self.site.events['valid_ranges'] > before_ranges, 'Restart did not resume with HTTP Range')
        require(not self.journal.list_recoverable(), 'Recovered queue still marked recoverable')
        files = list((self.root/'downloads'/'Artist 999999').rglob('*.png'))
        require(len(files) == 2 and all(path.read_bytes() == PNG for path in files), 'Restart corrupted PNG files')
        require(not part.exists(), 'Recovered partial was not removed')
        sample = self.snapshot('resumed_after_restart', profile)
        require(sample['native_scanners'] == 0 and sample['child_scan_dialogs'] == 0,
                'Recovery scan or its closed dialog survived queue construction')


def artist_album_count(value):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise argparse.ArgumentTypeError('The first artist must contain 1 through 10000 albums.') from None
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError('The first artist must contain 1 through 10000 albums.')
    return number


def run_workflows(output, extended=False, first_artist_albums=1000):
    first_artist_albums = artist_album_count(first_artist_albums)
    faulthandler.enable(all_threads=True)
    if os.name == 'nt': ctypes.windll.kernel32.SetErrorMode(0x8003)
    app = QApplication.instance() or QApplication([]); app.setQuitOnLastWindowClosed(False)
    image = QImage.fromData(PNG)
    require(not image.isNull(), 'PNG fixture does not decode')
    site = OfflineSite(); started = time.monotonic()
    metrics = dict(mode='extended' if extended else 'regression', offline=True, success=False)
    original_wait = desktop.Worker.wait_interruptibly
    # Accelerate only backoff clocks; retain production interruption checks.
    def fast_wait(worker, seconds): return original_wait(worker, min(seconds, .001))
    with tempfile.TemporaryDirectory(prefix='pawchive_artist_soak_') as folder, \
         patch('requests.Session', side_effect=site.session), \
         patch('requests.sessions.Session.request', side_effect=AssertionError('Real network forbidden')) as network, \
         patch.object(desktop.Worker, 'wait_interruptibly', fast_wait):
        harness = WorkflowHarness(folder, app, site)
        try:
            sizes = [first_artist_albums]+[64]*11 if extended else [8]*10
            for index, count in enumerate(sizes):
                profile = site.add(880000+index, count, 1 if extended else 2)
                sample = harness.artist(profile, count, 1 if extended else 2)
                print(json.dumps(sample, ensure_ascii=False), flush=True)
            harness.pause_and_restart()
            profile = site.add(990001, 7, 2)
            harness.artist(profile, 7, 2)
            metrics.update(success=True, artist_batches=len(sizes)+2,
                completed_albums=sum(sizes)+1+7, verified_png_files=sum(sizes)*(1 if extended else 2)+2+14,
                native_transfer_threads=len(harness.worker_refs), samples=harness.samples,
                first_artist_albums=sizes[0],
                max_reserved_transfer_slots=harness.max_reserved_transfer_slots,
                configured_image_worker_limit=DownloadScheduler.MAX_IMAGE_WORKERS,
                late_artist_signal_probes=harness.late_signal_probes,
                fault_counts=dict(site.events), real_network_requests=network.call_count,
                duration_seconds=round(time.monotonic()-started, 3))
            require(harness.counter.finished == len(harness.worker_refs), 'Missing native QThread finished signal')
            require(network.call_count == 0, 'A production HTTP request escaped the offline session')
            require(site.events['malformed_ranges'] and site.events['html_responses'] and site.events['valid_ranges'],
                    'Fault injection paths were not all exercised')
        except BaseException as error:
            metrics.update(success=False, error=repr(error), samples=harness.samples, fault_counts=dict(site.events))
            raise
        finally:
            try:
                harness.close()
            except BaseException as error:
                metrics.update(success=False, close_error=repr(error))
                raise
            finally:
                output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding='utf-8')
    return metrics


class ArtistWorkflowSoakTests(unittest.TestCase):
    def test_actual_artist_roundtrips_and_partial_restart_in_native_safe_subprocess(self):
        with tempfile.TemporaryDirectory(prefix='pawchive_soak_child_') as folder:
            output = Path(folder)/'metrics.json'
            command = [sys.executable, '-X', 'faulthandler', str(Path(__file__).resolve()),
                       '--worker', '--output', str(output)]
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=180, env={**os.environ, 'QT_QPA_PLATFORM':'offscreen'})
            self.assertEqual(result.returncode, 0, result.stdout[-3000:]+'\n'+result.stderr[-5000:])
            data = json.loads(output.read_text(encoding='utf-8'))
            self.assertTrue(data['success']); self.assertGreaterEqual(data['artist_batches'], 10)
            self.assertEqual(data['real_network_requests'], 0)
            self.assertGreaterEqual(data['verified_png_files'], 160)
            self.assertGreater(data['fault_counts']['malformed_ranges'], 0)
            self.assertTrue(any(row['phase']=='resumed_after_restart' for row in data['samples']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--soak', action='store_true')
    parser.add_argument('--first-artist-albums', type=artist_album_count, default=1000,
                        help='Albums in the first artist of an extended soak (1-10000; default: 1000).')
    parser.add_argument('--output', default=str(Path(tempfile.gettempdir())/'pawchive-soak-workflows.json'))
    args, remaining = parser.parse_known_args()
    if args.worker or args.soak:
        run_workflows(args.output, args.soak, args.first_artist_albums)
    else:
        unittest.main(argv=[sys.argv[0], *remaining])
