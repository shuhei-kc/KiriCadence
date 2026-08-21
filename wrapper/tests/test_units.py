"""ユニットテスト (DESIGN.md §11)。

config パース / flip / ファイル名規約と照会順 / フィルタ (終端手・
非合法手・depthゲート) / flip発火条件 / タイブレーク3種 / .ybb照会 /
決定ログ。エンジン不要のオフライン部だけを対象にする。
"""

import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import cshogi  # noqa: E402

import config as config_mod  # noqa: E402
import yaneuraou_book_lib as ybl  # noqa: E402
from book_repo import (BookRepository, board_sfen_main,  # noqa: E402
                       scan_book_dir)
from decision_log import DecisionLogger  # noqa: E402
import engine_proxy  # noqa: E402
import main as main_mod  # noqa: E402
from flip import flipped_move, flipped_sfen_main  # noqa: E402
from selector import make_tie_breaker  # noqa: E402


def board_from(moves: str = "") -> "cshogi.Board":
    board = cshogi.Board()
    for token in moves.split():
        board.push_usi(token)
    return board


def sfen_key(moves: str = "") -> str:
    return board_sfen_main(board_from(moves))


def write_db(path: str, blocks: dict[str, list[str]]) -> None:
    """sfen主要部 -> 指し手行リスト を SFEN順ソートで書く。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write("#YANEURAOU-DB2016 1.00\n")
        for sfen in sorted(blocks):
            f.write(f"sfen {sfen} 0\n")
            for line in blocks[sfen]:
                f.write(line + "\n")


class TestConfig(unittest.TestCase):
    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return config_mod.load_config(path)

    def test_basic_parse_and_trim(self):
        cfg, warnings = self._load(
            "# comment\n"
            "EnginePath   engine/foo.exe   \n"
            "\n"
            "BookMinDepth 3\n"
            "TieBreak First\n")
        self.assertTrue(cfg.engine_path.endswith("foo.exe"))
        self.assertNotIn(" ", cfg.engine_path[-8:])   # 末尾空白トリム
        self.assertEqual(cfg.book_min_depth, 3)
        self.assertEqual(cfg.tie_break, "first")      # 大文字許容
        self.assertEqual(warnings, [])

    def test_bom_and_unknown_key(self):
        cfg, warnings = self._load("﻿EnginePath x.exe\nNoSuchKey 1\n")
        self.assertTrue(cfg.engine_path.endswith("x.exe"))
        self.assertTrue(any("NoSuchKey" in w for w in warnings))

    def test_invalid_min_depth_fails_at_startup(self):
        with self.assertRaises(config_mod.ConfigError):
            self._load("EnginePath x\nBookMinDepth abc\n")

    def test_invalid_tie_break_fails_at_startup(self):
        with self.assertRaises(config_mod.ConfigError):
            self._load("EnginePath x\nTieBreak sometimes\n")

    def test_missing_engine_path_warns_but_loads(self):
        cfg, warnings = self._load("BookMinDepth 1\n")
        self.assertEqual(cfg.engine_path, "")
        self.assertTrue(any("EnginePath" in w for w in warnings))

    def test_reload_keeps_previous_on_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("EnginePath x\nBookMinDepth 2\nTieBreak random\n")
            cfg, _ = config_mod.load_config(path)
            self.assertEqual(cfg.book_min_depth, 2)
            # 不正値へ書き換え → 前の値を維持して警告
            with open(path, "w", encoding="utf-8") as f:
                f.write("EnginePath x\nBookMinDepth oops\nTieBreak first\n")
            cfg2, warnings = config_mod.reload_book_settings(cfg)
            self.assertEqual(cfg2.book_min_depth, 2)      # 維持
            self.assertEqual(cfg2.tie_break, "first")     # 正しい値は反映
            self.assertTrue(any("BookMinDepth" in w for w in warnings))

    def test_quoted_paths_are_unquoted(self):
        cfg, _ = self._load('EnginePath "C:/x y/engine.exe"\n'
                            "BookDir 'my book'\n")
        self.assertTrue(cfg.engine_path.endswith("engine.exe"))
        self.assertNotIn('"', cfg.engine_path)
        self.assertTrue(cfg.book_dir.endswith("my book"))

    def test_task_emit_on_validation(self):
        cfg, _ = self._load("EnginePath x\nTaskEmitOn Both\n")
        self.assertEqual(cfg.task_emit_on, "both")
        cfg, _ = self._load("EnginePath x\n")
        self.assertEqual(cfg.task_emit_on, "loss")     # 既定は負けのみ
        with self.assertRaises(config_mod.ConfigError):
            self._load("EnginePath x\nTaskEmitOn sometimes\n")

    def test_reload_survives_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("EnginePath x\n")
            cfg, _ = config_mod.load_config(path)
            os.remove(path)
            cfg2, warnings = config_mod.reload_book_settings(cfg)
            self.assertEqual(cfg2, cfg)
            self.assertTrue(warnings)


class TestFlip(unittest.TestCase):
    def test_move_roundtrip(self):
        for usi in ("7g7f", "2b3a", "8h2b+", "P*5e", "N*3c"):
            flipped = flipped_move(usi)
            self.assertIsNotNone(flipped)
            self.assertEqual(flipped_move(flipped), usi)

    def test_move_examples(self):
        self.assertEqual(flipped_move("7g7f"), "3c3d")
        self.assertEqual(flipped_move("8c8d"), "2g2f")
        self.assertEqual(flipped_move("P*5e"), "P*5e")
        self.assertEqual(flipped_move("8h2b+"), "2b8h+")
        self.assertIsNone(flipped_move("resign"))
        self.assertIsNone(flipped_move("none"))

    def test_sfen_roundtrip_with_promoted_and_hands(self):
        board = board_from("7g7f 3c3d 8h2b+ 3a2b")  # 角交換 (双方持駒あり)
        sfen_main = board_sfen_main(board)
        flipped = flipped_sfen_main(sfen_main)
        self.assertIsNotNone(flipped)
        self.assertEqual(flipped_sfen_main(flipped), sfen_main)
        # 反転結果が正当なSFENであること (cshogiが受理する)
        check = cshogi.Board()
        check.set_sfen(flipped + " 1")

    def test_sfen_flip_matches_cshogi_apply(self):
        """反転局面で反転手を指す = 元局面で指して反転する、の一致検証。"""
        board = board_from("2g2f 8c8d")
        sfen_main = board_sfen_main(board)
        flipped = flipped_sfen_main(sfen_main)
        move, fmove = "2f2e", flipped_move("2f2e")
        a = cshogi.Board(); a.set_sfen(sfen_main + " 1"); a.push_usi(move)
        b = cshogi.Board(); b.set_sfen(flipped + " 1"); b.push_usi(fmove)
        self.assertEqual(flipped_sfen_main(board_sfen_main(a)),
                         board_sfen_main(b))


class TestScanOrder(unittest.TestCase):
    def test_priority_side_and_name_order(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("100_x_c.db", "100_b_z.db", "100_w_a.db",
                         "000_x_main.db", "900_b_deep.db",
                         "misc.db", "12_b_bad.db", "100_q_bad.db",
                         "100_b_bad.txt"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("#YANEURAOU-DB2016 1.00\n")
            warnings = []
            infos = scan_book_dir(d, warnings.append, set())
            self.assertEqual(
                [i.name for i in infos],
                ["000_x_main.db", "100_b_z.db", "100_w_a.db",
                 "100_x_c.db", "900_b_deep.db"])   # b/w が x より先
            joined = "\n".join(warnings)
            for bad in ("misc.db", "12_b_bad.db", "100_q_bad.db",
                        "100_b_bad.txt"):
                self.assertIn(bad, joined)

    def test_warned_only_once(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "junk.txt"), "w") as f:
                f.write("x")
            warnings = []
            warned = set()
            scan_book_dir(d, warnings.append, warned)
            scan_book_dir(d, warnings.append, warned)
            self.assertEqual(len(warnings), 1)


class TestProbe(unittest.TestCase):
    """book_repo.probe のフィルタ・優先順・flip発火条件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.repo = BookRepository(log=lambda m: None)
        self.tb = make_tie_breaker("first")
        self.rng = random.Random(0)

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self, board, min_depth=1, tb=None):
        self.repo.refresh(self.dir)
        return self.repo.probe(board, min_depth, tb or self.tb, self.rng)

    def test_direct_hit_and_priority(self):
        start = board_sfen_main(board_from())
        write_db(os.path.join(self.dir, "100_x_low.db"),
                 {start: ["7g7f none 10 5"]})
        write_db(os.path.join(self.dir, "000_b_high.db"),
                 {start: ["2g2f none 10 5"]})
        hit, misses = self.probe(board_from())
        self.assertEqual(hit.usi, "2g2f")
        self.assertEqual(hit.file_name, "000_b_high.db")
        self.assertEqual(misses, {})   # 高優先でヒットしたら以降は見ない

    def test_side_filter_uses_game_turn(self):
        board = board_from("7g7f")      # 後手番
        sfen = board_sfen_main(board)
        write_db(os.path.join(self.dir, "000_b_black.db"),
                 {sfen: ["8c8d none 99 5"]})
        write_db(os.path.join(self.dir, "100_x_both.db"),
                 {sfen: ["3c3d none 10 5"]})
        hit, _ = self.probe(board)
        self.assertEqual(hit.usi, "3c3d")   # bファイルは後手番でスキップ
        self.assertEqual(hit.file_name, "100_x_both.db")

    def test_falls_through_to_next_file_on_depth_gate(self):
        start = board_sfen_main(board_from())
        write_db(os.path.join(self.dir, "000_x_shallow.db"),
                 {start: ["7g7f none 100 0"]})       # depth 0 → gate
        write_db(os.path.join(self.dir, "100_x_deep.db"),
                 {start: ["2g2f none 10 5"]})
        hit, misses = self.probe(board_from())
        self.assertEqual(hit.usi, "2g2f")
        self.assertEqual(misses["000_x_shallow.db"], "depth_gate")

    def test_depth_gate_does_not_pick_second_value(self):
        """value最大がdepth不足でも、value2番手は指さずファイルミス。"""
        start = board_sfen_main(board_from())
        write_db(os.path.join(self.dir, "000_x_a.db"),
                 {start: ["7g7f none 100 0", "2g2f none 50 30"]})
        hit, misses = self.probe(board_from())
        self.assertIsNone(hit)
        self.assertEqual(misses["000_x_a.db"], "depth_gate")

    def test_min_depth_zero_ignores_depth(self):
        start = board_sfen_main(board_from())
        write_db(os.path.join(self.dir, "000_x_a.db"),
                 {start: ["7g7f none 100 0"]})
        hit, _ = self.probe(board_from(), min_depth=0)
        self.assertEqual(hit.usi, "7g7f")

    def test_terminal_only_and_illegal_only(self):
        start = board_sfen_main(board_from())
        write_db(os.path.join(self.dir, "000_x_term.db"),
                 {start: ["resign none 0 0", "win none 0 0"]})
        write_db(os.path.join(self.dir, "100_x_illegal.db"),
                 {start: ["5e5d none 10 5"]})        # 5e に駒がない
        hit, misses = self.probe(board_from())
        self.assertIsNone(hit)
        self.assertEqual(misses["000_x_term.db"], "terminal_only")
        self.assertEqual(misses["100_x_illegal.db"], "illegal_only")

    def test_illegal_candidate_is_dropped_not_played(self):
        start = board_sfen_main(board_from())
        write_db(os.path.join(self.dir, "000_x_a.db"),
                 {start: ["5e5d none 999 9", "7g7f none 10 5"]})
        hit, _ = self.probe(board_from())
        self.assertEqual(hit.usi, "7g7f")   # 非合法な最大値は除外される

    def test_flip_hit_returns_unflipped_move(self):
        board = board_from("7g7f")          # 後手番の実局面
        flipped = flipped_sfen_main(board_sfen_main(board))
        # 反転局面 (先手番形) だけを収録。3c3d を反転すると 7g7f 側の形
        write_db(os.path.join(self.dir, "000_w_flip.db"),
                 {flipped: [f"{flipped_move('3c3d')} none 42 7"]})
        hit, _ = self.probe(board)
        self.assertTrue(hit.flipped)
        self.assertEqual(hit.usi, "3c3d")   # 実局面の向きで返る
        self.assertEqual(hit.value, 42)

    def test_flip_fires_when_direct_hit_is_all_filtered(self):
        """§5 の擬似コード矛盾の回帰テスト: 直接ヒットが全滅 (depth gate)
        でも flip を試すこと。"""
        board = board_from()                # 先手番
        start = board_sfen_main(board)
        flipped = flipped_sfen_main(start)
        write_db(os.path.join(self.dir, "000_b_a.db"),
                 {start: ["7g7f none 100 0"],            # 直接: depth 0 全滅
                  flipped: [f"{flipped_move('2g2f')} none 55 9"]})
        hit, _ = self.probe(board)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.flipped)
        self.assertEqual(hit.usi, "2g2f")

    def test_entry_with_no_moves_reports_no_moves(self):
        board = board_from()
        start = board_sfen_main(board)
        after = board_sfen_main(board_from("7g7f"))
        blocks = {start: [], after: ["8c8d none 10 5"]}
        write_db(os.path.join(self.dir, "000_x_a.db"), blocks)
        hit, misses = self.probe(board)
        self.assertIsNone(hit)
        self.assertEqual(misses["000_x_a.db"], "no_moves")

    def test_unsorted_db_is_detected_and_disabled(self):
        """実行時ソート検証: 非ソート .db は unsorted として止まり、
        静かな no_entry で誤魔化されないこと。"""
        seqs = ["", "7g7f", "7g7f 3c3d", "2g2f", "2g2f 8c8d", "1g1f",
                "9g9f", "5g5f", "3g3f", "7g7f 8c8d"]
        keys = sorted({sfen_key(m) for m in seqs})
        path = os.path.join(self.dir, "000_x_bad.db")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#YANEURAOU-DB2016 1.00\n")
            for key in reversed(keys):          # 降順 = 非ソート
                f.write(f"sfen {key} 0\n7g7f none 10 5\n")
        # 物理的に最後へ置かれた最小キー (=開始局面側) を引く
        target_board = board_from(
            next(m for m in seqs if sfen_key(m) == keys[0]))
        hit, misses = self.probe(target_board)
        self.assertIsNone(hit)
        self.assertEqual(misses["000_x_bad.db"], "unsorted")
        # 同じ対局内の次の照会はスキップ (unsorted のまま)
        hit, misses = self.repo.probe(target_board, 1, self.tb, self.rng)
        self.assertEqual(misses["000_x_bad.db"], "unsorted")
        # refresh (usinewgame相当) で再検出対象に戻る
        self.repo.refresh(self.dir)
        self.assertNotIn(path, self.repo._unsorted)

    def test_count_breaks_value_ties(self):
        """value が同じなら採用回数の多い手を採る (makebook 系の補強回数)。

        value は評価値なので回数を書かない — 32000 はやねうら王の
        VALUE_MATE で、回数を value に入れると詰みと区別が付かなくなる。
        """
        start = board_sfen_main(board_from())
        write_db(os.path.join(self.dir, "000_x_a.db"),
                 {start: ["7g7f none 0 32 3", "2g2f none 0 32 40",
                          "1g1f none 0 32 7"]})
        hit, _ = self.probe(board_from())
        self.assertEqual(hit.usi, "2g2f")          # 回数最大
        self.assertEqual(hit.value, 0)             # 評価値は 0 のまま

    def test_value_still_wins_over_count(self):
        """評価値の方が優先 (回数は同値のときだけ効く)。"""
        start = board_sfen_main(board_from())
        write_db(os.path.join(self.dir, "000_x_a.db"),
                 {start: ["7g7f none 90 32 1", "2g2f none 10 32 999"]})
        hit, _ = self.probe(board_from())
        self.assertEqual(hit.usi, "7g7f")

    def test_four_column_book_is_unaffected(self):
        """count 列の無い定跡 (peta 等) は全部 count=1 なので素通り。"""
        start = board_sfen_main(board_from())
        write_db(os.path.join(self.dir, "000_x_a.db"),
                 {start: ["7g7f none 50 32", "2g2f none 50 32"]})
        hit, _ = self.probe(board_from(), tb=make_tie_breaker("first"))
        self.assertEqual(hit.usi, "7g7f")          # 記載順の先頭
        self.assertEqual(hit.tie_candidates, 2)    # count で絞られていない

    def test_missing_dir_and_broken_file(self):
        self.repo.refresh(os.path.join(self.dir, "no_such_dir"))
        hit, misses = self.repo.probe(board_from(), 1, self.tb, self.rng)
        self.assertIsNone(hit)
        self.assertEqual(misses, {})
        # 壊れた .ybb は io_error でミス継続
        bad = os.path.join(self.dir, "000_x_bad.ybb")
        with open(bad, "wb") as f:
            f.write(b"not a ybb file at all")
        start = board_sfen_main(board_from())
        write_db(os.path.join(self.dir, "100_x_ok.db"),
                 {start: ["7g7f none 10 5"]})
        self.repo.refresh(self.dir)
        hit, misses = self.repo.probe(board_from(), 1, self.tb, self.rng)
        self.assertEqual(hit.usi, "7g7f")
        self.assertEqual(misses["000_x_bad.ybb"], "io_error")


