"""Cancelled collection cards share the pre-download picker's layout and cache."""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

from post_picker import PostPicker
from ui_i18n import language_for


_COPY = {
    '简体中文': dict(title='下载回收站', restore='恢复选中', delete='删除选中', close='关闭',
        all='全选', none='全不选', cancelled='已取消', missing='封面暂不可用', top='回到顶部',
        count='回收站 · {total} 个图集', empty='回收站目前为空',
        hint='勾选已取消的图集后恢复下载；回收站内容会按保留期限自动清理。',
        unavailable='暂时无法读取回收站，请关闭窗口后重试。', busy='请先等待当前下载任务结束，再恢复回收站图集。',
        restore_failed='未能恢复所选图集，记录仍保留在回收站中。',
        batch_unavailable='当前版本暂不支持同时恢复多个图集，请先选择一个图集。',
        delete_failed='部分图集未能删除，已保留在回收站中。'),
    '繁體中文': dict(title='下載回收站', restore='恢復選取', delete='刪除選取', close='關閉',
        all='全選', none='全不選', cancelled='已取消', missing='封面暫不可用', top='回到頂端',
        count='回收站 · {total} 個圖集', empty='回收站目前為空',
        hint='勾選已取消的圖集後恢復下載；回收站內容會依保留期限自動清理。',
        unavailable='暫時無法讀取回收站，請關閉視窗後重試。', busy='請先等候目前下載工作結束，再恢復回收站圖集。',
        restore_failed='未能恢復選取的圖集，記錄仍保留在回收站中。',
        batch_unavailable='目前版本暫不支援同時恢復多個圖集，請先選取一個圖集。',
        delete_failed='部分圖集未能刪除，已保留在回收站中。'),
    'English': dict(title='Download recycle bin', restore='Restore selected', delete='Delete selected', close='Close',
        all='Select all', none='Select none', cancelled='Cancelled', missing='Cover unavailable', top='Back to top',
        count='Recycle bin · {total} collections', empty='The recycle bin is empty',
        hint='Select cancelled collections to restore downloads. Recycle-bin entries expire automatically.',
        unavailable='The recycle bin could not be read. Close this window and try again.',
        busy='Wait for the current download to finish before restoring collections.',
        restore_failed='The selected collections could not be restored. Their recycle-bin records were kept.',
        batch_unavailable='This version cannot restore multiple collections together. Select one collection first.',
        delete_failed='Some collections could not be deleted. Their recycle-bin records were kept.'),
    '日本語': dict(title='ダウンロードごみ箱', restore='選択を復元', delete='選択を削除', close='閉じる',
        all='すべて選択', none='選択解除', cancelled='キャンセル済み', missing='カバーを表示できません', top='先頭へ戻る',
        count='ごみ箱 · {total} 件', empty='ごみ箱は空です',
        hint='キャンセルしたコレクションを選んで復元します。ごみ箱の項目は保存期限後に自動削除されます。',
        unavailable='ごみ箱を読み込めません。ウィンドウを閉じて再試行してください。',
        busy='現在のダウンロードが完了してから復元してください。',
        restore_failed='選択した項目を復元できませんでした。ごみ箱の記録は保持されています。',
        batch_unavailable='このバージョンでは複数の項目を同時に復元できません。1 件を選択してください。',
        delete_failed='一部の項目を削除できませんでした。ごみ箱の記録は保持されています。'),
}


