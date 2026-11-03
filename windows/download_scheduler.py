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

from PySide6.QtCore import QObject, Signal


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
    else:
        title = str(getattr(value, 'title', '') or getattr(value, 'name', '') or '未命名图集')
        url = str(getattr(value, 'url', '') or getattr(value, 'post_url', '') or '')
        thumbnail = str(getattr(value, 'thumbnail', '') or getattr(value, 'image', '') or '')
        formats = tuple(str(item) for item in (getattr(value, 'formats', ()) or ()))
        direct = bool(getattr(value, 'direct', False)) or has_direct_attachment(formats)
        task_id = str(getattr(value, 'id', '') or uuid.uuid4().hex)
        metadata = dict(getattr(value, 'metadata', {}) or {})
    return DownloadTask(
        id=task_id, title=title, url=url, thumbnail=thumbnail,
        formats=formats, sequence=int(sequence), direct=direct,
        priority=int(sequence), metadata=metadata)


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

    def __init__(self, worker_factory: Callable[[DownloadTask], Any], parent: QObject | None = None):
        super().__init__(parent)
        self.worker_factory = worker_factory
        self._tasks: dict[str, DownloadTask] = {}
        self._workers: dict[str, Any] = {}
        self._started = False
        self._closing = False

    @property
    def tasks(self) -> tuple[DownloadTask, ...]:
        return tuple(self.ordered_tasks())

    @property
    def active_tasks(self) -> tuple[DownloadTask, ...]:
        return tuple(task for task in self._tasks.values() if task.is_active)

    def ordered_tasks(self) -> list[DownloadTask]:
        return sorted(self._tasks.values(), key=lambda task: (task.priority, task.sequence, task.id))

    def task(self, task_id: str) -> DownloadTask | None:
        return self._tasks.get(str(task_id))

    def add_task(self, value: DownloadTask | dict[str, Any] | Any) -> DownloadTask:
        task = normalize_task(value, len(self._tasks))
        if task.id in self._tasks:
            task.id = uuid.uuid4().hex
        task.priority = len(self._tasks)
        self._tasks[task.id] = task
        self.task_added.emit(task.public_copy())
        self.queue_changed.emit()
        if self._started:
            self._schedule()
        return task

    def add_tasks(self, values: Iterable[DownloadTask | dict[str, Any] | Any]) -> list[DownloadTask]:
        result = [self.add_task(value) for value in values]
        return result

    def start(self) -> None:
        self._started = True
        self._closing = False
        self._schedule()

    def stop(self) -> None:
        """Request interruption of every active worker and stop scheduling."""
        self._closing = True
        self._started = False
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

    def resume(self, task_id: str) -> bool:
        task = self.task(task_id)
        if task is None or task.state not in {'paused', 'failed'}:
            return False
        task.state = 'queued'
        task.intent = ''
        task.error = ''
        task.attempts += 1
        self._emit(task)
        self.queue_changed.emit()
        if self._started:
            self._schedule()
        return True

    def retry(self, task_id: str) -> bool:
        """Retry a failed task at the highest priority."""
        task = self.task(task_id)
        if task is None or task.state not in {'failed', 'paused'}:
            return False
        task.state = 'queued'
        task.intent = ''
        task.error = ''
        task.attempts += 1
        self.move_to_top((task.id,))
        if self._started:
            self._schedule()
        return True

    def cancel(self, task_id: str) -> bool:
        task = self.task(task_id)
        if task is None or task.state in {'completed', 'cancelled'}:
            return False
        if task.state == 'queued' or task.state in {'paused', 'failed'}:
            task.state = 'cancelled'
            task.intent = 'cancel'
            self._emit(task)
            self.task_finished.emit(task.public_copy())
            self.task_recycled.emit(task.public_copy())
            self.queue_changed.emit()
            if self._started:
                self._schedule_or_idle()
            return True
        if task.state in {'running', 'pausing', 'cancelling'}:
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
        return sum(1 for task_id in task_ids if self.cancel(task_id))

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
        before = next((task.id for task in self.ordered_tasks()
                       if task.id not in selected and task.state == 'queued'), None)
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
        if not self._started or self._closing:
            return
        active = [task for task in self._tasks.values() if task.is_active]
        if any(task.direct for task in active):
            return
        first_queued=next((task for task in self.ordered_tasks() if task.state=='queued'),None)
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
            if task.state != 'queued':
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
        self._emit(task)
        # Reserve the slot before constructing the worker so a factory error
        # follows the same failed-task path and still advances the queue.
        self._workers[task.id] = None
        try:
            worker = self.worker_factory(task.public_copy())
            task.worker = worker
            self._workers[task.id] = worker
            progress = getattr(worker, 'progress', None)
            if progress is not None:
                progress.connect(lambda label, current, total, task_id=task.id:
                                 self._on_progress(task_id, label, current, total))
            finished = getattr(worker, 'finished_ok', None)
            if finished is not None:
                finished.connect(lambda count, task_id=task.id:
                                 self._on_success(task_id, count))
            failed = getattr(worker, 'failed', None)
            if failed is not None:
                failed.connect(lambda error, task_id=task.id:
                               self._on_failure(task_id, error))
            worker.start()
        except Exception as exc:
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
        task.progress = min(1.0, task.current / task.total) if task.total else 0.0
        self._emit(task)

    def _on_success(self, task_id: str, count: Any) -> None:
        task = self.task(task_id)
        if task is None or task_id not in self._workers:
            return
        worker = self._workers.pop(task_id, None)
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
            self.task_recycled.emit(task.public_copy())
        elif intent == 'pause':
            task.state = 'paused'
            self._emit(task)
            self.task_finished.emit(task.public_copy())
        else:
            task.state = 'completed'
            task.progress = 1.0
            try:
                task.completed_files = max(0, int(count or 0))
            except (TypeError, ValueError):
                task.completed_files = 0
            self._emit(task)
            self.task_finished.emit(task.public_copy())
        self.queue_changed.emit()
        self._schedule_or_idle()

    def _on_failure(self, task_id: str, error: Any) -> None:
        task = self.task(task_id)
        if task is None or task_id not in self._workers:
            return
        worker=self._workers.pop(task_id, None)
        completed_records=getattr(worker,'completed_records',None)
        if completed_records is not None:
            try:
                task.metadata['completed_records']=list(completed_records)
            except TypeError:
                task.metadata['completed_records']=completed_records
        task.worker = None
        task.error = str(error or '下载失败')
        intent = task.intent
        if intent == 'cancel':
            task.state = 'cancelled'
            self._emit(task)
            self.task_finished.emit(task.public_copy())
            self.task_recycled.emit(task.public_copy())
        elif intent == 'pause':
            task.state = 'paused'
            task.error = ''
            self._emit(task)
            self.task_finished.emit(task.public_copy())
        else:
            # A failed album is deliberately left in a retryable state.  The
            # scheduler immediately advances to the next priority task.
            task.state = 'failed'
            self._emit(task)
            self.task_finished.emit(task.public_copy())
        self.queue_changed.emit()
        self._schedule_or_idle()

    def _schedule_or_idle(self) -> None:
        self._schedule()
        if self._started and not any(task.is_active or task.state == 'queued'
                                     for task in self._tasks.values()):
            self.became_idle.emit()


__all__ = [
    'DIRECT_EXTENSIONS', 'DownloadScheduler', 'DownloadTask',
    'IMAGE_EXTENSIONS', 'has_direct_attachment', 'normalize_task',
]