class TestTieBreak(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.repo = BookRepository(log=lambda m: None)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_tied_book(self):
        """startpos で 2g2f / 7g7f が同値。応手列は 7g7f 側が有利:
        2g2f後: [10, -5] / 7g7f後: [10, -20] → deep は 7g7f を選ぶ。"""
        b26 = board_from("2g2f")
        b76 = board_from("7g7f")
        write_db(os.path.join(self.dir, "000_b_tied.db"), {
            board_sfen_main(board_from()): [
                "2g2f none 50 5", "7g7f none 50 5", "1g1f none 40 5"],
            board_sfen_main(b26): ["8c8d none 10 5", "3c3d none -5 5"],
            board_sfen_main(b76): ["8c8d none 10 5", "3c3d none -20 5"],
        })

    def probe(self, name):
        self._write_tied_book()
        self.repo.refresh(self.dir)
        hit, _ = self.repo.probe(board_from(), 1, make_tie_breaker(name),
                                 random.Random(7))
        return hit

    def test_deep_picks_move_that_limits_opponent(self):
        hit = self.probe("deep")
        self.assertEqual(hit.usi, "7g7f")
        self.assertEqual(hit.tie_candidates, 2)
        self.assertEqual(hit.decided_at, 2)     # 第2応手で差がついた

    def test_first_is_book_order(self):
        hit = self.probe("first")
        self.assertEqual(hit.usi, "2g2f")

    def test_random_picks_among_tied_only(self):
        seen = set()
        for seed in range(20):
            self._write_tied_book()
            self.repo.refresh(self.dir)
            hit, _ = self.repo.probe(board_from(), 1,
                                     make_tie_breaker("random"),
                                     random.Random(seed))
            seen.add(hit.usi)
        self.assertEqual(seen, {"2g2f", "7g7f"})   # 1g1f (value 40) は不可

    def test_deep_prefers_in_book_child(self):
        """指した先が定跡外の候補は最下位 (定跡に留まる手を優先)。"""
        b26 = board_from("2g2f")
        write_db(os.path.join(self.dir, "000_b_tied.db"), {
            board_sfen_main(board_from()): [
                "7g7f none 50 5", "2g2f none 50 5"],
            board_sfen_main(b26): ["8c8d none 10 5"],   # 2g2f 側だけ定跡内
        })
        self.repo.refresh(self.dir)
        hit, _ = self.repo.probe(board_from(), 1, make_tie_breaker("deep"),
                                 random.Random(0))
        self.assertEqual(hit.usi, "2g2f")

    def test_deep_draws_lots_when_no_replies(self):
        """応手が1つも引けないときは抽選する。

        応手は同じ定跡ファイルからしか引かないので、片手番だけの定跡
        (b または w のみ) では deep が原理的に働かない。記載順で固定すると
        意図して登録した2番手以降が一生指されなくなる。
        """
        # 先手番の局面しか入っていない b ファイル (指した先は後手番なので
        # このファイルには存在しない = 応手が引けない)
        write_db(os.path.join(self.dir, "000_b_oneside.db"), {
            board_sfen_main(board_from()): [
                "7g7f none 0 32", "2g2f none 0 32", "1g1f none 0 32"],
        })
        self.repo.refresh(self.dir)
        picked = set()
        for seed in range(40):
            hit, _ = self.repo.probe(board_from(), 1,
                                     make_tie_breaker("deep"),
                                     random.Random(seed))
            picked.add(hit.usi)
        self.assertEqual(picked, {"7g7f", "2g2f", "1g1f"})

    def test_deep_stays_deterministic_when_replies_exist(self):
        """応手が引ける局面では従来どおり決定的に選ぶ (抽選しない)。"""
        b26, b76 = board_from("2g2f"), board_from("7g7f")
        write_db(os.path.join(self.dir, "000_b_tied.db"), {
            board_sfen_main(board_from()): [
                "2g2f none 50 5", "7g7f none 50 5"],
            board_sfen_main(b26): ["8c8d none 10 5", "3c3d none -5 5"],
            board_sfen_main(b76): ["8c8d none 10 5", "3c3d none -20 5"],
        })
        self.repo.refresh(self.dir)
        for seed in range(20):
            hit, _ = self.repo.probe(board_from(), 1,
                                     make_tie_breaker("deep"),
                                     random.Random(seed))
            self.assertEqual(hit.usi, "7g7f")      # 相手の2番手が悪い側
            self.assertEqual(hit.decided_at, 2)


class TestYbb(unittest.TestCase):
    def test_probe_ybb_with_depth(self):
        with tempfile.TemporaryDirectory() as d:
            start = board_sfen_main(board_from())
            book = {f"{start} 1": [ybl.BookMove("7g7f", "none", 33, 8, 1)]}
            path = os.path.join(d, "000_x_bin.ybb")
            ybl.write_ybb_book(book, path)
            repo = BookRepository(log=lambda m: None)
            repo.refresh(d)
            hit, _ = repo.probe(board_from(), 1, make_tie_breaker("first"),
                                random.Random(0))
            self.assertEqual(hit.usi, "7g7f")
            self.assertEqual(hit.value, 33)
            self.assertEqual(hit.depth, 8)


class TestDecisionLogger(unittest.TestCase):
    def test_records_have_game_id(self):
        from book_repo import BookHit
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "decisions.jsonl")
            logger = DecisionLogger(path)
            logger.newgame()
            hit = BookHit("2g2f", 10, 5, False, "000_b_a.db", 2, 1)
            logger.hit("sfen_x", 4, "b", hit, "deep")
            logger.miss("sfen_y", 5, "w", files={"000_w_a.db": "no_entry"})
            logger.miss("sfen_z", 6, "b", reason="repetition_guard")
            logger.gameover("win")
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f]
        self.assertEqual([r["event"] for r in rows],
                         ["newgame", "hit", "miss", "miss", "gameover"])
        expected_id = f"{logger.instance_id}-0001"
        self.assertTrue(all(r["game_id"] == expected_id for r in rows))
        self.assertEqual(rows[1]["move"], "2g2f")
        self.assertEqual(rows[1]["tie"], "deep")
        self.assertEqual(rows[2]["files"], {"000_w_a.db": "no_entry"})
        self.assertEqual(rows[3]["reason"], "repetition_guard")
        self.assertEqual(rows[4]["result"], "win")

    def test_implicit_game_numbering_and_tz(self):
        """usinewgame を送らないクライアントでも -0000 に潰れないこと。"""
        from book_repo import BookHit
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "decisions.jsonl")
            logger = DecisionLogger(path)
            hit = BookHit("7g7f", 1, 5, False, "000_b_a.db", 1, 0)
            logger.hit("s1", 0, "b", hit, "deep",
                       prior_misses={"000_b_x.db": "no_entry"})
            logger.gameover("win")
            logger.miss("s2", 0, "b", files={})     # 次局も暗黙採番
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f]
        self.assertEqual([r["event"] for r in rows],
                         ["newgame", "hit", "gameover", "newgame", "miss"])
        self.assertTrue(rows[0]["implicit"])
        self.assertTrue(rows[0]["game_id"].endswith("-0001"))
        self.assertTrue(rows[3]["game_id"].endswith("-0002"))
        self.assertEqual(rows[1]["prior_misses"], {"000_b_x.db": "no_entry"})
        # ts に UTC オフセットが付いていること (+0900 等)
        self.assertRegex(rows[0]["ts"], r"[+-][0-9]{4}$")

    def test_no_path_is_noop(self):
        logger = DecisionLogger(None)
        logger.newgame()    # 例外にならないこと
        logger.gameover("")


