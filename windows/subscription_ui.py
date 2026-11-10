"""Small, responsive subscription manager for the Windows client."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)

from subscriptions import SubscriptionCheckWorker, SubscriptionStore, normalize_profile


class SubscriptionDialog(QDialog):
    """Manage subscribed artists and show background update state.

    All list mutations are performed through ``SubscriptionStore``.  The
    checker only emits data and this dialog updates widgets on the GUI thread.
    """

    def __init__(self, parent, state_store):
        super().__init__(parent)
        self.parent_window = parent
        self.lang = getattr(parent, "lang", "简体中文")
        self.store = SubscriptionStore(state_store)
        self.worker = None
        self.setWindowTitle("画师更新订阅")
        self.resize(650, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        title = QLabel("画师更新订阅")
        title.setStyleSheet("font-size:21px;font-weight:600")
        layout.addWidget(title)
        self.info = QLabel("后台检查最多同时访问 3 位画师；新画集会显示红点提醒。")
        self.info.setWordWrap(True)
        self.info.setObjectName("muted")
        layout.addWidget(self.info)
        row = QHBoxLayout()
        self.profile = QLineEdit()
        self.profile.setPlaceholderText("输入 Pawchive 画师主页或数字 ID")
        add = QPushButton("添加")
        add.clicked.connect(self.add_profile)
        row.addWidget(self.profile, 1)
        row.addWidget(add)
        layout.addLayout(row)
        self.items = QListWidget()
        self.items.setSpacing(5)
        self.items.itemDoubleClicked.connect(self.open_artist)
        layout.addWidget(self.items, 1)
        controls = QHBoxLayout()
        self.refresh_button = QPushButton("检查更新")
        self.refresh_button.clicked.connect(self.check_updates)
        self.remove_button = QPushButton("删除选中")
        self.remove_button.clicked.connect(self.remove_selected)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.remove_button)
        controls.addStretch(1)
        controls.addWidget(close)
        layout.addLayout(controls)
        self.refresh()

    def refresh(self):
        self.items.clear()
        records = self.store.list()
        if not records:
            self.items.addItem("还没有订阅画师。添加一个主页后会在后台检查更新。")
            self.items.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
            return
        for record in records:
            status = "  ● 有更新" if record.get("unread") else ""
            name = record.get("name") or "Unknown Artist"
            stamp = record.get("latest_ts", 0.0)
            date_text = "未知"
            if stamp:
                date_text = datetime.fromtimestamp(stamp, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            item = QListWidgetItem(f"{name}  ·  ID {record.get('id','')}{status}\n最新记录：{date_text}")
            item.setData(Qt.ItemDataRole.UserRole, record.get("profile"))
            item.setToolTip(record.get("profile", ""))
            self.items.addItem(item)

    def add_profile(self):
        value = normalize_profile(self.profile.text())
        if not value:
            QMessageBox.warning(self, "无法添加", "请输入完整的 Pawchive 画师主页或数字 ID。")
            return
        try:
            self.store.add(value)
        except (ValueError, OSError, RuntimeError) as error:
            QMessageBox.warning(self, "无法添加", str(error))
            return
        self.profile.clear()
        self.refresh()
        self._notify_parent()
        # The first background check turns the temporary local record into a
        # real baseline (latest post timestamp, display name and avatar URL)
        # without producing a false unread dot.  Reuse the bounded checker so
        # adding a creator never blocks the dialog on network I/O.
        self.check_updates()

    def remove_selected(self):
        item = self.items.currentItem()
        profile = item.data(Qt.ItemDataRole.UserRole) if item else ""
        if not profile:
            return
        self.store.remove(profile)
        self.refresh()
        self._notify_parent()

    def open_artist(self, item):
        profile = item.data(Qt.ItemDataRole.UserRole)
        if not profile:
            return
        self.store.mark_read(profile)
        self.refresh()
        self._notify_parent()
        parent = self.parent_window
        if hasattr(parent, "handle_link_text"):
            parent.handle_link_text(profile, prompt_post=False)

    def check_updates(self):
        if self.worker is not None and self.worker.isRunning():
            return
        self.refresh_button.setEnabled(False)
        self.info.setText("正在后台检查订阅…")
        self.worker = SubscriptionCheckWorker(self.store, self)
        self.worker.progress.connect(lambda *_: self.refresh())
        self.worker.finished_all.connect(self._check_finished)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def _check_finished(self, results):
        self.refresh_button.setEnabled(True)
        errors = sum(1 for item in results if item.get("error"))
        self.info.setText("检查完成。" + (f" {errors} 位画师暂时无法访问。" if errors else ""))
        self.refresh()
        self._notify_parent()

    def _notify_parent(self):
        callback = getattr(self.parent_window, "refresh_subscription_badge", None)
        if callable(callback):
            callback()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2500)
        super().closeEvent(event)


__all__ = ["SubscriptionDialog"]
