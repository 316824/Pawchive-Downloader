"""Isolated native-thread regressions and an opt-in, reproducible scheduler soak.

The normal unittest test uses a small subprocess so a native Qt crash cannot
take down the test runner. Run the stronger offline workload explicitly::

    python windows/tests/test_scheduler_soak.py --soak --tasks 6000 --batches 40 \
        --sync-failures 2000 --report C:/TEMPNEW/pawchive-soak-threads.json

Workers emit real queued Qt signals but perform no network or file transfers.
"""
from __future__ import annotations

import argparse
import ctypes
import faulthandler
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import unittest
import weakref

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
faulthandler.enable()
if os.name == 'nt':
    # A native failure must fail the subprocess, never open a modal Windows dialog.
    ctypes.windll.kernel32.SetErrorMode(0x8003)

from PySide6.QtCore import (QCoreApplication, QEvent, QObject, QThread,
                            QTimer, Signal, Slot, qInstallMessageHandler)

from download_scheduler import DownloadScheduler


LARGE_CURRENT = (1 << 34) + 123
LARGE_TOTAL = (1 << 35) + 321


def process_metrics():
    """Use OS counters without an optional psutil dependency."""
    if os.name != 'nt':
        return {}

    class MemoryCounters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in (
                'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]

    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    handle = kernel.GetCurrentProcess()
    handles = ctypes.c_ulong()
    kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_ulong)]
    if not kernel.GetProcessHandleCount(handle, ctypes.byref(handles)):
        raise ctypes.WinError()
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = [ctypes.c_void_p, ctypes.POINTER(MemoryCounters), ctypes.c_ulong]
    if not get_memory(handle, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return {'handles': handles.value, 'rss_bytes': counters.WorkingSetSize,
            'peak_rss_bytes': counters.PeakWorkingSetSize}


def flush_deletes(app):
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


def settle_threads(app):
    # QThread.finished precedes the final platform TLS/handle teardown. Measure
    # the process after that cleanup, rather than mistaking it for a handle leak.
    deadline = time.monotonic() + .15
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.001)
    flush_deletes(app)


class NativeLedger:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = {}
        self.native_started = 0
        self.native_finished = 0
        self.maximum_active = 0
        self.direct_native_started = 0
        self.interrupted = 0
        self.start_failures = 0
        self.factory_failures = 0
        self.silent_finishes = 0
        self.failures_emitted = 0
        self.successes_emitted = 0
        self.terminal_while_native_running = 0
        self.errors = []
        self.worker_refs = []
        self.relay_refs = []
        self.scheduler_refs = []
        self.os_samples = []

    def enter(self, worker):
        with self.lock:
            if len(self.active) >= 5:
                self.errors.append('More than five native threads ran concurrently.')
            if self.active and (worker.direct or any(self.active.values())):
                self.errors.append('A direct attachment overlapped another native thread.')
            self.active[id(worker)] = worker.direct
            self.maximum_active = max(self.maximum_active, len(self.active))
            self.native_started += 1
            self.direct_native_started += int(worker.direct)

    def leave(self, worker):
        with self.lock:
            self.active.pop(id(worker), None)
            self.native_finished += 1