class TestTaskEmitter(unittest.TestCase):
    """対局中の決着水準でタスクを1回だけ書き出す (連携骨格)。

    評価値は探索の手番側 = 自分から見た値なので、負の値は自分の劣勢。
    既定は「負けたときだけ」投入する。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "tasks")

    def _emitter(self, mode="loss", threshold=1000):
        from task_emitter import TaskEmitter
        return TaskEmitter(self.dir, threshold, mode)

    def _tasks(self):
        if not os.path.isdir(self.dir):
            return []
        return [n for n in os.listdir(self.dir) if n.endswith(".json")]

    def _record(self):
        names = self._tasks()
        self.assertEqual(len(names), 1)
        with open(os.path.join(self.dir, names[0]), encoding="utf-8") as f:
            return json.load(f)

    # -- 既定 (負けたときだけ) --

    def test_loss_emits_and_win_does_not(self):
        emitter = self._emitter()
        pos = "position startpos moves 7g7f 3c3d"
        self.assertFalse(emitter.observe("info score cp 3000 pv 2g2f", pos),
                         "勝勢では投入しない")
        self.assertFalse(emitter.observe("info score cp -300 pv 2g2f", pos))
        self.assertTrue(emitter.observe("info score cp -1200 pv 2g2f", pos))
        record = self._record()
        self.assertEqual(record["payload"]["score"], -1200)
        self.assertEqual(record["payload"]["outcome"], "loss")
        # 先頭の "position " は落として makebook 側の形式に合わせる
        self.assertEqual(record["payload"]["position"],
                         "startpos moves 7g7f 3c3d")

    def test_only_once_per_game(self):
        emitter = self._emitter()
        pos = "position startpos"
        self.assertTrue(emitter.observe("info score cp -1200", pos))
        self.assertFalse(emitter.observe("info score cp -2000", pos))
        self.assertEqual(len(self._tasks()), 1)

    def test_mated_score_emits(self):
        emitter = self._emitter()
        self.assertFalse(emitter.observe("info score mate 5",
                                         "position startpos"))
        self.assertTrue(emitter.observe("info score mate -3",
                                        "position startpos"))

    # -- 他のモード --

    def test_win_mode(self):
        emitter = self._emitter("win")
        self.assertFalse(emitter.observe("info score cp -3000",
                                         "position startpos"))
        self.assertTrue(emitter.observe("info score cp 1200",
                                        "position startpos"))
        self.assertEqual(self._record()["payload"]["outcome"], "win")

    def test_both_mode(self):
        emitter = self._emitter("both")
        self.assertTrue(emitter.observe("info score cp 1200",
                                        "position startpos"))

    def test_unknown_mode_falls_back_to_loss(self):
        emitter = self._emitter("nonsense")
        self.assertFalse(emitter.observe("info score cp 5000",
                                         "position startpos"))
        self.assertTrue(emitter.observe("info score cp -5000",
                                        "position startpos"))

    # -- 見ない場合 --

    def test_analysis_search_is_ignored(self):
        """go infinite / go mate の評価値は対局と無関係なので投入しない。"""
        emitter = self._emitter()
        emitter.set_game_search(False)
        self.assertFalse(emitter.observe("info score cp -5000",
                                         "position startpos"))
        emitter.set_game_search(True)
        self.assertTrue(emitter.observe("info score cp -5000",
                                        "position startpos"))

    def test_bounds_and_no_score_ignored(self):
        emitter = self._emitter()
        for line in ("info score cp -5000 upperbound",
                     "info depth 3 nodes 10", "info string hello"):
            self.assertFalse(emitter.observe(line, "position startpos"))
        self.assertEqual(self._tasks(), [])

    def test_newgame_resets(self):
        emitter = self._emitter()
        self.assertTrue(emitter.observe("info score cp -1200",
                                        "position startpos"))
        emitter.newgame()
        self.assertTrue(emitter.observe("info score cp -1200",
                                        "position startpos"))
        self.assertEqual(len(self._tasks()), 2)

    def test_disabled_without_dir(self):
        from task_emitter import TaskEmitter
        off = TaskEmitter("", 1000)
        self.assertFalse(off.enabled)
        self.assertFalse(off.observe("info score cp -5000",
                                     "position startpos"))

    def test_no_position_no_task(self):
        self.assertFalse(self._emitter().observe("info score cp -1200", None))


class StubEngine:
    """EngineProxy の代役 (復旧経路をプロセス無しで検証するため)。"""

    def __init__(self, on_read=None, restart_ok=True):
        self.sent: list[str] = []
        self.restarts = 0
        self.terminations = 0
        self.restart_ok = restart_ok
        self._on_read = on_read or (lambda: "")
        self._released = threading.Event()

    def start(self):
        return True

    def restart(self):
        self.restarts += 1
        self._released.clear()
        return (self.restart_ok() if callable(self.restart_ok)
                else self.restart_ok)

    def alive(self):
        return True

    def send(self, cmd):
        self.sent.append(cmd)

    def read_line(self):
        return self._on_read()

    def terminate(self):
        self.terminations += 1
        self._released.set()

    # -- ヘルパ --
    def block_until_terminated(self):
        """terminate されるまでブロックする read_line (無反応エンジン)。"""
        self._released.wait()
        return ""


def make_wrapper(tmpdir: str, engine=None):
    """実物の Wrapper を作り、エンジンとGUI出力だけ差し替える。"""
    config_path = os.path.join(tmpdir, "config.txt")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(f"BookDir {os.path.join(tmpdir, 'book')}\n"
                f"LogDir {os.path.join(tmpdir, 'logs')}\n")
    os.makedirs(os.path.join(tmpdir, "book"), exist_ok=True)
    cfg, _ = config_mod.load_config(config_path)
    wrapper = main_mod.Wrapper(cfg)
    wrapper.gui_lines: list[str] = []
    wrapper.send_gui = lambda *lines: wrapper.gui_lines.extend(lines)
    if engine is not None:
        wrapper.engine = engine
    return wrapper


class TestFileSwap(unittest.TestCase):
    """定跡を読んでいる最中でも差し替えられること。

    Windows の既定の open では読み取り中のファイルを置き換えられず、
    実運用で PermissionError が出た (生成側の os.replace が失敗)。
    読み手は共有削除つきで開き、書き手は ReplaceFileW を使う。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "book.db")
        with open(self.path, "wb") as f:
            f.write(b"OLD" * 10)

    def _write_tmp(self, data=b"NEW"):
        src = self.path + ".tmp"
        with open(src, "wb") as f:
            f.write(data * 10)
        return src

    def test_replace_while_reader_holds_file(self):
        from file_swap import open_shared, replace_file
        reader = open_shared(self.path)
        self.addCleanup(reader.close)
        self.assertEqual(reader.read(3), b"OLD")
        replace_file(self._write_tmp(), self.path)      # 例外が出ないこと
        # 読み手は古い実体を読み切れる (一貫したスナップショット)
        self.assertEqual(reader.read(3), b"OLD")
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(3), b"NEW")

    def test_replace_without_reader(self):
        from file_swap import replace_file
        replace_file(self._write_tmp(), self.path)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(3), b"NEW")

    def test_replace_when_target_missing(self):
        from file_swap import replace_file
        os.remove(self.path)
        replace_file(self._write_tmp(), self.path)
        self.assertTrue(os.path.exists(self.path))

    def test_open_shared_reads_normally(self):
        from file_swap import open_shared
        with open_shared(self.path) as f:
            self.assertEqual(f.read(), b"OLD" * 10)

    def test_book_probe_uses_shared_open(self):
        """定跡 probe が開いている間も置き換えられること (実物の経路)。"""
        from book_repo import open_checked_probe
        from file_swap import replace_file
        book = os.path.join(self.tmp.name, "100_x_t.db")
        start = board_sfen_main(cshogi.Board())
        write_db(book, {start: ["7g7f none 1 32"]})
        probe = open_checked_probe(book)
        self.addCleanup(probe.close)
        self.assertIsNotNone(probe.probe(start))
        src = book + ".tmp"
        write_db(src, {start: ["2g2f none 2 32"]})
        replace_file(src, book)                      # 例外が出ないこと
        with open(book, encoding="utf-8") as f:
            self.assertIn("2g2f", f.read())


