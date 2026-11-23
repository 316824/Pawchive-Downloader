"""Focus/rebuild/teardown regressions and isolated, offline Qt game pressure.

Run the longer native-widget workload with::

    python windows/tests/test_snake_lifecycle.py --soak --report OUTPUT.json

Only worker payloads are synthetic. Widgets, focus events, raster painting,
timers, scheduler threads and deferred deletion are the production objects.
"""
from __future__ import annotations

import argparse
import ctypes
import faulthandler
import gc
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import weakref

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QSettings, QThread, QTimer, Qt, Signal, qInstallMessageHandler
from PySide6.QtGui import QImage, QMouseEvent, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import delete, isValid

import download_pawchive_images as desktop
from download_scheduler import DownloadScheduler


def drain(qt):
    qt.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def press(game):
    position = QPointF(game.rect().center())
    return QMouseEvent(QEvent.Type.MouseButtonPress, position,
                       QPointF(game.mapToGlobal(position.toPoint())),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)


class SnakeLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def game(self):
        game = desktop.SnakeGameWidget()
        game._timer.stop()
        self.addCleanup(lambda: delete(game) if isValid(game) else None)
        return game

    def test_timer_completion_can_destroy_the_canvas_synchronously(self):
        game = self.game()
        game.begin_scan_complete(20)
        game._finish_until = 0
        calls = []
        game.completionFinished.connect(lambda: (calls.append(True), delete(game)))
        # A completion receiver is allowed to replace the complete scan dialog.
        game._advance_dissolve()
        self.assertEqual([True], calls)
        self.assertFalse(isValid(game))

    def test_click_completion_is_once_and_stops_all_timers_before_handoff(self):
        game = self.game()
        game.begin_scan_complete(20)
        calls = []
        game.completionFinished.connect(lambda: calls.append(
            (game._timer.isActive(), game._dissolve_timer.isActive())))
        game.mousePressEvent(press(game))
        game._advance_dissolve()
        game.mousePressEvent(press(game))
        game.start()
        game.begin_scan_complete(99)
        self.assertEqual([(False, False)], calls)
        self.assertFalse(game._timer.isActive())
        self.assertFalse(game._dissolve_timer.isActive())

    def test_mouse_completion_can_destroy_parent_and_canvas(self):
        parent = QDialog()
        game = desktop.SnakeGameWidget(parent)
        game.begin_scan_complete(20)
        game.completionFinished.connect(lambda: delete(parent))
        event = press(game)
        game.mousePressEvent(event)
        self.assertFalse(isValid(game))
        self.assertTrue(event.isAccepted())

    def test_stopped_canvas_does_not_steal_focus_or_emit_a_late_completion(self):
        game = self.game()
        game.begin_scan_complete(5)
        game.stop()
        with patch.object(game, 'setFocus', side_effect=AssertionError('Stopped canvas stole focus')):
            game.mousePressEvent(press(game))
        self.assertFalse(game._finishing)
        self.assertFalse(game._dissolve_particles)

    def test_resume_cannot_restart_game_movement_during_dissolve(self):
        game = self.game()
        game.begin_scan_complete(5)
        game.set_paused(True)
        game.set_paused(False)
        game.start()
        self.assertFalse(game._timer.isActive())
        self.assertTrue(game._dissolve_timer.isActive())

    def test_painter_is_released_even_when_canvas_paint_raises(self):
        game = self.game()
        surface = QImage(300, 220, QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(surface)
        with (patch.object(desktop, 'QPainter', return_value=painter),
              patch.object(game, '_paint_canvas', side_effect=RuntimeError('injected paint failure'))):
            with self.assertRaisesRegex(RuntimeError, 'injected paint failure'):
                game.paintEvent(None)
        self.assertFalse(painter.isActive())
        replacement = QPainter(surface)
        self.assertTrue(replacement.isActive())
        replacement.end()

    def test_pulse_chain_is_bounded_even_for_four_hundred_special_fragments(self):
        game = self.game()
        game.fragments = {(x, y) for x in range(20) for y in range(20)}
        game.fragment_buffs = {point: 'pulse' for point in game.fragments}
        game._activate_buff('pulse')
        self.assertEqual(410, game._score)
        self.assertFalse(game.fragments)
        self.assertFalse(game.fragment_buffs)
        self.assertFalse(game._collecting_all)

    def test_native_focus_rebuild_and_teardown_in_an_isolated_process(self):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, str(Path(__file__)), '--child'],
                                capture_output=True, text=True, encoding='utf-8',
                                env=env, timeout=35)
        self.assertEqual(0, result.returncode, result.stderr[-4000:])
        metrics = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(40, metrics['destroyed_dialogs'])
        self.assertGreaterEqual(metrics['focus_clicks'], 240)
        self.assertFalse(metrics['qt_paint_errors'])
        self.assertFalse(metrics['python_errors'])


class TransferWorker(QThread):
    progress = Signal(str, object, object)
    file_progress = Signal(int, int)
    finished_ok = Signal(int)
    failed = Signal(str)

    def run(self):
        for count in range(8):
            self.progress.emit('synthetic.png', count * 1024, 8192)
            self.msleep(1)
        self.file_progress.emit(1, 1)
        self.finished_ok.emit(1)


