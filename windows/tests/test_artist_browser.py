"""Real artist dialog entry points and shared lazy-avatar regressions."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QObject, QTimer, Signal, QSettings, QCoreApplication, QEvent, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from app_state import AppState
from artist_cards import (ArtistAvatarLoader, record_download, list_cards,
                          update_artist_metadata, cleanup_cache, visible_avatar_paths)
from artist_ui_text import artist_text
from background_store import normalize_page
from desktop_runtime import DesktopRuntime
from download_journal import DownloadJournal
from feature_dialogs import ArtistCardsDialog
from subscription_ui import SubscriptionDialog
from subscriptions import SubscriptionStore
import download_pawchive_images as desktop

PROFILE = 'https://pawchive.pw/fanbox/user/3439325'


class FakeAvatarWorker(QObject):
    result = Signal(str, str, str)
    finished = Signal()
    created = []

    def __init__(self, data_dir, profile, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.__class__.created.append(self)

    def start(self):
        pass


class NoTasks:
    def install(self, *_):
        raise AssertionError('No system task changes in UI tests')

    def remove(self, *_):
        raise AssertionError('No system task changes in UI tests')


class ArtistBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_artist_browser_')
        self.root = Path(self.temp.name)
        self.state = AppState(self.root/'state')
        self.runtime = DesktopRuntime(self.qt, self.state, DownloadJournal(self.state.data_dir), NoTasks(), tray_enabled=False)
        self.no_feed = patch.object(desktop.App, 'start_subscription_check', return_value=None)
        self.no_feed.start()
        self.no_network = patch('requests.sessions.Session.request', side_effect=AssertionError('No network in UI tests'))
        self.network = self.no_network.start()
        FakeAvatarWorker.created = []
        self.fake_worker = patch('artist_cards.AvatarFetchWorker', FakeAvatarWorker)
        self.fake_worker.start()
        self.window = desktop.App(QSettings(str(self.root/'settings.ini'), QSettings.Format.IniFormat), services=self.runtime)
        self.runtime.attach(self.window); self.runtime.timer.stop()
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject(); dialog.deleteLater()
        for worker in list(getattr(self.window, '_cabinet_avatar_workers', {}).values()):
            worker.finished.emit()
        self.runtime.prepare_exit(); self.window.busy = False
        self.window.close(); self.window.deleteLater(); self.runtime.close()
        self.qt.processEvents(); QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.fake_worker.stop(); self.no_feed.stop()
        self.network.assert_not_called(); self.no_network.stop()
        self.temp.cleanup()

    def wait(self, ms=50):
        end = time.monotonic() + ms/1000
        while time.monotonic() < end:
            self.qt.processEvents(); time.sleep(.002)

    def image(self, name='avatar.png'):
        folder = self.state.data_dir/'artist-cards'; folder.mkdir(exist_ok=True)
        path = folder/name
        image = QImage(64, 64, QImage.Format.Format_RGB32)
        image.fill(QColor('#44dd77')); self.assertTrue(image.save(str(path)))
        return str(path)

    def test_real_star_button_opens_empty_and_populated_cards_with_background_store(self):
        self.assertEqual(normalize_page('artist_cards'), 'artist_cards')
        self.assertEqual(normalize_page('subscriptions'), 'subscriptions')
        self.window.show(); self.wait(20)
        for count in (0, 1):
            if count:
                record_download(self.state, PROFILE, posts=1, files=2)
                self.window.refresh_reward_badge()
            seen = []
            timer = QTimer(self.window); timer.setSingleShot(True)

            def inspect():
                dialog = self.qt.activeModalWidget()
                if isinstance(dialog, ArtistCardsDialog):
                    seen.append(dialog.cards.count())
                    self.assertEqual(dialog._background_page, 'artist_cards')
                    dialog.reject()

            timer.timeout.connect(inspect); timer.start(30)
            self.window.artist_cards_button.click()
            timer.stop(); timer.deleteLater()
            self.assertEqual(seen, [count])
            self.wait(10)

    def test_subscription_avatar_loading_is_visible_only_paged_and_three_way(self):
        subscriptions = SubscriptionStore(self.state)
        for number in range(25):
            subscriptions.add(f'https://pawchive.pw/fanbox/user/{number}', name=f'Artist {number:02d}', baseline_ts=100)
        dialog = SubscriptionDialog(self.window, self.state); self.dialogs.append(dialog)
        self.assertFalse(FakeAvatarWorker.created, 'Hidden dialogs must not fetch avatars')
        dialog.show(); self.wait()
        self.assertEqual(dialog.items.count(), 20)
        self.assertGreater(len(FakeAvatarWorker.created), 0)
        self.assertLessEqual(len(FakeAvatarWorker.created), 3)
        first_wanted = set(dialog._avatar_loader._wanted)
        self.assertLess(len(first_wanted), 20, 'Only viewport rows should be requested')
        dialog.items.verticalScrollBar().setValue(dialog.items.verticalScrollBar().maximum())
        self.wait()
        self.assertNotEqual(dialog._avatar_loader._wanted, first_wanted)
        dialog.change_page(1); self.wait()
        self.assertEqual(dialog.items.count(), 5)
        page_profiles = set(dialog._visible_records)
        self.assertTrue(dialog._avatar_loader._wanted <= page_profiles)
        self.assertEqual(len(self.window._cabinet_avatar_workers), 3)
        FakeAvatarWorker.created[0].finished.emit(); self.wait()
        self.assertIn(FakeAvatarWorker.created[-1].profile, page_profiles)
        self.assertLessEqual(len(self.window._cabinet_avatar_workers), 3)
        self.assertFalse(list_cards(self.state), 'Subscriptions must not create achievement cards')

    def test_avatar_success_updates_existing_subscription_only_and_is_reused_after_reopen(self):
        subscriptions = SubscriptionStore(self.state)
        subscriptions.add(PROFILE, name='Artist A', baseline_ts=100)
        subscriptions.set_baseline(PROFILE, 200, unread=True)
        before = subscriptions.get(PROFILE)
        dialog = SubscriptionDialog(self.window, self.state); self.dialogs.append(dialog)
        dialog.show(); self.wait()
        worker = FakeAvatarWorker.created[0]
        avatar = self.image()
        worker.result.emit(PROFILE, avatar, ''); worker.finished.emit(); self.wait()
        after = subscriptions.get(PROFILE)
        self.assertEqual(after['avatar_path'], avatar)
        for key in ('baseline_ts', 'latest_ts', 'last_checked_ts', 'unread', 'enabled', 'initialized'):
            self.assertEqual(before[key], after[key], key)
        self.assertFalse(list_cards(self.state))
        self.assertEqual(visible_avatar_paths(self.state, [PROFILE]), {PROFILE: avatar})
        item = dialog.items.item(0)
        self.assertFalse(item.icon().isNull())
        self.assertGreater(item.icon().pixmap(32, 32).toImage().pixelColor(16, 16).green(), 150)
        dialog.reject(); self.wait()
        created = len(FakeAvatarWorker.created)
        reopened = SubscriptionDialog(self.window, self.state); self.dialogs.append(reopened)
        reopened.show(); self.wait()
        self.assertEqual(len(FakeAvatarWorker.created), created, 'A cached avatar should not refetch')

    def test_deleted_subscription_is_not_recreated_by_late_avatar(self):
        subscriptions = SubscriptionStore(self.state)
        subscriptions.add(PROFILE, baseline_ts=100)
        loader = ArtistAvatarLoader(self.state, self.window)
        loader.request([PROFILE])
        subscriptions.remove(PROFILE)
        worker = FakeAvatarWorker.created[0]
        worker.result.emit(PROFILE, self.image(), '')
        worker.finished.emit(); loader.close()
        self.assertIsNone(subscriptions.get(PROFILE))
        self.assertFalse(list_cards(self.state))

    def test_cleanup_keeps_subscription_avatar_but_removes_unused_avatar(self):
        avatar = self.image('subscription.png')
        orphan = self.image('orphan.png')
        subscriptions = SubscriptionStore(self.state)
        subscriptions.add(PROFILE, avatar_path=avatar, baseline_ts=100)
        self.assertEqual(cleanup_cache(self.state), 1)
        self.assertTrue(Path(avatar).is_file())
        self.assertFalse(Path(orphan).exists())

    def test_manual_subscription_check_reuses_main_worker_and_close_does_not_stop_it(self):
        class Checker(QObject):
            progress = Signal(str, object)
            finished_all = Signal(object)
            finished = Signal()
            stopped = False

            def isRunning(self): return True
            def stop(self): self.stopped = True

        checker = Checker(self.window)
        self.window._subscription_worker = checker
        self.window._subscription_loading_profiles = {PROFILE}
        dialog = SubscriptionDialog(self.window, self.state); self.dialogs.append(dialog)
        with patch('subscription_ui.SubscriptionCheckWorker') as constructor:
            dialog.check_updates()
            constructor.assert_not_called()
        self.assertIs(dialog.worker, checker)
        self.assertTrue(dialog._shared_worker)
        dialog.reject()
        self.assertFalse(checker.stopped, 'Closing a view must keep the shared background feed alive')
        checker.finished.emit()
        self.window._subscription_worker = None

    def test_shared_check_finished_before_view_connects_recovers_from_store(self):
        class Checker(QObject):
            progress = Signal(str, object)
            finished_all = Signal(object)
            finished = Signal()
            stopped = False

            def isRunning(self): return False
            def stop(self): self.stopped = True

        subscriptions = SubscriptionStore(self.state)
        subscriptions.add(PROFILE, baseline_ts=100)
        checker = Checker(self.window)
        dialog = SubscriptionDialog(self.window, self.state); self.dialogs.append(dialog)

        def finish_before_connect():
            subscriptions.set_baseline(PROFILE, 200, unread=True)
            self.window._subscription_worker = checker
            self.window._subscription_loading_profiles = {PROFILE}
            checker.finished_all.emit([dict(profile=PROFILE, error='')])
            checker.finished.emit()

        try:
            with patch.object(self.window, 'start_subscription_check', side_effect=finish_before_connect):
                dialog.check_updates()
            self.assertIsNone(dialog.worker)
            self.assertTrue(dialog.refresh_button.isEnabled())
            self.assertFalse(dialog._loading_profiles)
            self.assertEqual(dialog.info.text(), dialog.text('检查完成。'))
            self.assertEqual(dialog._visible_records[PROFILE]['latest_ts'], 200)
            self.assertTrue(dialog._visible_records[PROFILE]['unread'])
            self.assertFalse(checker.stopped, 'Reconciliation must never cancel the shared check')
        finally:
            self.window._subscription_worker = None
            self.window._subscription_loading_profiles.clear()

    def test_running_shared_check_waits_for_results_and_native_finish(self):
        class Checker(QObject):
            progress = Signal(str, object)
            finished_all = Signal(object)
            finished = Signal()
            running = True
            stopped = False

            def isRunning(self): return self.running
            def stop(self): self.stopped = True

        SubscriptionStore(self.state).add(PROFILE, baseline_ts=100)
        checker = Checker(self.window)
        self.window._subscription_worker = checker
        self.window._subscription_loading_profiles = {PROFILE}
        dialog = SubscriptionDialog(self.window, self.state); self.dialogs.append(dialog)
        try:
            dialog.check_updates()
            self.assertIs(dialog.worker, checker)
            self.assertFalse(dialog.refresh_button.isEnabled())
            self.assertFalse(dialog._reconcile_finished_shared_check())
            checker.finished_all.emit([dict(profile=PROFILE, error='offline')])
            self.assertFalse(dialog._loading_profiles)
            result_text = dialog.info.text()
            self.assertIn(dialog.text('{count} 位画师暂时无法访问。', count=1), result_text)
            self.assertFalse(dialog.refresh_button.isEnabled(), 'Wait for native finished, not just data')
            checker.running = False
            checker.finished.emit()
            self.assertIsNone(dialog.worker)
            self.assertTrue(dialog.refresh_button.isEnabled())
            self.assertEqual(dialog.info.text(), result_text)
            self.assertFalse(checker.stopped)
        finally:
            self.window._subscription_worker = None
            self.window._subscription_loading_profiles.clear()

    def test_early_shared_result_then_connected_native_finish_clears_loading(self):
        class Checker(QObject):
            progress = Signal(str, object)
            finished_all = Signal(object)
            finished = Signal()
            running = True

            def isRunning(self): return self.running
            def stop(self): raise AssertionError('A view cannot stop a shared check')

        checker = Checker(self.window)
        dialog = SubscriptionDialog(self.window, self.state); self.dialogs.append(dialog)

        def result_before_connect():
            self.window._subscription_worker = checker
            self.window._subscription_loading_profiles = {PROFILE}
            checker.finished_all.emit([])

        try:
            with patch.object(self.window, 'start_subscription_check', side_effect=result_before_connect):
                dialog.check_updates()
            self.assertFalse(dialog.refresh_button.isEnabled())
            self.assertEqual(dialog._loading_profiles, {PROFILE})
            checker.running = False; checker.finished.emit()
            self.assertTrue(dialog.refresh_button.isEnabled())
            self.assertFalse(dialog._loading_profiles)
            self.assertIsNone(dialog.worker)
            self.assertEqual(dialog.info.text(), dialog.text('检查完成。'))
        finally:
            self.window._subscription_worker = None
            self.window._subscription_loading_profiles.clear()

    def test_poll_recovers_missing_shared_finished_and_ignores_late_old_signals(self):
        class Checker(QObject):
            progress = Signal(str, object)
            finished_all = Signal(object)
            finished = Signal()
            running = True

            def isRunning(self): return self.running
            def stop(self): raise AssertionError('A view cannot stop a shared check')

        old = Checker(self.window)
        current = Checker(self.window)
        self.window._subscription_worker = old
        dialog = SubscriptionDialog(self.window, self.state); self.dialogs.append(dialog)
        try:
            dialog.check_updates()
            old.running = False
            dialog.show()
            self.wait(20)
            dialog._poll_status()
            self.assertIsNone(dialog.worker)
            self.assertTrue(dialog.refresh_button.isEnabled())
            self.window._subscription_worker = current
            dialog.check_updates()
            self.assertIs(dialog.worker, current)
            self.assertFalse(dialog.refresh_button.isEnabled())
            old.finished_all.emit([dict(error='old failure')]); old.finished.emit()
            self.assertIs(dialog.worker, current)
            self.assertFalse(dialog.refresh_button.isEnabled())
            self.assertEqual(dialog.info.text(), dialog.text('正在后台检查订阅…'))
        finally:
            current.running = False; current.finished.emit()
            self.window._subscription_worker = None
            self.window._subscription_loading_profiles.clear()

    def test_card_and_subscription_views_share_existing_cached_avatar(self):
        avatar = self.image()
        record_download(self.state, PROFILE, posts=1, files=1)
        update_artist_metadata(self.state, PROFILE, avatar_path=avatar)
        SubscriptionStore(self.state).add(PROFILE, baseline_ts=100)
        dialog = SubscriptionDialog(self.window, self.state); self.dialogs.append(dialog)
        dialog.show(); self.wait()
        self.assertFalse(FakeAvatarWorker.created)
        self.assertFalse(dialog.items.item(0).icon().isNull())

    def test_artist_dialog_controls_follow_all_four_languages(self):
        for language in ('简体中文', '繁體中文', 'English', '日本語'):
            self.window.set_lang(language)
            subscriptions = SubscriptionDialog(self.window, self.state); self.dialogs.append(subscriptions)
            cards = ArtistCardsDialog(self.window, self.state); self.dialogs.append(cards)
            self.assertEqual(subscriptions.windowTitle(), artist_text(language, '画师更新订阅'))
            self.assertEqual(subscriptions.refresh_button.text(), artist_text(language, '检查更新'))
            self.assertEqual(cards.migration_button.text(), artist_text(language, '数据迁移'))
            self.assertEqual(cards.add_button.text(), artist_text(language, '添加画师'))


if __name__ == '__main__':
    unittest.main()