class NativeWorker(QThread):
    progress = Signal(str, object, object)
    file_progress = Signal(int, int)
    finished_ok = Signal(int)
    failed = Signal(str)

    def __init__(self, ledger, direct, mode, attempt):
        super().__init__()
        self.ledger, self.direct = ledger, direct
        self.mode, self.attempt = mode, attempt
        self.preserve_on_interrupt = False

    def start(self):
        if self.mode == 'start-fail' and self.attempt == 1:
            self.ledger.start_failures += 1
            raise RuntimeError('injected native thread start failure')
        super().start()

    def run(self):
        ledger = self.ledger
        ledger.enter(self)
        try:
            self.progress.emit('large-file.jpg', LARGE_CURRENT, LARGE_TOTAL)
            self.file_progress.emit(1, 3)
            if self.mode in {'pause', 'cancel'} and self.attempt == 1:
                # Let the GUI react to queued progress before the worker settles.
                for _ in range(500):
                    if self.isInterruptionRequested():
                        break
                    self.msleep(1)
            else:
                self.msleep(2)
            if self.isInterruptionRequested():
                with ledger.lock:
                    ledger.interrupted += 1
                    if not self.preserve_on_interrupt:
                        ledger.errors.append('Interruption discarded its resumable payload.')
                self.failed.emit('injected interruption')
            elif self.mode == 'silent' and self.attempt == 1:
                with ledger.lock:
                    ledger.silent_finishes += 1
            elif self.mode == 'fail' and self.attempt == 1:
                with ledger.lock:
                    ledger.failures_emitted += 1
                self.failed.emit('injected transient network failure')
            else:
                with ledger.lock:
                    ledger.successes_emitted += 1
                self.finished_ok.emit(3)
            # The terminal result is deliberately earlier than QThread.finished.
            # Its pool slot and Python wrapper must survive this interval.
            self.msleep(3)
        except BaseException:
            with ledger.lock:
                ledger.errors.append(traceback.format_exc())
            self.failed.emit('unexpected test-worker failure')
        finally:
            ledger.leave(self)


class ObservedScheduler(DownloadScheduler):
    def __init__(self, factory, ledger):
        super().__init__(factory)
        self.ledger = ledger
        self.AUTO_RETRY_LIMIT = 0

    def _on_success(self, task_id, count):
        worker = self._workers.get(task_id)
        if worker is not None and worker.isRunning():
            self.ledger.terminal_while_native_running += 1
        super()._on_success(task_id, count)

    def _on_failure(self, task_id, error):
        worker = self._workers.get(task_id)
        if worker is not None and worker.isRunning():
            self.ledger.terminal_while_native_running += 1
        super()._on_failure(task_id, error)

    def _release_after_finished(self, task_id):
        relay = self._relays.get(task_id)
        if relay is not None and task_id not in self._finished_signals:
            self.ledger.relay_refs.append(weakref.ref(relay))
        super()._release_after_finished(task_id)