def run_pressure(*, extended=False):
    faulthandler.enable()
    if os.name == 'nt':
        ctypes.windll.kernel32.SetErrorMode(0x8003)
    qt = QApplication.instance() or QApplication([])
    qt.setQuitOnLastWindowClosed(False)
    metrics = dict(platform=QApplication.platformName(), simulated_frames=0,
                   simulated_game_seconds=0, focus_clicks=0, rebuilds=0,
                   rendered_canvases=0, destroyed_dialogs=0, completed_workers=0,
                   qt_paint_errors=[], python_errors=[])
    old_hook = sys.excepthook
    sys.excepthook = lambda kind, error, tb: metrics['python_errors'].append(str(error))
    old_thread_hook = threading.excepthook
    threading.excepthook = lambda args: metrics['python_errors'].append(str(args.exc_value))
    def message_handler(kind, context, message):
        if any(token in message for token in ('QPainter', 'paint device', 'QBackingStore',
                                              'QThread: Destroyed', 'QObject::killTimer')):
            metrics['qt_paint_errors'].append(message)
    old_handler = qInstallMessageHandler(message_handler)
    rounds = 300 if extended else 40
    frames = 200 if extended else 12
    workers = 600 if extended else 60
    rng = random.Random(831)
    refs = []
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='pawchive_snake_pressure_') as directory:
            # No user paths, credentials, task registration or real HTTP calls.
            with patch('requests.sessions.Session.request', side_effect=AssertionError('No network in game stress')):
                host = desktop.App(QSettings(str(Path(directory) / 'settings.ini'), QSettings.Format.IniFormat))
                host.update_controller.startup_timer.stop()
                host.show()
                scheduler = DownloadScheduler(lambda _: TransferWorker(), host)
                scheduler.add_tasks([dict(id=str(i), title=f'Album {i}',
                    url=f'https://pawchive.pw/fanbox/user/1/post/{i}',
                    formats=['.png']) for i in range(workers)])
                scheduler.task_finished.connect(lambda _: metrics.__setitem__(
                    'completed_workers', metrics['completed_workers'] + 1))
                scheduler.start()
                for index in range(rounds):
                    dialog = desktop.ScanAnimation(host)
                    dialog.show()
                    game = dialog.game
                    refs.append(weakref.ref(dialog))
                    refs.append(weakref.ref(game))
                    game._timer.stop()
                    now = [time.monotonic()]
                    for frame in range(frames):
                        now[0] += .14
                        with patch.object(desktop.time, 'monotonic', side_effect=lambda: now[0]):
                            if frame % 37 == 0:
                                game._invincible_until = game._collision_until = 0
                                game._ghost_until = game._overclock_until = 0
                                game._score = 2
                                game._soft_collision()
                                assert game._rebuild_until
                                metrics['rebuilds'] += 1
                            if frame % 2 == 0:
                                host.profile.setFocus()
                                QCoreApplication.sendEvent(game, press(game))
                                metrics['focus_clicks'] += 1
                                QTest.keyClick(game, rng.choice((Qt.Key.Key_A, Qt.Key.Key_S,
                                                                Qt.Key.Key_D, Qt.Key.Key_W)))
                            if frame % 17 == 0:
                                game.fragments = {(x, y) for x in range(20) for y in range(20)} - set(game.body)
                                kinds = ('ghost', 'magnet', 'overclock', 'pulse')
                                game.fragment_buffs = {point: rng.choice(kinds) for point in game.fragments}
                                game._activate_buff('pulse')
                            game.step()
                            assert 1 <= len(game.body) <= 60
                            assert game._score >= 0
                            metrics['simulated_frames'] += 1
                            if frame % 5 == 0:
                                canvas = game.grab().toImage()
                                assert not canvas.isNull()
                                metrics['rendered_canvases'] += 1
                        if frame % 10 == 0:
                            drain(qt)
                    dialog.update_counts(index + 1, 1000 + index)
                    if index % 3 == 0:
                        game._rebuild_until = time.monotonic() + 2
                    else:
                        game._rebuild_until = 0
                    dialog.show_completion(1000, lambda: None, automatic=bool(index % 2))
                    if dialog._completion_callback is not None:
                        if index % 2:
                            dialog._auto_completion_timer.setInterval(1)
                            QTest.qWait(3)
                        else:
                            overlay = dialog._completion_overlay
                            QCoreApplication.sendEvent(overlay, press(overlay))
                    dialog._allow_close = True
                    dialog.close()
                    dialog.deleteLater()
                    drain(qt)
                    assert not isValid(dialog)
                    assert not isValid(game)
                    metrics['destroyed_dialogs'] += 1
                deadline = time.monotonic() + 15
                while (scheduler._workers or any(task.state != 'completed' for task in scheduler.tasks)) and time.monotonic() < deadline:
                    drain(qt)
                    time.sleep(.001)
                assert not scheduler._workers
                assert metrics['completed_workers'] == workers
                scheduler.stop()
                scheduler.deleteLater()
                host.busy = False
                host.close()
                host.deleteLater()
                drain(qt)
                del game, dialog, host, scheduler
                gc.collect()
                assert not any(ref() is not None and isValid(ref()) for ref in refs)
    finally:
        qInstallMessageHandler(old_handler)
        sys.excepthook = old_hook
        threading.excepthook = old_thread_hook
    metrics['elapsed_seconds'] = round(time.monotonic() - started, 3)
    metrics['simulated_game_seconds'] = round(metrics['simulated_frames'] * .14, 2)
    assert not metrics['python_errors'], metrics['python_errors']
    assert not metrics['qt_paint_errors'], metrics['qt_paint_errors']
    return metrics


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--soak', action='store_true')
    parser.add_argument('--report', type=Path)
    args, other = parser.parse_known_args()
    if args.child or args.soak:
        result = run_pressure(extended=args.soak)
        if args.report:
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    else:
        unittest.main(argv=[sys.argv[0], *other])
