"""Paged, lazy-loading artist subscriptions using the shared avatar cache."""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)

from artist_cards import ArtistAvatarLoader, visible_avatar_paths
from artist_ui_text import artist_text
from background_ui import install_background_host
from subscriptions import SubscriptionCheckWorker, SubscriptionStore, normalize_profile


class SubscriptionDialog(QDialog):
    """Only visible rows request avatars; network workers belong to the app."""
    PAGE_SIZE = 20

    def __init__(self, parent, state_store):
        super().__init__(parent)
        self.parent_window = parent
        self.lang = getattr(parent, 'lang', '简体中文')
        self.state_store = state_store
        self.store = SubscriptionStore(state_store)
        self.worker = None
        self._shared_worker = False
        self._page = 0
        self._phase = 0
        self._loading_profiles = set()
        install_background_host(self, getattr(parent, 'background_store', None), 'subscriptions')
        self.setWindowTitle(self.text('画师更新订阅'))
        self.resize(690, 590)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        title = QLabel(self.text('画师更新订阅'))
        title.setStyleSheet('font-size:21px;font-weight:600')
        layout.addWidget(title)
        self.info = QLabel(self.text('后台检查最多同时访问 3 位画师；新画集会显示红点提醒。'))
        self.info.setWordWrap(True)
        self.info.setObjectName('muted')
        layout.addWidget(self.info)
        row = QHBoxLayout()
        self.profile = QLineEdit()
        self.profile.setPlaceholderText(self.text('输入 Pawchive 画师主页或数字 ID'))
        self.add_button = QPushButton(self.text('添加'))
        self.add_button.clicked.connect(self.add_profile)
        row.addWidget(self.profile, 1)
        row.addWidget(self.add_button)
        layout.addLayout(row)
        self.items = QListWidget()
        self.items.setSpacing(5)
        self.items.setIconSize(QSize(64, 64))
        self.items.itemDoubleClicked.connect(self.open_artist)
        layout.addWidget(self.items, 1)
        pages = QHBoxLayout()
        self.prev_button = QPushButton(self.text('上一页'))
        self.next_button = QPushButton(self.text('下一页'))
        self.page_label = QLabel()
        self.prev_button.clicked.connect(lambda: self.change_page(-1))
        self.next_button.clicked.connect(lambda: self.change_page(1))
        pages.addStretch(1); pages.addWidget(self.prev_button)
        pages.addWidget(self.page_label); pages.addWidget(self.next_button)
        layout.addLayout(pages)
        controls = QHBoxLayout()
        self.refresh_button = QPushButton(self.text('检查更新'))
        self.refresh_button.clicked.connect(self.check_updates)
        self.remove_button = QPushButton(self.text('删除选中'))
        self.remove_button.clicked.connect(self.remove_selected)
        close = QPushButton(self.text('关闭'))
        close.clicked.connect(self.accept)
        controls.addWidget(self.refresh_button); controls.addWidget(self.remove_button)
        controls.addStretch(1); controls.addWidget(close)
        layout.addLayout(controls)
        self._avatar_loader = ArtistAvatarLoader(state_store, self.parent_window)
        self._avatar_loader.changed.connect(self._avatar_changed)
        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.timeout.connect(self._load_visible_avatars)
        self.items.verticalScrollBar().valueChanged.connect(self._schedule_visible)
        self.items.currentItemChanged.connect(lambda *_: self.remove_button.setEnabled(bool(self._selected_profile())))
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(250)
        self._status_timer.timeout.connect(self._poll_status)
        self._status_timer.start()
        self.finished.connect(self._stop_optional_work)
        self.refresh()

    def text(self, source, **values):
        return artist_text(self.lang, source, **values)

    def _selected_profile(self):
        item = self.items.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ''

    def _records(self):
        return sorted(self.store.list(), key=lambda r: (str(r.get('name', '')).casefold(), r['profile']))

    @staticmethod
    def _signature(records):
        return tuple((r['profile'], r.get('name'), r.get('unread'), r.get('latest_ts'), r.get('avatar_path')) for r in records)

    def _row_text(self, record):
        profile = record['profile']
        loading = self._loading_profiles | set(getattr(self.parent_window, '_subscription_loading_profiles', ()))
        marker = ('◐', '◓', '◑', '◒')[self._phase % 4] if profile in loading else '●' if record.get('unread') else ''
        status = self.text('正在检查画师更新…') if profile in loading else self.text('有更新') if record.get('unread') else ''
        stamp = record.get('latest_ts', 0.0)
        date = self.text('未知')
        if stamp:
            try:
                date = datetime.fromtimestamp(stamp, timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')
            except (ValueError, OverflowError, OSError):
                pass
        name = record.get('name') or 'Unknown Artist'
        return f"{name}  ·  ID {record.get('id', '')}  {marker} {status}\n" + self.text('最新记录：{date}', date=date)

    def refresh(self):
        selected = self._selected_profile()
        scroll = self.items.verticalScrollBar().value()
        records = self._records()
        self._data_signature = self._signature(records)
        maximum = max(0, (len(records)-1)//self.PAGE_SIZE)
        self._page = min(self._page, maximum)
        page = records[self._page*self.PAGE_SIZE:(self._page+1)*self.PAGE_SIZE]
        self._visible_records = {r['profile']: r for r in page}
        self.items.clear()
        self.prev_button.setEnabled(self._page > 0)
        self.next_button.setEnabled(self._page < maximum)
        self.page_label.setText(f'{self._page+1}/{maximum+1}' if records else '0/0')
        if not records:
            item = QListWidgetItem(self.text('还没有订阅画师。添加一个主页后会在后台检查更新。'))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.items.addItem(item)
        paths = visible_avatar_paths(self.state_store, self._visible_records)
        for record in page:
            profile = record['profile']
            item = QListWidgetItem(self._row_text(record))
            item.setData(Qt.ItemDataRole.UserRole, profile)
            item.setToolTip(profile)
            item.setSizeHint(QSize(0, 84))
            icon = self.parent_window.windowIcon()
            pixmap = QPixmap(paths.get(profile, '')) if paths.get(profile) else QPixmap()
            if not pixmap.isNull():
                icon = QIcon(pixmap.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            item.setIcon(icon)
            self.items.addItem(item)
            if profile == selected:
                self.items.setCurrentItem(item)
        self.items.verticalScrollBar().setValue(scroll)
        self.remove_button.setEnabled(bool(self._selected_profile()))
        self._schedule_visible()

    def _schedule_visible(self, *_):
        if self.isVisible():
            self._visible_timer.start(20)

    def _load_visible_avatars(self):
        if not self.isVisible():
            return
        viewport = self.items.viewport().rect()
        profiles = []
        for index in range(self.items.count()):
            item = self.items.item(index)
            profile = item.data(Qt.ItemDataRole.UserRole)
            if profile and self.items.visualItemRect(item).intersects(viewport):
                profiles.append(profile)
        self._avatar_loader.request(profiles)

    def _avatar_changed(self, *_):
        if self.isVisible():
            self.refresh()

    def _poll_status(self):
        if not self.isVisible():
            return
        self._phase += 1
        records = self._records()
        if self._signature(records) != self._data_signature:
            self.refresh()
            return
        for index in range(self.items.count()):
            item = self.items.item(index)
            record = self._visible_records.get(item.data(Qt.ItemDataRole.UserRole))
            if record:
                item.setText(self._row_text(record))

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_visible()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_visible_timer'):
            self._schedule_visible()

    def change_page(self, offset):
        self._page = max(0, self._page + int(offset))
        self.items.verticalScrollBar().setValue(0)
        self.refresh()

    def add_profile(self):
        value = normalize_profile(self.profile.text())
        if not value:
            QMessageBox.warning(self, self.text('无法添加'), self.text('请输入完整的 Pawchive 画师主页或数字 ID。'))
            return
        try:
            self.store.add(value)
        except (ValueError, OSError, RuntimeError) as error:
            QMessageBox.warning(self, self.text('无法添加'), str(error))
            return
        self.profile.clear(); self.refresh(); self._notify_parent(); self.check_updates()

    def remove_selected(self):
        profile = self._selected_profile()
        if not profile:
            return
        self.store.remove(profile)
        self._loading_profiles.discard(profile)
        self.refresh(); self._notify_parent()

    def open_artist(self, item):
        profile = item.data(Qt.ItemDataRole.UserRole)
        if not profile:
            return
        callback = getattr(self.parent_window, 'open_artist_profile', None)
        if callable(callback):
            self.accept(); callback(profile)
        elif hasattr(self.parent_window, 'handle_link_text'):
            self.parent_window.handle_link_text(profile, prompt_post=False)

    def check_updates(self):
        from shiboken6 import isValid
        if self.worker is not None and isValid(self.worker) and self.worker.isRunning():
            return
        self.refresh_button.setEnabled(False)
        self.info.setText(self.text('正在后台检查订阅…'))
        starter = getattr(self.parent_window, 'start_subscription_check', None)
        if callable(starter):
            # Reuse the application's bounded feed worker. Starting another
            # three-way checker here would duplicate an in-flight startup check.
            starter()
            self.worker = getattr(self.parent_window, '_subscription_worker', None)
            self._shared_worker = True
            if self.worker is None or not isValid(self.worker):
                self.refresh_button.setEnabled(True)
                self.info.setText(self.text('检查完成。'))
                return
            self._loading_profiles = set(getattr(self.parent_window, '_subscription_loading_profiles', ()))
        else:
            self._shared_worker = False
            self._loading_profiles = {record['profile'] for record in self.store.list()}
            self.worker = SubscriptionCheckWorker(self.store, self.parent_window)
        self.worker.progress.connect(self._refresh_progress)
        self.worker.finished_all.connect(self._check_finished)
        self.worker.finished.connect(self._worker_stopped)
        if not self._shared_worker:
            self.worker.finished.connect(self.worker.deleteLater)
            self.worker.start()

    def _check_finished(self, results):
        self._loading_profiles.clear()
        errors = sum(1 for item in results if item.get('error'))
        self.info.setText(self.text('检查完成。') + (' ' + self.text('{count} 位画师暂时无法访问。', count=errors) if errors else ''))
        self.refresh(); self._notify_parent()

    def _worker_stopped(self):
        if self.worker is self.sender():
            self.worker = None
        self.refresh_button.setEnabled(True)

    def _refresh_progress(self, profile, *_):
        self._loading_profiles.discard(profile)
        self.refresh()

    def _stop_optional_work(self, *_):
        from shiboken6 import isValid
        self._visible_timer.stop(); self._status_timer.stop(); self._avatar_loader.close()
        if not self._shared_worker and self.worker is not None and isValid(self.worker) and self.worker.isRunning():
            self.worker.stop()

    def _notify_parent(self):
        callback = getattr(self.parent_window, 'refresh_subscription_badge', None)
        if callable(callback):
            callback()

    def closeEvent(self, event):
        self._stop_optional_work()
        super().closeEvent(event)


__all__ = ['SubscriptionDialog']