class ArtistChain(QObject):
    """Begin artist B inside artist A's idle callback, before deferred deletion."""
    MODES = {7: 'fail', 11: 'pause', 19: 'cancel', 23: 'silent',
             31: 'start-fail', 37: 'factory-fail'}

    def __init__(self, ledger, total, batches):
        super().__init__()
        self.ledger, self.total, self.batches = ledger, total, batches
        self.scheduler = None
        self.batch = 0
        self.batch_counts = []
        self.terminal_counts = {'completed': 0, 'cancelled': 0, 'paused': 0, 'failed': 0}
        self.progress_signals = 0
        self.main_thread = QThread.currentThread()
        self.large_progress_ids = set()
        self.controlled = set()
        self.errors = []
        self.done = False

    def launch_next(self):
        if self.batch == self.batches:
            self.done = True
            return
        batch = self.batch
        self.batch += 1
        count = self.total // self.batches + int(batch < self.total % self.batches)
        attempts = {}
        ledger = self.ledger

        def factory(task):
            attempt = attempts[task.id] = attempts.get(task.id, 0) + 1
            mode = task.metadata['mode']
            if mode == 'factory-fail' and attempt == 1:
                ledger.factory_failures += 1
                raise RuntimeError('injected worker construction failure')
            worker = NativeWorker(ledger, task.direct, mode, attempt)
            ledger.worker_refs.append(weakref.ref(worker))
            return worker

        scheduler = ObservedScheduler(factory, ledger)
        self.scheduler = scheduler
        ledger.scheduler_refs.append(weakref.ref(scheduler))
        scheduler.task_updated.connect(self.updated)
        scheduler.task_finished.connect(self.terminal)
        scheduler.became_idle.connect(self.artist_done)
        scheduler.add_tasks({
            'id': f'artist-{batch}-album-{index}',
            'title': f'artist {batch}, album {index}',
            'url': f'offline://artist-{batch}/{index}',
            'formats': ['.zip'] if index % 17 == 0 else ['.jpg'],
            'metadata': {'mode': self.MODES.get(index % 61, 'success')},
        } for index in range(count))
        scheduler.start()

    @Slot(object)
    def updated(self, task):
        if QThread.currentThread() != self.main_thread:
            self.errors.append('task_updated arrived outside the main Qt thread.')
        if task.label != 'large-file.jpg':
            return
        self.progress_signals += 1
        if (task.current, task.total) != (LARGE_CURRENT, LARGE_TOTAL):
            self.errors.append('Progress integers were narrowed or corrupted.')
        self.large_progress_ids.add(task.id)
        if task.id in self.controlled:
            return
        mode = task.metadata['mode']
        if mode == 'pause':
            self.controlled.add(task.id)
            if not self.scheduler.pause(task.id) or not self.scheduler.resume(task.id):
                self.errors.append('Pause/resume before native finish was rejected.')
        elif mode == 'cancel':
            self.controlled.add(task.id)
            if not self.scheduler.cancel(task.id):
                self.errors.append('Native cancellation was rejected.')

    @Slot(object)
    def terminal(self, task):
        if QThread.currentThread() != self.main_thread:
            self.errors.append('task_finished arrived outside the main Qt thread.')
        self.terminal_counts[task.state] += 1
        if task.state == 'failed' and not self.scheduler.retry(task.id):
            self.errors.append('Retry before QThread.finished was rejected.')

    @Slot()
    def artist_done(self):
        try:
            old = self.scheduler
            if old._workers or old._relays or old._terminal_tasks or old._finished_signals:
                raise AssertionError('Artist became idle with native worker/relay ownership left.')
            for task in old.tasks:
                expected = 'cancelled' if task.metadata['mode'] == 'cancel' else 'completed'
                if task.state != expected:
                    raise AssertionError(f'{task.id}: expected {expected}, got {task.state}')
                if task.worker is not None:
                    raise AssertionError('A settled task still owns a QThread wrapper.')
            self.batch_counts.append(len(old.tasks))
            self.ledger.os_samples.append(process_metrics())
            old.stop()
            old.deleteLater()
            self.scheduler = None
            self.launch_next()
        except BaseException:
            self.errors.append(traceback.format_exc())
            self.done = True


def run_native(app, tasks, batches, timeout):
    ledger = NativeLedger()
    chain = ArtistChain(ledger, tasks, batches)
    chain.launch_next()
    deadline = time.monotonic() + timeout
    try:
        while not chain.done and time.monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(.0005)
        if not chain.done:
            raise AssertionError(f'Native thread soak exceeded {timeout} seconds.')
        if chain.errors or ledger.errors:
            raise AssertionError('\n'.join(chain.errors + ledger.errors))
        if sum(chain.batch_counts) != tasks or len(chain.batch_counts) != batches:
            raise AssertionError('Some consecutive artist batches were not completed.')
        if len(chain.large_progress_ids) != tasks:
            raise AssertionError('Not every selected task delivered its >int32 progress.')
        if ledger.native_started != ledger.native_finished or ledger.active:
            raise AssertionError('Native threads remained alive at the end of the artist chain.')
        if ledger.maximum_active != 5:
            raise AssertionError('The five-worker image pool was not exercised.')
        for name in ('start_failures', 'factory_failures', 'interrupted', 'silent_finishes',
                     'failures_emitted', 'terminal_while_native_running', 'direct_native_started'):
            if not getattr(ledger, name):
                raise AssertionError(f'The {name} path was not exercised.')
        result = {
            'selected_tasks': tasks, 'artist_batches': batches,
            'immediate_artist_handoffs': max(0, batches - 1),
            'native_threads_started': ledger.native_started,
            'native_threads_finished': ledger.native_finished,
            'maximum_concurrent_native_threads': ledger.maximum_active,
            'direct_native_threads_started': ledger.direct_native_started,
            'start_failures': ledger.start_failures,
            'factory_failures': ledger.factory_failures,
            'interruptions': ledger.interrupted,
            'silent_native_finishes': ledger.silent_finishes,
            'transient_failures': ledger.failures_emitted,
            'success_signals': ledger.successes_emitted,
            'terminal_callbacks_before_native_finish': ledger.terminal_while_native_running,
            'large_progress_values': [LARGE_CURRENT, LARGE_TOTAL],
            'tasks_with_large_progress': len(chain.large_progress_ids),
            'progress_updates': chain.progress_signals,
            'terminal_states_observed': chain.terminal_counts,
            'violations': chain.errors + ledger.errors,
            'batch_os_samples': ledger.os_samples,
        }
    finally:
        if chain.scheduler is not None:
            chain.scheduler.stop()
            end = time.monotonic() + 5
            while chain.scheduler._workers and time.monotonic() < end:
                app.processEvents()
                time.sleep(.001)
            # Test cleanup must itself never discard a running QThread.
            for worker in list(chain.scheduler._workers.values()):
                if worker is not None:
                    worker.wait(2000)
            chain.scheduler.deleteLater()
            chain.scheduler = None
        chain.deleteLater()
        chain = None
        flush_deletes(app)
    result['created_qthread_objects'] = len(ledger.worker_refs)
    result['released_qthread_objects'] = sum(ref() is None for ref in ledger.worker_refs)
    result['created_relays'] = len(ledger.relay_refs)
    result['live_worker_wrappers_after_deferred_delete'] = sum(ref() is not None for ref in ledger.worker_refs)
    result['live_relays_after_deferred_delete'] = sum(ref() is not None for ref in ledger.relay_refs)
    result['live_schedulers_after_deferred_delete'] = sum(ref() is not None for ref in ledger.scheduler_refs)
    for key in ('live_worker_wrappers_after_deferred_delete', 'live_relays_after_deferred_delete',
                'live_schedulers_after_deferred_delete'):
        if result[key]:
            raise AssertionError(f'{key}: {result[key]}')
    return result


