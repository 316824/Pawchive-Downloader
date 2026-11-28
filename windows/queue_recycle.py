"""Serialized durable cancellation commits, kept away from the GUI thread."""
from PySide6.QtCore import QThread, Signal


class QueueRecycleWorker(QThread):
    """Persist lightweight snapshots before the GUI releases cancelled tasks.

    Requests contain display scalars and a journal ID only. Immutable download
    plans stay in their journal and are loaded in this worker, never copied into
    every queued cancellation. Interruption does not abandon accepted commits:
    the host waits for this owned thread before closing either SQLite store.
    """
    result = Signal(object)

    def __init__(self, recycle_bin, journal, requests, parent=None):
        super().__init__(parent)
        self.recycle_bin = recycle_bin
        self.journal = journal
        self.requests = list(requests)

    def run(self):
        committed, errors = [], []
        for request in self.requests:
            entry = None
            try:
                entry = self.recycle_bin.add(**request['entry'])
                job_id = request.get('job_id')
                if self.journal is not None and job_id:
                    job = self.journal.load(job_id)
                    job['status'] = 'recycled'
                    job['config']['recycle_entry_id'] = entry.id
                    self.journal.save(job, dirty_records=[])
                committed.append(dict(task_id=request['task_id'], url=request['url'],
                    counted_files=request.get('counted_files', 0)))
            except Exception as error:
                # A bin entry whose journal did not commit is not recoverable.
                # Keep the cancelled task in the queue so cancellation can retry.
                if entry is not None:
                    try:
                        self.recycle_bin.remove(entry.id)
                    except Exception:
                        pass
                errors.append(dict(task_id=request['task_id'], error=str(error)))
        self.requests.clear()
        self.result.emit(dict(committed=committed, errors=errors))
