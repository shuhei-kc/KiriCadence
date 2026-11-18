"""History-based white choices with actual book lookup and legal replay."""
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import cshogi
from book_repo import board_sfen_main
from flip import flipped_sfen_main
from main import history_return_move, SEARCH
from test_units import make_wrapper, write_db, StubEngine

ROUTE = '2h3h 8b7b 3h2h'


class HistoryReturnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.w = make_wrapper(str(self.root), StubEngine())
        self.track(ROUTE)

    def track(self, route):
        command = 'position startpos' + (' moves ' + route if route else '')
        self.w._track_position(command.split())
        self.assertTrue(self.w.tracking_ok)

    def book(self, rows=None, name='000_w_test.db', flipped=False):
        key = board_sfen_main(self.w.board)
        if flipped:
            key = flipped_sfen_main(key)
        write_db(str(self.root / 'book' / name),
                 {key: rows if rows is not None else ['3c3d none 500 10']})
        self.w.repo.refresh(str(self.root / 'book'))

    def test_unregistered_return_overrides_higher_book_value(self):
        self.book()
        before = self.w.board.sfen(), self.w.position_counts.copy()
        lines, move = self.w._book_decision()
        self.assertEqual(move, '7b8b')
        self.assertIn('Book_History_Return', lines[0])
        self.assertNotIn('score cp', lines[0])
        self.assertEqual(before, (self.w.board.sfen(), self.w.position_counts))
        self.assertEqual(self.w._book_probe_state, 'hit')
        self.assertIsNone(self.w._book_probe_hit)
        records = [json.loads(s) for s in (self.root / 'logs/decisions.jsonl')
                   .read_text(encoding='utf-8').splitlines()]
        self.assertEqual(records[-1]['event'], 'history_return')
        self.assertEqual(records[-1]['previous_occurrences'], 1)
        self.assertNotIn('value', records[-1])

    def test_low_value_depth_zero_return_also_wins(self):
        self.book(['3c3d none 500 10', '7b8b none -2000 0'])
        self.w.config = replace(self.w.config, book_depth_bonus=100)
        self.assertEqual(self.w._book_decision()[1], '7b8b')

    def test_membership_with_only_depth_zero_still_qualifies(self):
        self.book(['3c3d none 500 0'])
        self.assertEqual(self.w._book_decision()[1], '7b8b')

    def test_flipped_book_membership(self):
        self.book(['7g7f none 500 10'], flipped=True)
        self.assertEqual(self.w._book_decision()[1], '7b8b')

    def test_no_book_and_wrong_side_book_do_not_qualify(self):
        self.assertIsNone(self.w._book_decision())
        self.book(name='000_b_test.db')
        self.assertIsNone(self.w._book_decision())

    def test_disabled_book_does_not_return(self):
        self.book()
        self.w.book_enabled = False
        self.assertIsNone(self.w._book_decision())

    def test_no_matching_history_keeps_normal_choice(self):
        self.book()
        self.w.position_counts.clear()
        self.assertEqual(self.w._book_decision()[1], '3c3d')

    def test_match_includes_hands_side_and_orientation(self):
        self.book()
        child = self.w.board.copy()
        child.push_usi('7b8b')
        key = board_sfen_main(child)
        parts = key.split()
        for other in (' '.join([parts[0], 'w', parts[2]]),
                      ' '.join([parts[0], parts[1], 'P']),
                      flipped_sfen_main(key)):
            self.w.position_counts.clear()
            self.w.position_counts[other] = 1
            self.assertEqual(self.w._book_decision()[1], '3c3d')

    def test_black_policy_unchanged(self):
        self.track('2h3h 8b7b')
        self.book(['3h2h none 100 10'], name='000_b_test.db')
        child = self.w.board.copy()
        child.push_usi('3h2h')
        self.w.position_counts[board_sfen_main(child)] = 1
        self.assertIsNone(history_return_move(self.w.board,
                          self.w.position_counts, object(), {}))
        self.assertEqual(self.w._book_decision()[1], '3h2h')
        self.w.position_counts[board_sfen_main(self.w.board)] = 2
        self.assertIsNone(self.w._book_decision())

    def test_ponderhit_uses_return_and_stops_search(self):
        self.book()
        self.w.state = SEARCH
        self.w._handle_ponderhit()
        self.assertEqual(self.w.gui_lines[-1], 'bestmove 7b8b')
        self.assertIn('stop', self.w.engine.sent)

    def test_pv_uses_return_on_future_white_turn(self):
        self.track('2h3h 8b7b')
        key = board_sfen_main(self.w.board)
        child = self.w.board.copy()
        child.push_usi('3h2h')
        write_db(str(self.root / 'book/000_x_test.db'),
                 {key: ['3h2h none 100 10'],
                  board_sfen_main(child): ['3c3d none 500 10']})
        self.w.repo.refresh(str(self.root / 'book'))
        self.assertEqual(self.w._book_pv('3h2h'), ['3h2h', '7b8b'])


if __name__ == '__main__':
    unittest.main()