class StartFailure(QObject):
    finished = Signal()
    finished_ok = Signal(int)
    failed = Signal(str)

    def start(self):
        raise RuntimeError('injected synchronous start failure')


def run_synchronous_failures(app, count, timeout):
    attempts = 0
    maximum_stack_depth = 0
    refs = []

    def factory(task):
        nonlocal attempts, maximum_stack_depth
        attempts += 1
        frame, depth = sys._getframe(), 0
        while frame is not None:
            depth += 1
            frame = frame.f_back
        maximum_stack_depth = max(maximum_stack_depth, depth)
        if attempts % 2:
            raise RuntimeError('injected synchronous factory failure')
        worker = StartFailure()
        refs.append(weakref.ref(worker))
        return worker

    scheduler = DownloadScheduler(factory)
    scheduler.AUTO_RETRY_LIMIT = 0
    scheduler.add_tasks({'id': f'failure-{index}', 'title': str(index),
                         'formats': ['.jpg']} for index in range(count))
    heartbeats = []
    heartbeat = QTimer()
    heartbeat.setInterval(1)
    heartbeat.timeout.connect(lambda: heartbeats.append(True))
    heartbeat.start()
    started = time.monotonic()
    scheduler.start()
    while (scheduler._workers or any(task.state == 'queued' for task in scheduler.tasks)
           or scheduler._schedule_timer.isActive()) and time.monotonic() - started < timeout:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    heartbeat.stop()
    heartbeat.deleteLater()
    if attempts != count or any(task.state != 'failed' for task in scheduler.tasks):
        raise AssertionError(f'Synchronous failure queue did not drain: {attempts}/{count}.')
    if maximum_stack_depth > 64:
        raise AssertionError(f'Synchronous failures grew the Python stack: {maximum_stack_depth}.')
    if not heartbeats:
        raise AssertionError('The synchronous failure queue starved the Qt event loop.')
    if scheduler._workers or scheduler._relays or scheduler._finished_signals:
        raise AssertionError('A failed start permanently retained a worker slot.')
    elapsed = time.monotonic() - started
    scheduler.stop()
    scheduler.deleteLater()
    scheduler = None
    flush_deletes(app)
    live = sum(ref() is not None for ref in refs)
    if live:
        raise AssertionError(f'{live} failed-start workers leaked.')
    return {'tasks': count, 'factory_failures': (count + 1) // 2,
            'start_failures': count // 2, 'maximum_python_stack_depth': maximum_stack_depth,
            'qt_heartbeat_ticks': len(heartbeats), 'elapsed_seconds': round(elapsed, 3),
            'live_worker_wrappers_after_deferred_delete': live}