class RecycleBinDialog(PostPicker):
    """The existing cover picker with recycle actions and lightweight entries.

    The bin remains authoritative: restoring calls the app's queue preparation
    entry point and never consumes a record before the app has persisted it.
    """

    def __init__(self, app):
        self.app = app
        self.lang = language_for(app)
        self.recycle_copy = _COPY.get(self.lang, _COPY['简体中文'])
        self._action_in_progress = False
        self._read_error = False
        entries = self._read_entries()
        text = dict(choose_title=self.recycle_copy['title'], select_all=self.recycle_copy['all'],
            select_none=self.recycle_copy['none'], cover_missing=self.recycle_copy['missing'],
            no_direct=self.recycle_copy['cancelled'], cancel=self.recycle_copy['close'],
            back_top=self.recycle_copy['top'])
        checkbox_class = type(app.title_check) if isinstance(getattr(app, 'title_check', None), QCheckBox) else QCheckBox
        super().__init__(app, entries, checkbox_class, text)
        self.setObjectName('recyclePicker')
        # Each card is literally the same CoverCard as the pre-download picker.
        # Only its actions and badges change; selection and image bounds stay shared.
        self.copy = dict(self.copy, confirm=self.recycle_copy['restore'],
            count=self.recycle_copy['count'], empty=self.recycle_copy['empty'])
        self.setWindowTitle(self.recycle_copy['title'])
        self.heading.setText(self.recycle_copy['title'])
        self.summary.setText(self.recycle_copy['count'].format(total=len(self.titles)))
        self.empty.setText(self.recycle_copy['empty'])
        self.cancel_button.setText(self.recycle_copy['close'])
        self.confirm_button.setText(self.recycle_copy['restore'])
        self.confirm_button.clicked.disconnect()
        self.confirm_button.clicked.connect(self.restore_selected)
        self.delete_button = QPushButton(self.recycle_copy['delete'], self)
        self.delete_button.setAutoDefault(False)
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_button.clicked.connect(self.delete_selected)
        footer = self.layout().itemAt(self.layout().count() - 1).layout()
        footer.insertWidget(0, self.delete_button)
        self.notice = QLabel(self.recycle_copy['hint'], self)
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setStyleSheet('background:transparent;border:0;color:' + self.palette['muted'] + ';font-size:12px;')
        self.layout().insertWidget(self.layout().count() - 1, self.notice)
        self._select_all(False)
        if self._read_error:
            self.notice.setText(self.recycle_copy['unavailable'])
        self.layout().activate()

    def _read_entries(self):
        """Do not retain each entry's possibly large worker/manifest metadata."""
        recycle_bin = getattr(self.app, 'recycle_bin', None)
        if recycle_bin is None:
            return []
        try:
            entries = recycle_bin.list_entries()
            result = [dict(_recycle_id=entry.id, title=str(entry.title or ''),
                url=str(entry.post_url or entry.download_url or ''), image=str(entry.cover_path or ''),
                formats=[], content_mode='all') for entry in entries]
            self._read_error = False
            return result
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            self._read_error = True
            return []

    def _build_cards(self):
        if self._closed:
            return
        super()._build_cards()
        for card in self.cards:
            if card is None:
                continue
            card.content_choice.hide()
            card.formats.setText(self.recycle_copy['cancelled'])
            card.formats.setAccessibleName(self.recycle_copy['cancelled'])

    def _update_count(self):
        super()._update_count()
        selected = bool(self.selected) and not self._action_in_progress
        self.confirm_button.setEnabled(selected and not bool(getattr(self.app, 'busy', False))
            and not bool(getattr(self.app, '_mandatory_update', False))
            and not bool(getattr(self.app, '_update_installing', False)))
        if hasattr(self, 'delete_button'):
            self.delete_button.setEnabled(selected)

    def selected_ids(self):
        return [str(self.titles[index]['_recycle_id']) for index in sorted(self.selected)
                if 0 <= index < len(self.titles)]

    def restore_selected(self):
        if self._action_in_progress:
            return
        identifiers = self.selected_ids()
        if not identifiers:
            return
        if (getattr(self.app, 'busy', False) or getattr(self.app, '_mandatory_update', False)
                or getattr(self.app, '_update_installing', False)):
            self.notice.setText(self.recycle_copy['busy'])
            return
        restore = getattr(self.app, 'restore_recycle_entries', None)
        if not callable(restore):
            if len(identifiers) != 1:
                self.notice.setText(self.recycle_copy['batch_unavailable'])
                return
            restore_one = getattr(self.app, 'restore_recycle_entry', None)
            restore = (lambda ids: restore_one(ids[0])) if callable(restore_one) else None
        self._action_in_progress = True
        self._update_count()
        try:
            success = bool(restore(identifiers)) if callable(restore) else False
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            success = False
        finally:
            self._action_in_progress = False
        if success:
            self.accept()
        else:
            self.notice.setText(self.recycle_copy['restore_failed'])
            self._update_count()

    def delete_selected(self):
        if self._action_in_progress:
            return
        identifiers = self.selected_ids()
        if not identifiers:
            return
        self._action_in_progress = True
        self._update_count()
        failed = False
        try:
            for identifier in identifiers:
                try:
                    if not self.app.remove_recycle_entry(identifier):
                        failed = True
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    failed = True
            self._reload_entries()
            self.notice.setText(self.recycle_copy['delete_failed'] if failed else self.recycle_copy['hint'])
        finally:
            self._action_in_progress = False
            self._update_count()

    def _reload_entries(self):
        # A failed removal stays in the bin; cards are rebuilt in the same
        # bounded batches, and invisible cards never decode full-size images.
        self._build_timer.stop()
        self._layout_timer.stop()
        for card in self.cards:
            if card is not None:
                self.grid.removeWidget(card)
                card.hide()
                card.deleteLater()
        self.titles = self._read_entries()
        self.cards, self.checks = [None] * len(self.titles), [None] * len(self.titles)
        self.selected = set()
        self._positions = {}
        self._visible = []
        images = {entry['image'] for entry in self.titles}
        for path in list(self._cover_cache):
            if path not in images:
                self._cover_cache.pop(path, None)
        self.summary.setText(self.recycle_copy['count'].format(total=len(self.titles)))
        self._build_cards()
        self._update_count()