class TestEngineProxyThreadSafety(unittest.TestCase):
    """監視タイマー(別スレッド)の terminate と参照が競合しても、
    捕捉されない例外を出さないこと (process 差し替えの TOCTOU)。"""

    def _racy(self, none_at: int):
        class Stdout:
            def readline(self):
                return "x\n"

        class Stdin:
            def write(self, s):
                pass

            def flush(self):
                pass

        class Proc:
            stdout = Stdout()
            stdin = Stdin()

            def poll(self):
                return None

        class Racy(engine_proxy.EngineProxy):
            n = 0

            @property
            def process(self):
                type(self).n += 1
                return None if type(self).n >= none_at else Proc()

            @process.setter
            def process(self, value):
                pass

        return Racy("dummy", log=lambda msg: None)

    def test_read_line_and_send_survive_concurrent_terminate(self):
        for none_at in (1, 2, 3, 4):
            with self.subTest(none_at=none_at):
                self._racy(none_at).read_line()      # 例外が出ないこと
                self._racy(none_at).send("usi")


class TestRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_watchdog_releases_unresponsive_engine(self):
        """usiokを返さないエンジンで RECOVERY に籠城しないこと。"""
        engine = StubEngine()
        engine._on_read = engine.block_until_terminated
        w = make_wrapper(self.tmp.name, engine)
        start = time.time()
        self.assertFalse(w._wait_token("usiok", 0.5))
        self.assertLess(time.time() - start, 3.0)
        self.assertEqual(engine.terminations, 1)

    def test_watchdog_does_not_kill_after_token_arrives(self):
        """tokenが来た後にタイマーが発火しても、正常なエンジンを殺さない。"""
        engine = StubEngine(on_read=lambda: "usiok\n")
        w = make_wrapper(self.tmp.name, engine)
        self.assertTrue(w._wait_token("usiok", 0.05))
        time.sleep(0.3)                       # タイマー発火時刻を跨ぐ
        self.assertEqual(engine.terminations, 0)

    def test_recover_retries_then_gives_up(self):
        engine = StubEngine(on_read=lambda: "")   # 即EOF = ハンドシェイク失敗
        w = make_wrapper(self.tmp.name, engine)
        w.last_go_cmd = "go btime 1000"
        with mock.patch.object(main_mod.time, "sleep"):
            self.assertFalse(w._recover(resend_go=True))
        self.assertEqual(engine.restarts, main_mod.RECOVERY_ATTEMPTS)
        self.assertEqual(w.state, main_mod.IDLE)

    def test_recover_succeeds_on_later_attempt(self):
        """1回目の起動に失敗しても、リトライで復旧すること
        (1発勝負で対局を投げない)。"""
        engine = StubEngine()
        engine.restart_ok = lambda: engine.restarts >= 2
        engine._on_read = lambda: ("usiok\n" if engine.restarts >= 2
                                   else "")
        replies = ["usiok\n", "readyok\n"]
        engine._on_read = lambda: (replies.pop(0) if engine.restarts >= 2
                                   and replies else "")
        w = make_wrapper(self.tmp.name, engine)
        w.last_go_cmd = "go btime 1000"
        with mock.patch.object(main_mod.time, "sleep"):
            self.assertTrue(w._recover(resend_go=True))
        self.assertEqual(engine.restarts, 2)
        self.assertEqual(w.state, main_mod.SEARCH)
        self.assertIn("go btime 1000", engine.sent)

    def test_reader_survives_exception_and_resigns_when_giving_up(self):
        """read_line/EOF経路で例外が続いても reader は無音で死なず、
        探索中なら投了で GUI を解放すること。"""
        def boom():
            raise AttributeError("'NoneType' object has no attribute 'stdout'")

        engine = StubEngine(on_read=boom)
        w = make_wrapper(self.tmp.name, engine)
        w.state = main_mod.SEARCH
        with mock.patch.object(main_mod.time, "sleep"):
            thread = threading.Thread(target=w._engine_reader, daemon=True)
            thread.start()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "readerが停止しない")
        self.assertEqual(w.gui_lines, ["bestmove resign"])
        self.assertEqual(w.state, main_mod.IDLE)

    def test_reader_recovers_after_transient_error(self):
        """一時的な例外の後も中継を続けること (1回で諦めない)。"""
        seq = [lambda: (_ for _ in ()).throw(RuntimeError("transient")),
               lambda: "info string hello\n",
               lambda: ""]

        def on_read():
            return seq.pop(0)() if seq else ""

        engine = StubEngine(on_read=on_read)
        w = make_wrapper(self.tmp.name, engine)
        with mock.patch.object(main_mod.time, "sleep"):
            thread = threading.Thread(target=w._engine_reader, daemon=True)
            thread.start()
            thread.join(timeout=10)
        self.assertIn("info string hello", w.gui_lines)