def run_soak(tasks=6000, batches=40, sync_failures=2000, timeout=120):
    if tasks < batches or tasks // batches < 40:
        raise ValueError('Each artist batch needs at least 40 tasks to cover every injected mode.')
    app = QCoreApplication.instance() or QCoreApplication([])
    messages = []

    def qt_message(kind, context, message):
        messages.append(message)

    previous = qInstallMessageHandler(qt_message)
    started = time.monotonic()
    flush_deletes(app)
    cold_start = process_metrics()
    try:
        # Qt and CPython lazily allocate process-wide worker-thread resources.
        # Preserve the cold counter and compare the workload with a warmed one.
        warmup = run_native(app, 80, 2, min(timeout, 30))
        # The first nonzero Windows QTimer also initializes process-wide timer
        # handles. Warm that phase before comparing handle counts as well.
        timer_warmup = run_synchronous_failures(app, 40, min(timeout, 30))
        settle_threads(app)
        baseline = process_metrics()
        native_started = time.monotonic()
        native = run_native(app, tasks, batches, timeout)
        native['elapsed_seconds'] = round(time.monotonic() - native_started, 3)
        synchronous = run_synchronous_failures(app, sync_failures, timeout)
        settle_threads(app)
        final = process_metrics()
        if any('Destroyed while thread' in message for message in messages):
            raise AssertionError('Qt reported destruction of a running QThread.')
        samples = [baseline, final] + native['batch_os_samples']
        result = {
            'status': 'passed', 'python': sys.version, 'platform': sys.platform,
            'qt_platform': os.environ['QT_QPA_PLATFORM'],
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'warmup': warmup, 'timer_warmup': timer_warmup,
            'native': native, 'synchronous_failures': synchronous,
            'native_threads_started_including_warmup': warmup['native_threads_started'] + native['native_threads_started'],
            'process_cold_start': cold_start,
            'process_baseline': baseline, 'process_final': final,
            'maximum_sampled_handles': max((sample.get('handles', 0) for sample in samples), default=0),
            'peak_rss_bytes': max((sample.get('peak_rss_bytes', 0) for sample in samples), default=0),
            'handle_delta': final.get('handles', 0) - baseline.get('handles', 0),
            'qt_messages': messages,
        }
        return result
    finally:
        qInstallMessageHandler(previous)


class SchedulerSoakSubprocessTests(unittest.TestCase):
    def test_native_artist_handoffs_and_failure_queue_in_isolated_process(self):
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        completed = subprocess.run([
            sys.executable, str(Path(__file__).resolve()), '--soak', '--tasks', '240',
            '--batches', '6', '--sync-failures', '120', '--timeout', '45',
        ], env=env, capture_output=True, text=True, timeout=65)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result['status'], 'passed')
        self.assertGreaterEqual(result['native']['native_threads_started'], 240)
        self.assertEqual(result['native']['artist_batches'], 6)
        self.assertEqual(result['native']['maximum_concurrent_native_threads'], 5)
        self.assertEqual(result['synchronous_failures']['tasks'], 120)


if __name__ == '__main__':
    if '--soak' not in sys.argv:
        unittest.main()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--soak', action='store_true')
        parser.add_argument('--tasks', type=int, default=6000)
        parser.add_argument('--batches', type=int, default=40)
        parser.add_argument('--sync-failures', type=int, default=2000)
        parser.add_argument('--timeout', type=float, default=120)
        parser.add_argument('--report', type=Path)
        args = parser.parse_args()
        try:
            report = run_soak(args.tasks, args.batches, args.sync_failures, args.timeout)
        except BaseException:
            report = {'status': 'failed', 'error': traceback.format_exc()}
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(encoded + '\n', encoding='utf-8')
        print(encoded)
        sys.exit(0 if report['status'] == 'passed' else 1)
