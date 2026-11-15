"""Deterministic game checks; no scanner, user settings or network access."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

import download_pawchive_images as desktop


class SnakeGameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.clock = patch.object(desktop.time, 'monotonic', return_value=100.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.game = desktop.SnakeGameWidget()
        self.game._timer.stop()
        self.addCleanup(self.game.stop)
        self.addCleanup(self.game.deleteLater)
        self.game._invincible_until = 0.0
        self.game.fragments.clear()
        self.game.fragment_buffs.clear()

    def test_idle_requires_thirty_seconds_and_wake_grants_five_seconds(self):
        game = self.game
        self.now.return_value = 129.99
        game.step()
        self.assertFalse(game._idle)
        self.now.return_value = 130.0
        game.step()
        self.assertTrue(game._idle)
        # Even a reverse direction that is ignored as steering is activity.
        self.now.return_value = 131.0
        game._set_direction(tuple(-component for component in game.direction))
        self.assertFalse(game._idle)
        self.assertEqual(136.0, game._invincible_until)
        self.assertEqual(131.0, game._idle_since)
        before = game._score
        self.now.return_value = 135.99
        game._soft_collision()
        self.assertEqual(before, game._score)
        self.now.return_value = 136.01
        game._soft_collision()
        self.assertLess(game._score, before)

    def test_any_key_wakes_idle_with_protection(self):
        game = self.game
        game._idle = True
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Space,
                          Qt.KeyboardModifier.NoModifier)
        game.keyPressEvent(event)
        self.assertFalse(game._idle)
        self.assertEqual(105.0, game._invincible_until)

    def test_idle_does_not_collect_or_take_collision_damage(self):
        game = self.game
        game._idle = True
        game.body = [(5, 5), (5, 4), (6, 4), (6, 5), (6, 6), (5, 6)]
        game.direction = game.next_direction = (1, 0)
        game.obstacles = [[6.0, 5], [5.5, 4]]
        game.fragments = {(6, 5), (7, 5), (8, 5)}
        game.fragment_buffs = {(6, 5): 'pulse'}
        game._magnet_until = 200.0
        before = (game._score, len(game.body), set(game.fragments))
        game._move_snake()  # Hits its body, a glitch, and a shard together.
        game._soft_collision()
        game._apply_magnet(game.body[0])
        self.assertEqual(before, (game._score, len(game.body), game.fragments))
        self.assertFalse(game._rebuild_until)

    def test_vertical_moves_collide_across_fractional_obstacle_positions(self):
        game = self.game
        for offset in (-.95, -.7, -.2, 0, .2, .7, .95):
            with self.subTest(offset=offset):
                game._collision_until = 0.0
                game._score = 20
                game.body = [(10, 9), (9, 9), (8, 9), (7, 9), (6, 9)]
                game.direction = game.next_direction = (0, 1)
                game.obstacles = [[10 + offset, 10]]
                game._move_snake()
                self.assertEqual(18, game._score)
                self.assertEqual(3, len(game.body))

    def test_moving_obstacle_hits_body_between_snake_steps(self):
        game = self.game
        game.body = [(10, 5), (9, 5), (8, 5), (7, 5), (6, 5)]
        game._score = 20
        game.obstacles = [[6.7, 5]]
        game._check_obstacle_collision([7.2])
        self.assertEqual(18, game._score)
        self.assertEqual(3, len(game.body))
        # A single overlapping glitch must not remove a segment every frame.
        game._check_obstacle_collision()
        self.assertEqual(18, game._score)

    def test_timing_preserves_remainder_and_overclock_is_twice_as_fast(self):
        def movement_count(overclock):
            game = self.game
            game._snake_clock = 0.0
            game._last_tick = 100.0
            game._idle_since = 100.0
            game._overclock_until = 200.0 if overclock else 0.0
            with patch.object(game, '_move_snake') as move:
                for frame in range(1, 61):
                    self.now.return_value = 100.0 + frame / 30.0
                    game.step()
                return move.call_count
        self.assertEqual(14, movement_count(False))
        self.assertEqual(28, movement_count(True))

    def test_fragment_generation_checks_occupancy_once_and_avoids_glitch(self):
        game = self.game
        game.obstacles = [[5.5, 7]]
        with patch.object(game, '_occupied', wraps=game._occupied) as occupied:
            game._spawn_fragment()
            self.assertEqual(1, occupied.call_count)
        self.assertTrue({(5, 7), (6, 7)} <= game._occupied())
        self.assertFalse(game.fragments & game._occupied())

    def _body_at_length(self, length):
        game = self.game
        # Leave the next head cell free, with a non-overlapping long tail.
        game.body = [(10, 10)] + [(x, y) for y in range(game.GRID)
                                 for x in range(game.GRID)
                                 if (x, y) != (10, 10)][:length - 1]
        game.direction = game.next_direction = (1, 0)
        game._invincible_until = 200.0

    def test_regular_fragments_grow_to_sixty_then_keep_scoring(self):
        game = self.game
        self._body_at_length(59)
        for index in range(100):
            x, y = game.body[0]
            game.fragments = {((x + 1) % game.GRID, y)}
            game._move_snake()
            self.assertEqual(60, len(game.body))
            self.assertEqual(11 + index, game._score)
            self.assertFalse(game.fragments)

    def test_special_fragments_still_activate_at_length_cap(self):
        game = self.game
        for kind, effect in (('ghost', '_ghost_until'), ('magnet', '_magnet_until'),
                             ('overclock', '_overclock_until'), ('pulse', None)):
            with self.subTest(kind=kind):
                self._body_at_length(60)
                game._score = 10
                game.fragments = {(11, 10)}
                game.fragment_buffs = {(11, 10): kind}
                game.obstacles = [[18.0, 18]]
                game._move_snake()
                self.assertEqual(60, len(game.body))
                self.assertEqual(11, game._score)
                self.assertFalse(game.fragments)
                if effect:
                    self.assertGreater(getattr(game, effect), 100.0)
                else:
                    self.assertFalse(game.obstacles)

    def test_magnet_and_pulse_batch_pickups_cannot_exceed_length_cap(self):
        game = self.game
        self._body_at_length(60)
        game.fragments = {(11, 10), (11, 11), (10, 11)}
        game._magnet_until = 200.0
        game._apply_magnet(game.body[0])
        self.assertEqual(60, len(game.body))
        self.assertEqual(13, game._score)
        self.assertFalse(game.fragments)
        game.fragments = {(17, 17), (18, 18), (19, 19)}
        game.fragment_buffs = {(17, 17): 'pulse', (18, 18): 'ghost'}
        game.obstacles = [[17.0, 19]]
        game._activate_buff('pulse')
        self.assertEqual(60, len(game.body))
        self.assertEqual(16, game._score)
        self.assertFalse(game.fragments)
        self.assertFalse(game.obstacles)
        self.assertGreater(game._ghost_until, 100.0)

    def test_canvas_renders_and_invincibility_visibly_blinks(self):
        game = self.game
        game.show()
        game._invincible_until = 110.0
        # Keep geometry fixed and compare the snake's color between phases.
        self.now.return_value = 100.0
        dim = game.grab().toImage()
        self.now.return_value = 100.2
        bright = game.grab().toImage()
        self.assertFalse(dim.isNull())
        self.assertNotEqual(dim.pixelColor(157, 115), bright.pixelColor(157, 115))


if __name__ == '__main__':
    unittest.main()
