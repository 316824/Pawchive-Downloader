"""Priority-aware album scheduling for the Windows downloader.

The existing network worker is intentionally kept focused on downloading one
selection.  This module owns the queue around those workers: it preserves the
user's original selection order for later numbering, allows priority changes,
and limits concurrency according to the content type of each album.  A task
whose metadata contains a non-image attachment is treated as a direct task and
never runs alongside another task.  Image-only tasks may use up to five
workers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
import uuid
import weakref

from PySide6.QtCore import QObject, Signal, Slot, QTimer


IMAGE_EXTENSIONS = frozenset({
    '.avif', '.bmp', '.gif', '.heic', '.jpeg', '.jpg', '.png', '.tif',
    '.tiff', '.webp',
})
DIRECT_EXTENSIONS = frozenset({
    '.7z', '.clip', '.gz', '.mkv', '.mov', '.mp3', '.mp4', '.pdf', '.psd',
    '.rar', '.tar', '.tar.bz2', '.tar.gz', '.tar.xz', '.webm', '.zip',
})


def _extension(value: Any) -> str:
    value = str(value or '').strip().lower()
    if not value:
        return ''
    if not value.startswith('.'):
        value = '.' + value
    return value


def has_direct_attachment(formats: Iterable[Any]) -> bool:
    """Return whether *formats* includes a non-image attachment.

    The scanner supplies extensions such as ``.zip`` and ``.png``.  Unknown
    extensions are conservatively treated as direct attachments because they
    may be archives, videos, or other files that should stay serial.
    """
    values=tuple(formats or ())
    # A scan that has not yet learned the attachment suffix is deliberately
    # serialized.  It may turn out to be an archive or video, and opening a
    # five-way pool before that is known is the unsafe choice.
    if not values:
        return True
    for value in values:
        ext = _extension(value)
        if ext and ext not in IMAGE_EXTENSIONS:
            return True
    return False


@dataclass
class DownloadTask:
    """One selected album/post in the priority queue."""

    id: str
    title: str
    url: str
    thumbnail: str = ''
    formats: tuple[str, ...] = ()
    sequence: int = 0
    direct: bool = False
    priority: int = 0
    state: str = 'queued'
    progress: float = 0.0
    current: int = 0
    total: int = 0
    label: str = ''
    error: str = ''
    attempts: int = 0
    completed_files: int = 0
    files_current: int = 0
    files_total: int = 0
    file_bytes_current: int = 0
    file_bytes_total: int = 0
    intent: str = ''
    worker: Any = field(default=None, repr=False, compare=False)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_image_only(self) -> bool:
        return not self.direct

    @property
    def is_active(self) -> bool:
        return self.state in {'running', 'pausing', 'cancelling'}

    def public_copy(self) -> 'DownloadTask':
        """Return a worker-free snapshot suitable for a Qt signal/UI."""
        values = dict(self.__dict__)
        values.pop('worker', None)
        values['metadata'] = dict(self.metadata)
        return DownloadTask(**values)


def normalize_task(value: DownloadTask | dict[str, Any] | Any, sequence: int) -> DownloadTask:
    """Normalize scanner/PostPicker data into a :class:`DownloadTask`."""
    if isinstance(value, DownloadTask):
        task = value
        task.sequence = int(sequence if task.sequence is None else task.sequence)
        if not task.id:
            task.id = uuid.uuid4().hex
        task.formats = tuple(str(item) for item in task.formats or ())
        task.direct = bool(task.direct or has_direct_attachment(task.formats))
        task.priority = int(sequence)
        task.state = task.state or 'queued'
        return task
    if isinstance(value, dict):
        get = value.get
        title = str(get('title') or get('name') or '未命名图集')
        url = str(get('url') or get('post_url') or '')
        thumbnail = str(get('thumbnail') or get('image') or '')
        formats = tuple(str(item) for item in (get('formats') or ()))
        direct = bool(get('direct')) or has_direct_attachment(formats)
        task_id = str(get('id') or uuid.uuid4().hex)
        metadata = dict(get('metadata') or {})
        expected_files = get('file_count', get('expected_files', metadata.get('file_count', 0)))
    else:
        title = str(getattr(value, 'title', '') or getattr(value, 'name', '') or '未命名图集')
        url = str(getattr(value, 'url', '') or getattr(value, 'post_url', '') or '')
        thumbnail = str(getattr(value, 'thumbnail', '') or getattr(value, 'image', '') or '')
        formats = tuple(str(item) for item in (getattr(value, 'formats', ()) or ()))
        direct = bool(getattr(value, 'direct', False)) or has_direct_attachment(formats)
        task_id = str(getattr(value, 'id', '') or uuid.uuid4().hex)
        metadata = dict(getattr(value, 'metadata', {}) or {})
        expected_files = getattr(value, 'file_count', getattr(value, 'expected_files', metadata.get('file_count', 0)))
    try:
        expected_files = max(0, int(expected_files or 0))
    except (TypeError, ValueError):
        expected_files = 0
    return DownloadTask(
        id=task_id, title=title, url=url, thumbnail=thumbnail,
        formats=formats, sequence=int(sequence), direct=direct,
        priority=int(sequence), files_total=expected_files, metadata=metadata)


class _WorkerRelay(QObject):
    """Deliver worker signals to the scheduler's GUI thread, never a lambda's thread."""
    def __init__(self, scheduler, task_id, serial):
        super().__init__(scheduler)
        # Qt already owns this relay through its parent. A Python strong
        # reference back to that parent creates a mixed ownership cycle:
        # deferred relay destruction can drop the parent's final reference,
        # whose destructor then deletes the same child a second time.
        self._scheduler_ref = weakref.ref(scheduler)
        self.task_id, self.serial = task_id, serial

    def live_scheduler(self):
        scheduler = self._scheduler_ref()
        return scheduler if scheduler is not None and scheduler._run_serial.get(self.task_id) == self.serial else None

    @Slot(str, object, object)
    def progress(self, label, current, total):
        scheduler = self.live_scheduler()
        if scheduler is not None: scheduler._on_progress(self.task_id, label, current, total)

    @Slot(int, int)
    def file_progress(self, current, total):
        scheduler = self.live_scheduler()
        if scheduler is not None: scheduler._on_file_progress(self.task_id, current, total)

    @Slot(int)
    def success(self, count):
        scheduler = self.live_scheduler()
        if scheduler is not None: scheduler._on_success(self.task_id, count)

    @Slot(str)
    def failure(self, error):
        scheduler = self.live_scheduler()
        if scheduler is not None: scheduler._on_failure(self.task_id, error)

    @Slot()
    def finished(self):
        scheduler = self.live_scheduler()
        if scheduler is not None: scheduler._worker_finished(self.task_id)


class DownloadScheduler(QObject):
    """Run album workers in priority order with safe concurrency limits.

    ``worker_factory`` receives a :class:`DownloadTask` and must return an
    object exposing the existing worker signals ``progress``, ``finished_ok``
    and ``failed``, plus ``start`` and ``requestInterruption`` methods.  The
    scheduler never calls ``wait`` from a signal handler, so workers can emit
    their terminal signal from a QThread without deadlocking the GUI thread.
    """

    task_added = Signal(object)
    task_updated = Signal(object)
    task_finished = Signal(object)
    task_recycled = Signal(object)
    queue_changed = Signal()
    became_idle = Signal()

    MAX_IMAGE_WORKERS = 5
    # A failed album remains visible at the head of the queue while the
    # remaining albums continue. Once the rest of the queue is drained we
    # give it a few automatic recovery attempts before requiring user input.
    AUTO_RETRY_LIMIT = 3
    AUTO_RETRY_DELAY_MS = 5000

    def __init__(self, worker_factory: Callable[[DownloadTask], Any], parent: QObject | None = None):
        super().__init__(parent)
        self.worker_factory = worker_factory
        self._tasks: dict[str, DownloadTask] = {}
        self._workers: dict[str, Any] = {}
        # A QThread emits the worker's terminal signal just before its
        # ``run`` method returns. Keep the Python wrapper alive until Qt emits
        # ``finished``; dropping the last reference in the terminal callback
        # can destroy a running QThread and crash during download teardown.
        self._terminal_tasks: set[str] = set()
        self._finished_signals: set[str] = set()
        self._started = False
        self._closing = False
        self._suspended = False
        self._all_paused = False
        self._resume_pending: set[str] = set()
        self._run_serial: dict[str, int] = {}
        self._next_run_serial = 0
        self._next_sequence = 0
        self._relays: dict[str, _WorkerRelay] = {}
        self._retry_task_id = None
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._run_auto_retry)
        self._scheduling = False
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setSingleShot(True)
        self._schedule_timer.timeout.connect(self._schedule_or_idle)

    @property
    def tasks(self) -> tuple[DownloadTask, ...]:
        return tuple(self.ordered_tasks())

    @property
    def active_tasks(self) -> tuple[DownloadTask, ...]:
        return tuple(task for task in self._tasks.values() if task.is_active)

    def ordered_tasks(self) -> list[DownloadTask]:
        return sorted(self._tasks.values(), key=lambda task: (
            not (task.metadata.get('failure_pinned', False) and task.state not in {'completed', 'cancelled'}),
            task.priority, task.sequence, task.id))

    def task(self, task_id: str) -> DownloadTask | None:
        return self._tasks.get(str(task_id))

    def add_task(self, value: DownloadTask | dict[str, Any] | Any) -> DownloadTask:
        task = normalize_task(value, self._next_sequence)
        self._next_sequence += 1
        if task.id in self._tasks:
            task.id = uuid.uuid4().hex
        task.priority = self._next_sequence-1
        self._tasks[task.id] = task
        self.task_added.emit(task.public_copy())
        self.queue_changed.emit()
        if self._started:
            self._schedule()
        return task

    def add_tasks(self, values: Iterable[DownloadTask | dict[str, Any] | Any]) -> list[DownloadTask]:
        # Bulk insertion must not emit a full queue refresh or start a worker
        # for each row. The host can populate hundreds of albums in one pass.
        result = []
        for value in values:
            task = normalize_task(value, self._next_sequence)
            self._next_sequence += 1
            if task.id in self._tasks: task.id = uuid.uuid4().hex
            task.priority = self._next_sequence-1
            self._tasks[task.id] = task
            result.append(task)
            self.task_added.emit(task.public_copy())
        if result: self.queue_changed.emit()
        if self._started: self._schedule()
        return result

    def start(self) -> None:
        self._all_paused = False
        self._started = True
        self._closing = False
        self._schedule()

    def stop(self) -> None:
        """Request interruption of every active worker and stop scheduling."""
        self._closing = True
        self._started = False
        self._cancel_auto_retry()
        self._schedule_timer.stop()
        self._resume_pending.clear()
        for task in self.active_tasks:
            task.intent = 'cancel'
            task.state = 'cancelling'
            self._emit(task)
            worker = self._workers.get(task.id)
            if worker is not None:
                try:
                    # Keep the interrupted album/checkpoint available for the
                    # recycle bin instead of deleting its partial payload.
                    worker.preserve_on_interrupt = True
                except (AttributeError, RuntimeError):
                    pass
                try:
                    worker.requestInterruption()
                except (AttributeError, RuntimeError):
                    pass

    def pause(self, task_id: str) -> bool:
        task = self.task(task_id)
        if task is None or task.state in {'completed', 'cancelled'}:
            return False
        self._resume_pending.discard(task.id)
        if task.state == 'queued':
            task.state = 'paused'
            task.intent = ''
            self._emit(task)
            self.queue_changed.emit()
            if self._started:
                self._schedule_or_idle()
            return True
        if task.state in {'running', 'pausing'}:
            task.intent = 'pause'
            task.state = 'pausing'
            self._emit(task)
            worker = self._workers.get(task.id)
            if worker is not None:
                try:
                    worker.preserve_on_interrupt = True
                except (AttributeError, RuntimeError):
                    pass
                try:
                    worker.requestInterruption()
                except (AttributeError, RuntimeError):
                    pass
            return True
        return False

    @property
    def has_paused_tasks(self) -> bool:
        return any(task.state in {'paused', 'pausing'} for task in self._tasks.values())

    def pause_all(self) -> int:
        """Pause active and waiting work without starting replacements between rows."""
        self._all_paused = True
        self._suspended = True
        self._cancel_auto_retry()
        self._resume_pending.clear()
        try:
            count = sum(1 for task in list(self._tasks.values())
                        if task.state in {'queued', 'running', 'pausing'} and self.pause(task.id))
        finally:
            self._suspended = False
        self.queue_changed.emit()
        return count

    def resume_all(self) -> int:
        self._all_paused = False
        self._suspended = True
        try:
            count = sum(1 for task in list(self._tasks.values())
                        if task.state in {'paused', 'pausing'} and self.resume(task.id))
        finally:
            self._suspended = False
        self.queue_changed.emit()
        if self._started: self._schedule()
        return count

    def resume(self, task_id: str) -> bool:
        task = self.task(task_id)
        if task is None or task.state not in {'paused', 'pausing', 'failed'}:
            return False
        self._all_paused = False
        if task.state == 'pausing':
            # requestInterruption cannot be undone on a QThread. Remember the
            # click and restart from its checkpoint after that native thread exits.
            self._resume_pending.add(task.id)
            return True
        if self._retry_task_id == task.id: self._cancel_auto_retry()
        task.state = 'queued'
        task.intent = ''
        task.error = ''
        task.attempts += 1
        self._emit(task)
        self.queue_changed.emit()
        if self._started: self._schedule()
        return True

    def retry(self, task_id: str) -> bool:
        """Retry without dropping the failure pin until the task really succeeds."""
        task = self.task(task_id)
        if task is None or task.state not in {'failed', 'paused'}:
            return False
        self._all_paused = False
        if self._retry_task_id == task.id: self._cancel_auto_retry()
        task.metadata['auto_retry_count'] = 0
        task.metadata.pop('auto_retry_pending', None)
        task.state = 'queued'
        task.intent = ''
        task.error = ''
        task.attempts += 1
        self.move_to_top((task.id,))
        self._emit(task)
        self.queue_changed.emit()
        if self._started: self._schedule()
        return True

    def retry_all_failed(self) -> int:
        self._suspended = True
        try:
            count = sum(1 for task in list(self._tasks.values())
                        if task.state == 'failed' and self.retry(task.id))
        finally:
            self._suspended = False
        if self._started: self._schedule()
        return count

    def cancel(self, task_id: str) -> bool:
        task = self.task(task_id)
        if task is None:
            return False
        # A failed recycle-bin write must leave a retryable queue entry. Never
        # drop its only in-memory copy before the host acknowledges persistence.
        if task.state=='cancelled':
            if task.id not in self._workers:self.task_recycled.emit(task.public_copy())
            return True
        if self._retry_task_id == task.id: self._cancel_auto_retry()
        self._resume_pending.discard(task.id)
        if task.state in {'queued', 'paused', 'failed', 'completed'}:
            task.state = 'cancelled'
            task.intent = 'cancel'
            self._emit(task)
            self.task_finished.emit(task.public_copy())
            if task.id not in self._workers:self.task_recycled.emit(task.public_copy())
            else:self._release_after_finished(task.id)
            self.queue_changed.emit()
            if self._started:
                self._schedule_or_idle()
            return True
        if task.state in {'running', 'pausing', 'cancelling', 'completed', 'paused', 'failed'}:
            task.intent = 'cancel'
            task.state = 'cancelling'
            self._emit(task)
            worker = self._workers.get(task.id)
            if worker is not None:
                try:
                    # Preserve the interrupted payload so the host can move
                    # it into the recycle bin after the worker settles.
                    worker.preserve_on_interrupt = True
                except (AttributeError, RuntimeError):
                    pass
                try:
                    worker.requestInterruption()
                except (AttributeError, RuntimeError):
                    pass
            return True
        return False

    def cancel_many(self, task_ids: Iterable[str]) -> int:
        was_suspended=self._suspended
        self._suspended=True
        try:return sum(1 for task_id in dict.fromkeys(task_ids) if self.cancel(task_id))
        finally:
            self._suspended=was_suspended
            if self._started:self._schedule_or_idle()

    def remove_cancelled(self, task_id: str) -> bool:
        """Release a cancelled task only after its bin record is durable.

        Native workers retain their queue slot until ``finished``; even a
        terminal signal is too early to destroy their task or file manifests.
        The host calls this acknowledgement from ``task_recycled`` after its
        persistent recycle-bin add succeeds. Failure leaves the task intact.
        """
        task=self.task(task_id)
        if task is None or task.state!='cancelled' or task.id in self._workers:return False
        self._tasks.pop(task.id,None)
        self._resume_pending.discard(task.id)
        self._terminal_tasks.discard(task.id);self._finished_signals.discard(task.id)
        self._run_serial.pop(task.id,None)
        task.worker=None;task.metadata.clear();task.thumbnail=''
        self.queue_changed.emit()
        return True

    def reorder(self, task_ids: Iterable[str], before_id: str | None = None) -> bool:
        """Move selected queued tasks before ``before_id``.

        Running tasks remain untouched.  ``sequence`` is never changed, so a
        later global renamer can still use the user's original selection order.
        """
        selected = [str(task_id) for task_id in task_ids]
        selected = [task_id for task_id in selected if self.task(task_id) is not None]
        if not selected:
            return False
        movable = [task_id for task_id in selected if self.task(task_id).state == 'queued']
        if not movable:
            return False
        current = [task.id for task in self.ordered_tasks()]
        current = [task_id for task_id in current if task_id not in movable]
        if before_id is None or str(before_id) not in current:
            index = len(current)
        else:
            index = current.index(str(before_id))
        current[index:index] = movable
        self._reindex(current)
        self.queue_changed.emit()
        if self._started:
            self._schedule()
        return True

    def set_order(self, ordered_ids: Iterable[str]) -> bool:
        """Apply a complete UI order while preserving unknown/missing items.

        Queue widgets usually emit the whole visible order after a drag.  The
        scheduler accepts that list directly, keeps running tasks in place,
        and appends any IDs omitted by the widget in their previous order.
        Original ``sequence`` values remain unchanged for file numbering.
        """
        requested = []
        seen = set()
        for value in ordered_ids:
            task_id = str(value)
            if task_id in self._tasks and task_id not in seen:
                requested.append(task_id)
                seen.add(task_id)
        existing = [task.id for task in self.ordered_tasks() if task.id not in requested]
        new_order = requested + existing
        if [task.id for task in self.ordered_tasks()] == new_order:
            return False
        self._reindex(new_order)
        self.queue_changed.emit()
        if self._started:
            self._schedule()
        return True

    def move_to_top(self, task_ids: Iterable[str]) -> bool:
        selected = {str(task_id) for task_id in task_ids}
        # Priority order is visible even while another task is running. The
        # running worker remains untouched, but a retried task must become the
        # first queued priority entry instead of being inserted after it.
        before = next((task.id for task in self.ordered_tasks()
                       if task.id not in selected), None)
        return self.reorder(selected, before)

    def selection_order(self) -> list[DownloadTask]:
        """Tasks in immutable original selection order for final renaming."""
        return sorted(self._tasks.values(), key=lambda task: (task.sequence, task.id))

    def summary(self) -> list[dict[str, Any]]:
        return [self._as_dict(task) for task in self.ordered_tasks()]

    def _as_dict(self, task: DownloadTask) -> dict[str, Any]:
        values = dict(task.__dict__)
        values.pop('worker', None)
        values['formats'] = list(task.formats)
        values['metadata'] = dict(task.metadata)
        return values

    def _first_queued_id(self) -> str | None:
        for task in self.ordered_tasks():
            if task.state == 'queued':
                return task.id
        return None

    def _reindex(self, ordered_ids: list[str]) -> None:
        for priority, task_id in enumerate(ordered_ids):
            task = self._tasks[task_id]
            if task.priority != priority:
                task.priority = priority
                self._emit(task)

    def _emit(self, task: DownloadTask) -> None:
        self.task_updated.emit(task.public_copy())

    def _schedule(self) -> None:
        if self._scheduling:
            # A factory/start error (or a synchronous worker) can finish in
            # _start_task itself. Advancing recursively over a large artist's
            # queue exhausts the Python stack; let the event loop advance the
            # next bounded batch instead.
            if not self._schedule_timer.isActive(): self._schedule_timer.start(0)
            return
        self._scheduling = True
        try:
            self._schedule_pass()
        finally:
            self._scheduling = False

    def _schedule_pass(self) -> None:
        if not self._started or self._closing or self._suspended or self._all_paused:
            return
        # Terminal callbacks can arrive before QThread.finished. These native
        # threads still occupy their slots until they have fully stopped.
        active = [self._tasks[task_id] for task_id in self._workers if task_id in self._tasks]
        if any(task.direct for task in active):
            return
        first_queued=next((task for task in self.ordered_tasks() if task.state=='queued' and task.id not in self._workers),None)
        if first_queued is not None and first_queued.direct:
            # Direct links use a single global slot and block the image pool.
            if not active:
                self._start_task(first_queued)
            return
        image_active = len(active)
        capacity = self.MAX_IMAGE_WORKERS - image_active
        if capacity <= 0:
            return
        for task in self.ordered_tasks():
            if task.state != 'queued' or task.id in self._workers:
                continue
            if task.direct:
                # A direct task cannot run alongside already-started images;
                # it will be considered again once all active images settle.
                break
            if capacity <= 0:
                break
            self._start_task(task)
            capacity -= 1

    def _start_task(self, task: DownloadTask) -> None:
        task.state = 'running'
        task.intent = ''
        task.error = ''
        task.label = ''
        self._emit(task)
        # Reserve the slot before constructing the worker so a factory error
        # follows the same failed-task path and still advances the queue.
        self._workers[task.id] = None
        try:
            worker = self.worker_factory(task.public_copy())
            task.worker = worker
            self._workers[task.id] = worker
            self._next_run_serial += 1
            serial = self._next_run_serial
            self._run_serial[task.id] = serial
            relay = _WorkerRelay(self, task.id, serial)
            self._relays[task.id] = relay
            finished_signal = getattr(worker, 'finished', None)
            if finished_signal is not None and hasattr(finished_signal, 'connect'):
                self._finished_signals.add(task.id)
                finished_signal.connect(relay.finished)
            for name, slot in (('progress', relay.progress), ('file_progress', relay.file_progress),
                               ('finished_ok', relay.success), ('failed', relay.failure)):
                signal = getattr(worker, name, None)
                if signal is not None: signal.connect(slot)
            worker.start()
        except Exception as exc:
            # A worker whose start() raised before the native thread launched
            # cannot emit finished. Retaining that impossible expectation
            # would permanently occupy its pool slot and block the queue.
            worker = self._workers.get(task.id)
            try:
                running = bool(getattr(worker, 'isRunning', lambda: False)())
            except (RuntimeError, TypeError):
                running = False
            if not running: self._finished_signals.discard(task.id)
            self._on_failure(task.id, str(exc))

    def _on_progress(self, task_id: str, label: str, current: Any, total: Any) -> None:
        task = self.task(task_id)
        if task is None or task.state not in {'running', 'pausing', 'cancelling'}:
            return
        task.label = str(label or '')
        try:
            task.current = max(0, int(current or 0))
        except (TypeError, ValueError):
            task.current = 0
        try:
            task.total = max(0, int(total or 0))
        except (TypeError, ValueError):
            task.total = 0
        task.file_bytes_current = task.current
        task.file_bytes_total = task.total
        if task.files_total:
            fraction = task.current / task.total if task.total else 0.0
            task.progress = min(1.0, (task.files_current + fraction) / task.files_total)
        else:
            task.progress = min(1.0, task.current / task.total) if task.total else 0.0
        self._emit(task)

    def _on_file_progress(self, task_id: str, current: Any, total: Any) -> None:
        task = self.task(task_id)
        if task is None or task.state not in {'running', 'pausing', 'cancelling'}:
            return
        try:
            task.files_current = max(0, int(current or 0))
        except (TypeError, ValueError):
            task.files_current = 0
        try:
            task.files_total = max(0, int(total or 0))
        except (TypeError, ValueError):
            task.files_total = 0
        task.progress = min(1.0, task.files_current / task.files_total) if task.files_total else 0.0
        self._emit(task)

    def _on_success(self, task_id: str, count: Any) -> None:
        task = self.task(task_id)
        if task is None or task_id not in self._workers or task_id in self._terminal_tasks:
            return
        worker = self._workers.get(task_id)
        self._terminal_tasks.add(task_id)
        completed_records = getattr(worker, 'completed_records', None)
        # The production Worker keeps its durable records in DownloadJournal
        # rather than duplicating them on the QThread object.  Read the
        # completed records at the terminal signal so the UI can perform the
        # final stable selection-order rename after priority reordering.
        if completed_records is None and worker is not None:
            journal = getattr(worker, 'journal', None)
            job_id = getattr(worker, 'job_id', None)
            if journal is not None and job_id:
                try:
                    job = journal.load(job_id)
                    completed_records = [dict(record) for record in job.get('files', ())
                                         if record.get('status') == 'complete']
                except (KeyError, OSError, TypeError, ValueError):
                    completed_records = None
        if completed_records is not None:
            try:
                task.metadata['completed_records'] = list(completed_records)
            except TypeError:
                task.metadata['completed_records'] = completed_records
        task.worker = None
        intent = task.intent
        if intent == 'cancel':
            task.state = 'cancelled'
            self._emit(task)
            self.task_finished.emit(task.public_copy())
        elif intent == 'pause':
            task.state = 'paused'
            self._emit(task)
            self.task_finished.emit(task.public_copy())
        else:
            task.state = 'completed'
            task.metadata.pop('failure_pinned', None)
            task.metadata.pop('auto_retry_pending', None)
            task.progress = 1.0
            try:
                task.completed_files = max(0, int(count or 0))
            except (TypeError, ValueError):
                task.completed_files = 0
            task.files_current = task.completed_files
            task.files_total = max(task.files_total, task.completed_files)
            self._emit(task)
            self.task_finished.emit(task.public_copy())
        self.queue_changed.emit()
        self._release_after_finished(task_id)

    def _on_failure(self, task_id: str, error: Any) -> None:
        task = self.task(task_id)
        if task is None or task_id not in self._workers or task_id in self._terminal_tasks:
            return
        worker=self._workers.get(task_id)
        self._terminal_tasks.add(task_id)
        completed_records=getattr(worker,'completed_records',None)
        if completed_records is not None:
            try:
                task.metadata['completed_records']=list(completed_records)
            except TypeError:
                task.metadata['completed_records']=completed_records
        task.worker = None
        # The worker emits a file-specific failure line immediately before its
        # terminal signal. Keep that line in the queue row so the failed
        # filename remains visible when the item is promoted to the top.
        detail = str(task.label or '')
        failure_markers = ('下载失败', '下載失敗', 'Download failed', 'ダウンロード失敗')
        task.error = detail if any(marker in detail for marker in failure_markers) else str(error or '下载失败')
        intent = task.intent
        if intent == 'cancel':
            task.state = 'cancelled'
            self._emit(task)
            self.task_finished.emit(task.public_copy())
        elif intent == 'pause':
            task.state = 'paused'
            task.error = ''
            self._emit(task)
            self.task_finished.emit(task.public_copy())
        else:
            # A failed album is deliberately left in a retryable state.  The
            # scheduler immediately advances to the next priority task. Keep
            # the failed item at the visible top of the queue so its cover and
            # error text cannot be lost below a long list of pending albums.
            task.state = 'failed'
            task.metadata['failure_pinned'] = True
            self._move_failed_to_top(task.id)
            self._emit(task)
            self.task_finished.emit(task.public_copy())
        self.queue_changed.emit()
        self._release_after_finished(task_id)

    def _worker_finished(self, task_id: str) -> None:
        """Release a QThread wrapper only after its native thread stopped."""
        self._finished_signals.discard(task_id)
        if task_id not in self._terminal_tasks and task_id in self._workers:
            self._on_failure(task_id, 'Download worker stopped without a completion result.')
        self._release_after_finished(task_id)

    def _release_after_finished(self, task_id: str) -> None:
        if task_id not in self._terminal_tasks:
            return
        if task_id in self._finished_signals:
            # A real QThread has not necessarily stopped when finished_ok or
            # failed is delivered. Wait for its finished signal.
            if self.task(task_id) is not None and task_id in self._workers:
                return
        self._terminal_tasks.discard(task_id)
        self._finished_signals.discard(task_id)
        worker = self._workers.pop(task_id, None)
        relay = self._relays.pop(task_id, None)
        self._run_serial[task_id] = self._run_serial.get(task_id, 0) + 1
        if relay is not None: relay.deleteLater()
        if isinstance(worker, QObject): worker.deleteLater()
        task=self.task(task_id)
        if task is not None and task.state=='cancelled':
            # Move files/checkpoints only now: the HTTP worker has exited and
            # cannot still be writing into the album or its partial files.
            self.task_recycled.emit(task.public_copy())
        if task_id in self._resume_pending:
            self._resume_pending.discard(task_id)
            task = self.task(task_id)
            if task is not None and task.state == 'paused':
                task.state, task.intent, task.error = 'queued', '', ''
                task.attempts += 1
                self._emit(task)
                self.queue_changed.emit()
        self._schedule_or_idle()

    def _move_failed_to_top(self, task_id: str) -> None:
        """Promote a failed item without making it runnable immediately."""
        ordered = [task.id for task in self.ordered_tasks()]
        task_id = str(task_id)
        if task_id not in ordered:
            return
        if ordered[0] == task_id:
            return
        ordered.remove(task_id)
        ordered.insert(0, task_id)
        self._reindex(ordered)

    def _cancel_auto_retry(self) -> None:
        self._retry_timer.stop()
        task = self.task(self._retry_task_id) if self._retry_task_id else None
        if task is not None: task.metadata.pop('auto_retry_pending', None)
        self._retry_task_id = None

    def _auto_retry_failed(self) -> bool:
        """Back off after the rest of the queue drains; never tight-loop a blocked host."""
        if self._retry_timer.isActive(): return True
        if self._closing or self._suspended or self._all_paused: return False
        for task in self.ordered_tasks():
            if task.state != 'failed' or task.id in self._workers: continue
            try: count = max(0, int(task.metadata.get('auto_retry_count', 0) or 0))
            except (TypeError, ValueError): count = 0
            if count >= self.AUTO_RETRY_LIMIT: continue
            self._retry_task_id = task.id
            task.metadata['auto_retry_pending'] = True
            self._retry_timer.start(min(30000, self.AUTO_RETRY_DELAY_MS * (2 ** count)))
            self._emit(task)
            self.queue_changed.emit()
            return True
        return False

    @Slot()
    def _run_auto_retry(self) -> None:
        task = self.task(self._retry_task_id) if self._retry_task_id else None
        self._retry_task_id = None
        if task is None: return
        task.metadata.pop('auto_retry_pending', None)
        if not self._started or self._closing or self._suspended or self._all_paused or task.state != 'failed': return
        # Newly added/manual retry work takes precedence over an old timer.
        if self._workers or any(item.state == 'queued' for item in self._tasks.values()): return
        try: count = max(0, int(task.metadata.get('auto_retry_count', 0) or 0))
        except (TypeError, ValueError): count = 0
        task.metadata['auto_retry_count'] = count + 1
        task.state, task.intent, task.error = 'queued', '', ''
        task.current = task.total = 0
        task.attempts += 1
        self._emit(task)
        self.queue_changed.emit()
        # worker_factory always builds a fresh worker and HTTP session, while
        # the durable checkpoint keeps already verified files and safe ranges.
        self._schedule()

    def _schedule_or_idle(self) -> None:
        self._schedule()
        if self._scheduling or self._schedule_timer.isActive(): return
        if self._started and not self._closing and not self._suspended and not self._workers and not any(
                task.is_active or task.state == 'queued' for task in self._tasks.values()):
            if self._auto_retry_failed(): return
            self.became_idle.emit()


__all__ = [
    'DIRECT_EXTENSIONS', 'DownloadScheduler', 'DownloadTask',
    'IMAGE_EXTENSIONS', 'has_direct_attachment', 'normalize_task',
]