class TestMainLoopFailSafe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_command_exception_does_not_kill_process(self):
        """コマンド処理の例外でラッパーごと落ちないこと。"""
        w = make_wrapper(self.tmp.name, StubEngine())
        calls = []

        def flaky(cmd, parts, line):
            calls.append(cmd)
            if cmd == "boom":
                raise RuntimeError("internal")
            return cmd != "quit"

        w._handle_gui_command = flaky
        w.engine.terminate = lambda: None
        with mock.patch.object(main_mod.sys, "stdin",
                               io_lines(["boom", "isready", "quit"])):
            w.run()
        self.assertEqual(calls, ["boom", "isready", "quit"])

    def test_go_failure_resigns_instead_of_hanging(self):
        """go 処理が失敗したら、GUIをbestmove待ちで固めず投了する。"""
        w = make_wrapper(self.tmp.name, StubEngine())
        w._handle_go_impl = lambda parts, line: (_ for _ in ()).throw(
            RuntimeError("boom"))
        w._handle_go(["go", "btime", "1000"], "go btime 1000")
        self.assertIn("bestmove resign", w.gui_lines)


def io_lines(lines):
    """sys.stdin の代役 (readline で1行ずつ返す)。"""
    import io
    return io.StringIO("".join(line + "\n" for line in lines))


if __name__ == "__main__":
    unittest.main()
