"""Live failure view; every control acts on the main queue's scheduler."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
from desktop_design import DownloadQueueWidget
from ui_i18n import t, language_for


class FailedQueueDialog(QDialog):
    def __init__(self,app):
        super().__init__(app)
        self.app=app
        self.lang=language_for(app)
        self.setWindowTitle(t(self.lang,'下载失败的图集'))
        self.resize(700,540)
        layout=QVBoxLayout(self)
        self.summary=QLabel()
        layout.addWidget(self.summary)
        self.queue=DownloadQueueWidget(app.download_queue.progress_class,self)
        self.queue.actionRequested.connect(app.handle_queue_action)
        self.queue.set_language(app.lang)
        layout.addWidget(self.queue,1)
        row=QHBoxLayout()
        self.retry=QPushButton(t(self.lang,'全部重试'))
        self.retry.clicked.connect(app.retry_all_failed)
        row.addWidget(self.retry); row.addStretch(1)
        close=QPushButton(t(self.lang,'关闭')); close.clicked.connect(self.accept)
        row.addWidget(close); layout.addLayout(row)
        self.timer=QTimer(self); self.timer.setInterval(150)
        self.timer.timeout.connect(self.refresh); self.timer.start()
        self.finished.connect(self.timer.stop)
        self.refresh()

    def refresh(self):
        scheduler=self.app.queue_scheduler
        tasks=[t.public_copy() for t in scheduler.tasks
               if t.state=='failed' or t.metadata.get('failure_pinned')] if scheduler else []
        self.summary.setText(t(self.lang,'{count} 个失败或正在重试的图集 · 与主下载队列实时同步',count=len(tasks)))
        self.retry.setEnabled(any(t.state=='failed' for t in tasks))
        self.queue.set_items(tasks)
