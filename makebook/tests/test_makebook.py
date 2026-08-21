"""定跡修正システムのユニットテスト。

もっとも大事なのは局面キーが KiriCompass csa.db と一致することで、
ここが狂うと全部が静かにずれる。実 db がある環境ではそれも検証する。
"""

import os
import struct
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import cshogi  # noqa: E402

import common  # noqa: E402
from book_repo import board_sfen_main  # noqa: E402
from common import (Config, ConfigError, LearningBook,  # noqa: E402
                    move16_to_usi, position_key)
from csa_source import GameRecord, SeenStore  # noqa: E402
from cycle import BLACK, WHITE, CycleRunner, GameOutcome, PlayedMove  # noqa
from problem import (PositionError, derive_problem,  # noqa: E402
                     open_book_repo, parse_position_command)
from tasks import KIND_KIFU, KIND_PROBLEM, TaskQueue  # noqa: E402
from usi_engine import SearchResult, _parse_info  # noqa: E402

CSA_DB = r"C:\shogi\KiriCompass\data\csa.db"


def board_from(moves: str = "") -> cshogi.Board:
    board = cshogi.Board()
    for token in moves.split():
        board.push_usi(token)
    return board


def write_db(path: str, blocks: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("#YANEURAOU-DB2016 1.00\n")
        for sfen in sorted(blocks):
            f.write(f"sfen {sfen} 0\n")
            for line in blocks[sfen]:
                f.write(line + "\n")


class TestPositionKey(unittest.TestCase):
    """csa.db との互換性 (ここが最重要)。"""

    def test_key_is_stable(self):
        key = position_key("lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/"
                           "1B5R1/LNSGKGSNL b -")
        self.assertEqual(key, position_key(board_sfen_main(cshogi.Board())))
        self.assertIsInstance(key, int)
        self.assertGreaterEqual(key, -(1 << 63))
        self.assertLess(key, 1 << 63)

    def test_move16_roundtrip(self):
        for usi in ("7g7f", "2b3a", "8h2b+", "P*5e", "R*8b", "1a1b"):
            code = _usi_to_move16(usi)
            self.assertEqual(move16_to_usi(code), usi)

    @unittest.skipUnless(os.path.exists(CSA_DB), "csa.db がない環境")
    def test_matches_real_csa_db(self):
        """cshogi で作ったキーが実 csa.db の position_games と一致すること。"""
        import sqlite3
        conn = sqlite3.connect(
            "file:" + CSA_DB.replace("\\", "/") + "?mode=ro", uri=True)
        row = conn.execute(
            "SELECT game_id, moves FROM games WHERE initial_sfen IS NULL "
            "ORDER BY game_id DESC LIMIT 1").fetchone()
        game_id, blob = row
        codes = struct.unpack(f"<{len(blob) // 2}H", blob)
        board = cshogi.Board()
        checked = 0
        for ply, code in enumerate(codes[:30]):
            key = position_key(board_sfen_main(board))
            found = conn.execute(
                "SELECT next_move FROM position_games "
                "WHERE position_key=? AND game_id=? AND ply=?",
                (key, game_id, ply)).fetchone()
            self.assertIsNotNone(found, f"ply {ply} の局面キーが一致しない")
            self.assertEqual(move16_to_usi(found[0]), move16_to_usi(code))
            board.push_usi(move16_to_usi(code))
            checked += 1
        conn.close()
        self.assertEqual(checked, 30)


def _usi_to_move16(usi: str) -> int:
    """テスト用の逆変換 (KiriCompass 形式)。"""
    if usi[1] == "*":
        to_sq = (int(usi[2]) - 1) * 9 + (ord(usi[3]) - ord("a"))
        return to_sq | ((81 + "PLNSGBR".index(usi[0])) << 7)
    frm = (int(usi[0]) - 1) * 9 + (ord(usi[1]) - ord("a"))
    to_sq = (int(usi[2]) - 1) * 9 + (ord(usi[3]) - ord("a"))
    return to_sq | (frm << 7) | ((1 << 14) if usi.endswith("+") else 0)


class TestPositionParsing(unittest.TestCase):
    """手入力の取り違えを弾く (合法性検証)。"""

    def test_startpos_and_moves(self):
        board = parse_position_command("startpos moves 7g7f 3c3d")
        self.assertEqual(board.move_number, 3)

    def test_position_prefix_allowed(self):
        board = parse_position_command("position startpos moves 7g7f")
        self.assertEqual(board.move_number, 2)

    def test_sfen_start(self):
        sfen = board_from("7g7f 3c3d").sfen()
        board = parse_position_command(f"sfen {sfen} moves 2g2f")
        self.assertEqual(board_sfen_main(board),
                         board_sfen_main(board_from("7g7f 3c3d 2g2f")))

    def test_illegal_move_is_rejected_with_index(self):
        with self.assertRaises(PositionError) as ctx:
            parse_position_command("startpos moves 7g7f 3c3d 7g7f")
        self.assertIn("3手目", str(ctx.exception))

    def test_garbage_move_is_rejected(self):
        with self.assertRaises(PositionError):
            parse_position_command("startpos moves 7g7f zz9z")

    def test_bad_prefix_and_empty(self):
        for text in ("", "moves 7g7f", "startpos 7g7f"):
            with self.assertRaises(PositionError):
                parse_position_command(text)


class TestSfenGuard(unittest.TestCase):
    """cshogi の set_sfen は不正SFENでプロセスごと落ちるため、
    渡す前に必ず弾く (例外にならないので try/except では守れない)。"""

    def test_valid_sfens_pass(self):
        from sfen_guard import validate_sfen
        for sfen in (
            "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b - 1",
            "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b -",
            "l7l/2k1g4/2ns1+S1p1/p1ppp4/6BPP/P1PP5/2N1P+nP2/5R3/L+r1K4L "
            "b B2GSN4Pgs2p 0",
            board_from("7g7f 3c3d 8h2b+ 3a2b").sfen(),
        ):
            self.assertIsNone(validate_sfen(sfen), sfen)

    def test_short_rank_is_rejected(self):
        """実際にプロセスを落とした形 (段が8マス)。"""
        from sfen_guard import validate_sfen
        error = validate_sfen(
            "lnsgkgsnl/6b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b - 1")
        self.assertIsNotNone(error)
        self.assertIn("マス数", error)

    def test_various_broken_sfens(self):
        from sfen_guard import validate_sfen
        base = "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL"
        cases = [
            (f"{base} b", "項目が足りません"),
            (f"{base}/9 b - 1", "9段"),
            (f"{base} x - 1", "手番"),
            (f"{base} b Z 1", "持駒"),
            (f"{base} b - x", "手数"),
            ("lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSN0 b - 1",
             "0"),
            ("lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSN+ b - 1",
             "'+'"),
            ("+Ksgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b - 1",
             "成れません"),
        ]
        for sfen, hint in cases:
            error = validate_sfen(sfen)
            self.assertIsNotNone(error, sfen)
            self.assertIn(hint, error, sfen)

    def test_parse_position_rejects_broken_sfen(self):
        with self.assertRaises(PositionError) as ctx:
            parse_position_command(
                "sfen lnsgkgsnl/6b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/"
                "LNSGKGSNL b - 1")
        self.assertIn("sfen が不正", str(ctx.exception))

    def test_safe_set_sfen_leaves_board_untouched(self):
        from sfen_guard import safe_set_sfen
        board = cshogi.Board()
        before = board.sfen()
        error = safe_set_sfen(board, "bad/sfen b - 1")
        self.assertIsNotNone(error)
        self.assertEqual(board.sfen(), before)


class TestLearningBook(unittest.TestCase):
    def test_roundtrip_and_wrapper_can_read(self):
        book = LearningBook(value_mode="count", depth=32)
        start = board_sfen_main(cshogi.Board())
        book.add(start, "7g7f", 3)
        book.add(start, "2g2f", 1)
        book.add(board_sfen_main(board_from("7g7f")), "3c3d")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "100_x_test.db")
            book.save(path)
            again = LearningBook.load(path)
            self.assertEqual(again.get(start), {"7g7f": 3, "2g2f": 1})
            # ラッパーが読めること (value=count なので採用回数の多い手が選ばれる)
            import random
            from selector import make_tie_breaker
            repo = open_book_repo(d, 999)
            hit, _ = repo.probe(cshogi.Board(), 1, make_tie_breaker("deep"),
                                random.Random(0))
            self.assertIsNotNone(hit)
            self.assertEqual(hit.usi, "7g7f")

    def test_remove_drops_empty_position(self):
        book = LearningBook()
        start = board_sfen_main(cshogi.Board())
        book.add(start, "7g7f")
        self.assertTrue(book.remove(start, "7g7f"))
        self.assertEqual(len(book), 0)
        self.assertFalse(book.remove(start, "7g7f"))

    def test_value_column_is_not_the_count(self):
        """採用回数を value 欄に書かない。

        value はエンジンの評価値の欄。やねうら王は ±32000
        (VALUE_MATE) を詰みとして扱うので、値に大きく寄与する数を
        書くと詰みと見分けが付かない。
        """
        book = LearningBook(value_mode="zero", depth=32)
        start = board_sfen_main(cshogi.Board())
        book.add(start, "2g2f", 75000)
        line = [l for l in book.to_text().splitlines()
                if l.startswith("2g2f")][0]
        self.assertEqual(line, "2g2f none 0 32 75000")   # value=0, count=75000
        self.assertNotIn("32000", line)

    def test_count_mode_is_clamped_below_mate(self):
        """count モードを使う場合でも詰み値の帯には入れない。"""
        book = LearningBook(value_mode="count", depth=32)
        book.add(board_sfen_main(cshogi.Board()), "2g2f", 75000)
        line = [l for l in book.to_text().splitlines()
                if l.startswith("2g2f")][0]
        self.assertEqual(line, "2g2f none 9999 32 75000")

    def test_large_counts_survive_roundtrip(self):
        book = LearningBook(value_mode="zero", depth=32)
        start = board_sfen_main(cshogi.Board())
        book.add(start, "2g2f", 97000)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "100_x_t.db")
            book.save(path)
            again = LearningBook.load(path)
            self.assertEqual(again.get(start), {"2g2f": 97000})

    def test_sorted_output(self):
        book = LearningBook()
        for moves in ("7g7f", "2g2f", ""):
            book.add(board_sfen_main(board_from(moves)), "1g1f")
        lines = [ln for ln in book.to_text().splitlines()
                 if ln.startswith("sfen ")]
        self.assertEqual(lines, sorted(lines))


class TestProblemDerivation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_last_book_hit_defines_problem(self):
        """終局側から見て最後に採用手がある局面の『適用後』が課題局面。"""
        write_db(os.path.join(self.tmp.name, "000_b_abs.db"), {
            board_sfen_main(cshogi.Board()): ["7g7f none 0 32"],
            board_sfen_main(board_from("7g7f 3c3d")): ["2g2f none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        moves = "7g7f 3c3d 2g2f 8c8d 2f2e".split()
        problem = derive_problem(cshogi.Board(), moves, repo, 1, "deep", "t")
        self.assertEqual(problem.source_move, "2g2f")
        self.assertEqual(problem.sfen_main,
                         board_sfen_main(board_from("7g7f 3c3d 2g2f")))

    def test_side_of_position_does_not_matter(self):
        """先後どちらの局面でも、最後のヒットが採られること。"""
        write_db(os.path.join(self.tmp.name, "000_w_abs.db"), {
            board_sfen_main(board_from("7g7f")): ["3c3d none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        problem = derive_problem(cshogi.Board(), "7g7f 3c3d 2g2f".split(),
                                 repo, 1, "deep", "t")
        self.assertEqual(problem.sfen_main,
                         board_sfen_main(board_from("7g7f 3c3d")))

    def test_actual_move_is_applied_not_book_move(self):
        """定跡手ではなく**実際に指された手**を適用する。

        定跡手を使うと自分が本線だと思っている変化しか掘れず、
        相手が外してきた変化を調べられない。
        """
        write_db(os.path.join(self.tmp.name, "000_b_abs.db"), {
            board_sfen_main(cshogi.Board()): ["7g7f none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        # 定跡は 7g7f だが、実戦は 2g2f を指した
        problem = derive_problem(cshogi.Board(), "2g2f 3c3d".split(),
                                 repo, 1, "deep", "t")
        self.assertEqual(problem.source_move, "2g2f")     # 実戦の手
        self.assertEqual(problem.book_move, "7g7f")       # 定跡の採用手
        self.assertEqual(problem.sfen_main,
                         board_sfen_main(board_from("2g2f")))

    def test_book_move_used_when_kifu_ends_there(self):
        """棋譜がそこで終わっているときだけ定跡手を使う。"""
        write_db(os.path.join(self.tmp.name, "000_b_abs.db"), {
            board_sfen_main(board_from("2g2f 3c3d")): ["7g7f none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        problem = derive_problem(cshogi.Board(), "2g2f 3c3d".split(),
                                 repo, 1, "deep", "t")
        self.assertEqual(problem.source_move, "7g7f")
        self.assertEqual(problem.book_move, "7g7f")
        self.assertEqual(problem.sfen_main,
                         board_sfen_main(board_from("2g2f 3c3d 7g7f")))

    def test_problem_is_always_in_the_source_kifu(self):
        """実戦の手を使うので、課題局面は必ず由来棋譜に現れる。

        (定跡手を使っていたときは「課題局面が現れない」で由来棋譜を
        取り込めないことがあった)
        """
        write_db(os.path.join(self.tmp.name, "000_b_abs.db"), {
            board_sfen_main(cshogi.Board()): ["7g7f none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        moves = "2g2f 3c3d 2f2e".split()
        problem = derive_problem(cshogi.Board(), moves, repo, 1, "deep", "t")
        board = cshogi.Board()
        seen = [board_sfen_main(board)]
        for usi in problem.source_moves:
            board.push_usi(usi)
            seen.append(board_sfen_main(board))
        self.assertIn(problem.sfen_main, seen)

    def test_deviation_is_judged_by_candidate_set(self):
        """「定跡どおりか」は候補集合で見る。

        タイブレークで選ばれた1手と比べると、意図的に複数登録した
        局面で「別の候補を指しただけ」まで外された変化になってしまう。
        """
        write_db(os.path.join(self.tmp.name, "000_b_abs.db"), {
            board_sfen_main(cshogi.Board()): ["7g7f none 0 32",
                                              "2g2f none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        for played in ("7g7f", "2g2f"):
            problem = derive_problem(cshogi.Board(), [played, "3c3d"],
                                     repo, 1, "deep", "t")
            self.assertEqual(set(problem.book_candidates), {"7g7f", "2g2f"})
            self.assertIn(problem.source_move, problem.book_candidates,
                          f"{played} は登録済みなので定跡どおり")
        # 候補に無い手なら外れたと判定される
        problem = derive_problem(cshogi.Board(), ["1g1f", "3c3d"],
                                 repo, 1, "deep", "t")
        self.assertNotIn(problem.source_move, problem.book_candidates)

    def test_single_candidate_still_detects_deviation(self):
        write_db(os.path.join(self.tmp.name, "000_b_abs.db"), {
            board_sfen_main(cshogi.Board()): ["7g7f none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        problem = derive_problem(cshogi.Board(), ["2g2f"], repo, 1, "deep", "t")
        self.assertEqual(problem.book_candidates, ("7g7f",))
        self.assertNotIn(problem.source_move, problem.book_candidates)

    def test_no_hit_falls_back_to_start(self):
        repo = open_book_repo(self.tmp.name, 100)
        problem = derive_problem(cshogi.Board(), ["7g7f"], repo, 1, "deep", "t")
        self.assertEqual(problem.sfen_main, board_sfen_main(cshogi.Board()))
        self.assertIn("定跡ヒットなし", problem.origin)

    def test_priority_filter_excludes_lower_books(self):
        write_db(os.path.join(self.tmp.name, "900_x_deep.db"), {
            board_sfen_main(board_from("7g7f 3c3d")): ["2g2f none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        self.assertEqual(repo.files, [])

    def test_target_book_itself_is_excluded(self):
        """対象定跡自身は判定に使わない。

        含めると、サイクルが書き足した局面がフロンティアとして拾われ、
        課題局面が際限なく奥へ移動してしまう。
        """
        # 対象定跡 (優先度100) にだけ深い局面がある状態を作る
        write_db(os.path.join(self.tmp.name, "100_x_target.db"), {
            board_sfen_main(board_from("7g7f 3c3d 2g2f 8c8d")):
                ["2f2e none 0 32"],
        })
        write_db(os.path.join(self.tmp.name, "000_b_abs.db"), {
            board_sfen_main(cshogi.Board()): ["7g7f none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        self.assertEqual([f.name for f in repo.files], ["000_b_abs.db"])
        moves = "7g7f 3c3d 2g2f 8c8d 2f2e 8d8e".split()
        problem = derive_problem(cshogi.Board(), moves, repo, 1, "deep", "t")
        # 100_x の深い局面ではなく、000_b の初手の直後が課題局面になる
        self.assertEqual(problem.source_book, "000_b_abs.db")
        self.assertEqual(problem.sfen_main,
                         board_sfen_main(board_from("7g7f")))

    def test_target_priority_from_filename(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("TargetBook 250_x_mybook.db\n")
            config, _ = Config.load(path)
            self.assertEqual(config.target_priority(), 250)

    def test_bad_target_book_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("TargetBook mybook.db\n")
            with self.assertRaises(ConfigError):
                Config.load(path)


class TestTaskQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = TaskQueue(self.tmp.name)

    def test_fifo_and_complete(self):
        self.queue.push(KIND_PROBLEM, {"position": "startpos"}, "a")
        self.queue.push(KIND_KIFU, {"position": "startpos moves 7g7f"}, "b")
        self.assertEqual(self.queue.count(), 2)
        first = self.queue.take()
        self.assertEqual(first.origin, "a")
        self.queue.complete(first)
        self.assertEqual(self.queue.count(), 1)
        second = self.queue.take()
        self.assertEqual(second.kind, KIND_KIFU)
        self.queue.fail(second, "理由")
        self.assertEqual(self.queue.count(), 0)

    def test_take_leaves_file_for_retry(self):
        self.queue.push(KIND_PROBLEM, {"position": "startpos"})
        self.queue.take()
        self.assertEqual(self.queue.count(), 1)   # 完了するまで消えない

    def test_broken_file_goes_to_error(self):
        with open(os.path.join(self.tmp.name, "20260101-x-kifu.json"),
                  "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertIsNone(self.queue.take())
        self.assertEqual(self.queue.count(), 0)


def build_csa_db(path, rows):
    """テスト用の最小 csa.db。rows = (game_id, started_at, black, white, result)"""
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE games (game_id INTEGER PRIMARY KEY, event TEXT NOT NULL"
        " UNIQUE, source TEXT NOT NULL, started_at TEXT, black_name TEXT"
        " NOT NULL, white_name TEXT NOT NULL, result INTEGER, end_reason TEXT"
        " NOT NULL, ply_count INTEGER NOT NULL, initial_sfen TEXT,"
        " moves BLOB NOT NULL)")
    conn.execute(
        "CREATE TABLE position_games (position_key INTEGER NOT NULL,"
        " sort_key INTEGER NOT NULL, game_id INTEGER NOT NULL,"
        " ply INTEGER NOT NULL, next_move INTEGER NOT NULL,"
        " PRIMARY KEY (position_key, sort_key, game_id, ply)) WITHOUT ROWID")
    for game_id, started, black, white, result in rows:
        conn.execute(
            "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (game_id, f"e{game_id}", "floodgate", started, black, white,
             result, "toryo", 1, None, b""))
    conn.commit()
    conn.close()


class TestNameSearch(unittest.TestCase):
    """タスク補充は「自分が負けた対局」だけを引く。

    直したいのは負けた定跡であり、勝ち局を積んでもキューが伸びるだけ。
    千日手は DrawWinner の裏側 (既定では先手) にとって負け扱い。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "csa.db")
        build_csa_db(self.path, [
            # (id, 開始時刻, 先手, 後手, result)  1=先手勝 2=後手勝 0=引分
            (1, "2026-01-09", "MyBot_x", "opponent", 2),   # 先手で負け ○
            (2, "2026-01-08", "opponent", "MyBot_x", 1),   # 後手で負け ○
            (3, "2026-01-07", "MyBot_x", "opponent", 1),   # 先手で勝ち ×
            (4, "2026-01-06", "opponent", "MyBot_x", 2),   # 後手で勝ち ×
            (5, "2026-01-05", "MyBot_x", "opponent", 0),   # 先手で千日手 ○
            (6, "2026-01-04", "MyBot_x", "opponent", None),  # 結果なし ×
            (7, "2026-01-03", "other", "another", 1),      # 名前が違う ×
            (8, "2026-01-02", "opponent", "MyBot_x", 1),   # 後手で負け ○
            (9, "2026-01-01", "opponent", "MyBot_x", 0),   # 後手で千日手 ×
        ])
        from csa_source import CsaSource
        self.source = CsaSource(self.path)
        self.addCleanup(self.source.close)

    def test_only_losses_newest_first(self):
        refs = self.source.games_by_name(["MyBot"], 10)
        self.assertEqual([r.game_id for r in refs], [1, 2, 5, 8])

    def test_black_draw_is_a_loss_but_white_draw_is_not(self):
        """先手は千日手を避けたいので負け扱い。後手は千日手歓迎なので違う。"""
        found = {r.game_id for r in self.source.games_by_name(["MyBot"], 10)}
        self.assertIn(5, found)        # 先手で千日手
        self.assertNotIn(9, found)     # 後手で千日手

    def test_draw_loser_follows_setting(self):
        refs = self.source.games_by_name(["MyBot"], 10, draw_loser="w")
        self.assertIn(9, [r.game_id for r in refs])
        self.assertNotIn(5, [r.game_id for r in refs])
        refs = self.source.games_by_name(["MyBot"], 10, draw_loser="")
        ids = [r.game_id for r in refs]
        self.assertNotIn(5, ids)
        self.assertNotIn(9, ids)

    def test_wins_and_unfinished_are_excluded(self):
        found = {r.game_id for r in self.source.games_by_name(["MyBot"], 10)}
        for game_id in (3, 4, 6, 7):
            self.assertNotIn(game_id, found)

    def test_limit_and_seen_skip(self):
        self.assertEqual(
            [r.game_id for r in self.source.games_by_name(["MyBot"], 1)], [1])

        class Seen:
            def is_seen(self, game_id):
                return game_id in (1, 2)

        refs = self.source.games_by_name(["MyBot"], 10, Seen())
        self.assertEqual([r.game_id for r in refs], [5, 8])

    def test_losses_only_can_be_disabled(self):
        refs = self.source.games_by_name(["MyBot"], 10, losses_only=False)
        self.assertEqual([r.game_id for r in refs], [1, 2, 3, 4, 5, 6, 8, 9])

    def test_multiple_filters(self):
        refs = self.source.games_by_name(["MyBot", "another"], 10)
        # game 7 は 'another' が後手で先手勝ち = 敗戦なので入る
        self.assertEqual([r.game_id for r in refs], [1, 2, 5, 7, 8])

    def test_loser_side(self):
        from csa_source import GameRecord
        black_loss = GameRecord(1, "e", "", "MyBot_x", "opponent", 2, None, [])
        white_loss = GameRecord(2, "e", "", "opponent", "MyBot_x", 1, None, [])
        black_draw = GameRecord(3, "e", "", "MyBot_x", "opponent", 0, None, [])
        white_draw = GameRecord(4, "e", "", "opponent", "MyBot_x", 0, None, [])
        win = GameRecord(5, "e", "", "MyBot_x", "opponent", 1, None, [])
        self.assertEqual(self.source.loser_side(black_loss, ["MyBot"]), "先手")
        self.assertEqual(self.source.loser_side(white_loss, ["MyBot"]), "後手")
        self.assertEqual(self.source.loser_side(black_draw, ["MyBot"]), "先手")
        self.assertEqual(self.source.loser_side(white_draw, ["MyBot"]), "")
        self.assertEqual(self.source.loser_side(win, ["MyBot"]), "")


class TestInstanceLock(unittest.TestCase):
    """二重起動の防止。2プロセスが state/定跡を書くと必ず壊れる。"""

    def test_second_acquire_is_refused(self):
        from common import InstanceLock
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.lock")
            first, second = InstanceLock(path), InstanceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())     # 動作中は取れない
            first.release()
            self.assertTrue(second.acquire())      # 終了後は取れる
            second.release()

    def test_release_without_acquire_is_safe(self):
        from common import InstanceLock
        InstanceLock(os.path.join(tempfile.gettempdir(), "x.lock")).release()


class TestHealing(unittest.TestCase):
    """壊れた value を持つ定跡でも、読み込み→保存で自然に治ること。

    LearningBook は value 欄を読まず count を重みに使うため、
    旧プロセスが書いた 32000 入りのファイルが残っていても、
    次の保存で value=0 に戻る。
    """

    def test_corrupted_values_heal_on_save(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.db")
            start = board_sfen_main(cshogi.Board())
            with open(bad, "w", encoding="utf-8") as f:
                f.write("#YANEURAOU-DB2016 1.00\n"
                        f"sfen {start} 0\n"
                        "8b8a none 32000 32 74000\n")
            book = LearningBook.load(bad, "zero", 32)
            out = os.path.join(d, "healed.db")
            book.save(out)
            with open(out, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn(" 32000 ", text)
            self.assertIn("8b8a none 0 32 74000", text)   # countは保持


class TestDrawLoserSetting(unittest.TestCase):
    def test_follows_draw_winner(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("DrawWinner white\n")
            config, _ = Config.load(path)
            self.assertEqual(config.draw_loser(), "b")
            with open(path, "w", encoding="utf-8") as f:
                f.write("DrawWinner black\n")
            config, _ = Config.load(path)
            self.assertEqual(config.draw_loser(), "w")


class TestSeenStore(unittest.TestCase):
    def test_mark_and_skip(self):
        with tempfile.TemporaryDirectory() as d:
            store = SeenStore(os.path.join(d, "state.db"))
            self.assertFalse(store.is_seen(42))
            store.mark([42, 43], "pos")
            self.assertTrue(store.is_seen(42))
            store.mark([42], "again")          # 重複は無害
            self.assertEqual(store.count(), 2)
            store.close()


class StubEngine:
    """score を順に返すだけのエンジン代役。

    要素は score(int) か (score, bestmove) のタプル。
    """

    def __init__(self, scores):
        self.scores = list(scores)
        self.calls = []

    def search(self, position_cmd, nodes, timeout=0):
        self.calls.append(position_cmd)
        item = self.scores.pop(0) if self.scores else 0
        if isinstance(item, tuple):
            score, bestmove = item
        else:
            score, bestmove = item, "7g7f"
        return SearchResult(bestmove, None, score, False, nodes, 0.0)

    def set_option(self, *a):
        pass

    def isready(self):
        pass

    def alive(self):
        return True


class StubSource:
    def __init__(self, record):
        self.record = record

    def game(self, game_id):
        return self.record


def make_runner(tmpdir, book, engine, source=None, max_ply=320):
    config_path = os.path.join(tmpdir, "config.txt")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(f"BookDir {tmpdir}\nLogDir {tmpdir}\n"
                f"CsaDbPath {tmpdir}\nDecisiveEval 1000\n"
                f"AnalysisNodes 1000\nWinStreak 2\n"
                f"SelfPlayNodes 1000\nResignValue 400\n"
                f"MaxPly {max_ply}\n")
    config, _ = Config.load(config_path)
    return CycleRunner(config, book, engine, source, None, tmpdir,
                       report=lambda msg: None)


class TestPrecedentImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_registers_only_winner_moves_up_to_decisive(self):
        """決着水準に達した時点で打ち切り、勝ち側の手だけ登録する。"""
        moves = "7g7f 3c3d 2g2f 8c8d 2f2e".split()
        record = GameRecord(1, "e", "", "b", "w", 2, None, moves)
        # 課題局面 = 初期局面。ply0..3 は互角、ply4 で先手 +1500 → 先手勝ち扱い
        engine = StubEngine([0, 0, 0, 0, 1500])
        book = LearningBook()
        from problem import Problem
        problem = Problem(cshogi.Board().sfen(),
                          board_sfen_main(cshogi.Board()), "t")
        runner = make_runner(self.tmp.name, book, engine, StubSource(record))
        from csa_source import GameRef
        added, removed, reason = runner._import_one(GameRef(1, 0, 0),
                                                    problem, 1, 1)
        self.assertEqual(reason, "")
        # 先手の手 (ply0=7g7f, ply2=2g2f) のみ。ply4 は決着扱いで登録しない
        self.assertEqual(added, 2)
        self.assertEqual(book.get(board_sfen_main(cshogi.Board())),
                         {"7g7f": 1})
        self.assertEqual(book.get(board_sfen_main(board_from("7g7f 3c3d"))),
                         {"2g2f": 1})
        # 後手の手は入らない
        self.assertEqual(book.get(board_sfen_main(board_from("7g7f"))), {})

    def test_same_position_keeps_last_move_only(self):
        """検討区間に同一局面が2回出たら、最後の指し手だけを加算する
        (1つの棋譜から同じ局面に2手入れない)。"""
        # 7g7f 3c3d の後、双方の飛車往復で同じ局面へ戻る
        moves = "7g7f 3c3d 2h3h 8b7b 3h2h 7b8b 2g2f 8c8d".split()
        record = GameRecord(1, "e", "", "b", "w", 1, None, moves)
        # ply7 (後手番) で先手 +1500 相当 → 先手勝ち扱い
        engine = StubEngine([0] * 7 + [-1500])
        book = LearningBook()
        from problem import Problem
        problem = Problem(cshogi.Board().sfen(),
                          board_sfen_main(cshogi.Board()), "t")
        runner = make_runner(self.tmp.name, book, engine, StubSource(record))
        from csa_source import GameRef
        runner._import_one(GameRef(1, 0, 0), problem, 1, 1)
        repeated = board_sfen_main(board_from("7g7f 3c3d"))
        self.assertEqual(book.get(repeated), {"2g2f": 1})   # 2h3h ではない
        self.assertEqual(book.get(board_sfen_main(cshogi.Board())),
                         {"7g7f": 1})

    def test_starts_from_last_occurrence_of_problem(self):
        """課題局面が複数回現れる棋譜では、最後の出現から検討する。"""
        moves = "2h3h 8b7b 3h2h 7b8b 7g7f 3c3d".split()
        record = GameRecord(1, "e", "", "b", "w", 1, None, moves)
        engine = StubEngine([0, -1500])
        book = LearningBook()
        from problem import Problem
        problem = Problem(cshogi.Board().sfen(),
                          board_sfen_main(cshogi.Board()), "t")
        runner = make_runner(self.tmp.name, book, engine, StubSource(record))
        from csa_source import GameRef
        runner._import_one(GameRef(1, 0, 0), problem, 1, 1)
        # 最初の検討は ply4 (飛車が戻った後) の局面から
        self.assertEqual(len(engine.calls[0].split(" moves ")[1].split()), 4)
        self.assertEqual(book.get(board_sfen_main(cshogi.Board())),
                         {"7g7f": 1})

    def test_no_decision_registers_nothing(self):
        """指し継ぎでも決着しなければ (非合法手が返る等) 何も登録しない。"""
        moves = "7g7f 3c3d".split()
        record = GameRecord(1, "e", "", "b", "w", 1, None, moves)
        engine = StubEngine([0, 0, (0, "7g7f")])   # 指し継ぎは非合法手
        book = LearningBook()
        from problem import Problem
        problem = Problem(cshogi.Board().sfen(),
                          board_sfen_main(cshogi.Board()), "t")
        runner = make_runner(self.tmp.name, book, engine, StubSource(record))
        from csa_source import GameRef
        added, removed, reason = runner._import_one(GameRef(1, 0, 0),
                                                    problem, 1, 1)
        self.assertEqual((added, removed), (0, 0))
        self.assertEqual(reason, "決着せず")
        self.assertEqual(len(book), 0)


class StubNoPrecedents:
    """csa.db に未取得の前例が無い状態。"""

    def games_at_position(self, key, limit, seen=None):
        return []

    def game(self, game_id):
        return None


class TestSourceKifuImport(unittest.TestCase):
    """課題局面の由来になった棋譜自身も前例と同じロジックで取り込む。

    放流対局そのものが一番効く材料なので、課題局面を決めたら捨てる、
    という作りにはしない。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_derive_problem_keeps_source_kifu(self):
        write_db(os.path.join(self.tmp.name, "000_b_abs.db"), {
            board_sfen_main(cshogi.Board()): ["7g7f none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        moves = "7g7f 3c3d 2g2f".split()
        problem = derive_problem(cshogi.Board(), moves, repo, 1, "deep", "t")
        self.assertEqual(problem.source_moves, moves)
        self.assertTrue(problem.source_sfen.startswith("lnsgkgsnl"))

    def test_source_kifu_is_imported(self):
        from problem import Problem
        moves = "7g7f 3c3d 2g2f 8c8d 2f2e".split()
        # 課題局面 = 初期局面。ply4 で先手 +1500 → 先手勝ち扱い
        engine = StubEngine([0, 0, 0, 0, 1500])
        book = LearningBook()
        problem = Problem(cshogi.Board().sfen(),
                          board_sfen_main(cshogi.Board()), "放流対局",
                          source_sfen=cshogi.Board().sfen(),
                          source_moves=moves)
        runner = make_runner(self.tmp.name, book, engine, StubNoPrecedents())
        used, added, removed = runner.import_precedents(problem)
        self.assertEqual((used, added, removed), (1, 2, 0))  # 7g7f と 2g2f
        self.assertEqual(book.get(board_sfen_main(cshogi.Board())),
                         {"7g7f": 1})
        self.assertEqual(book.get(board_sfen_main(board_from("7g7f 3c3d"))),
                         {"2g2f": 1})

    def test_deviated_game_still_imports_source_kifu(self):
        """定跡を外された棋譜でも、由来棋譜は必ず取り込める。

        課題局面は「外した手を適用した後」なので、その局面は導出元の
        棋譜に必ず現れる。`課題局面が現れない` は前例側だけで起きる。
        """
        from problem import Problem
        write_db(os.path.join(self.tmp.name, "000_b_abs.db"), {
            board_sfen_main(cshogi.Board()): ["7g7f none 0 32"],
        })
        repo = open_book_repo(self.tmp.name, 100)
        # 定跡は 7g7f だが実戦は 2g2f (外された変化)
        moves = "2g2f 3c3d 2f2e 8c8d".split()
        problem = derive_problem(cshogi.Board(), moves, repo, 1, "deep", "t")
        self.assertNotIn(problem.source_move, problem.book_candidates)
        self.assertEqual(problem.sfen_main,
                         board_sfen_main(board_from("2g2f")))
        # ply3 (後手番) で先手優勢 → 先手勝ち扱い
        engine = StubEngine([0, 0, -1500])
        book = LearningBook()
        runner = make_runner(self.tmp.name, book, engine, StubNoPrecedents())
        used, added, _removed = runner.import_precedents(problem)
        self.assertEqual(used, 1)                  # 由来棋譜が使われた
        self.assertGreater(added, 0)               # 「課題局面が現れない」でない

    def test_problem_without_source_kifu_skips_it(self):
        from problem import Problem
        engine = StubEngine([])
        book = LearningBook()
        problem = Problem(cshogi.Board().sfen(),
                          board_sfen_main(cshogi.Board()), "手動指定")
        runner = make_runner(self.tmp.name, book, engine, StubNoPrecedents())
        self.assertEqual(runner.import_precedents(problem), (0, 0, 0))
        self.assertEqual(engine.calls, [])          # 検討も走らない

    def test_source_kifu_and_precedents_are_counted_together(self):
        from csa_source import GameRef
        from problem import Problem

        class Source(StubNoPrecedents):
            def games_at_position(self, key, limit, seen=None):
                return [GameRef(7, 0, 0)]

            def game(self, game_id):
                return GameRecord(7, "e", "", "b", "w", 1, None,
                                  "2g2f 8c8d 2f2e".split())

        class Seen:
            def mark(self, ids, reason=""):
                return len(ids)

        moves = "7g7f 3c3d 2g2f".split()
        # どちらも ply2 (先手番) で +1500 = 先手勝ち扱いになるようにする
        engine = StubEngine([0, 0, 1500,        # 由来棋譜 (7g7f を登録)
                             0, 0, 1500])       # 前例   (2g2f を登録)
        book = LearningBook()
        problem = Problem(cshogi.Board().sfen(),
                          board_sfen_main(cshogi.Board()), "放流対局",
                          source_sfen=cshogi.Board().sfen(),
                          source_moves=moves)
        runner = make_runner(self.tmp.name, book, engine, Source())
        runner.seen = Seen()
        used, added, _removed = runner.import_precedents(problem)
        self.assertEqual(used, 2)               # 由来棋譜 + 前例
        self.assertGreater(added, 0)
        # 由来棋譜の 7g7f と前例の 2g2f が同じ局面に同居する
        # (他の棋譜由来の手は消さない)
        self.assertEqual(book.get(board_sfen_main(cshogi.Board())),
                         {"7g7f": 1, "2g2f": 1})


class TestPlayOut(unittest.TestCase):
    """棋譜が決着しないまま終わったら、その先を指し継いで決着させる。

    入玉模様など評価値が伸びない将棋を丸ごと捨てないための処理。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _problem(self):
        from problem import Problem
        return Problem(cshogi.Board().sfen(),
                       board_sfen_main(cshogi.Board()), "t")

    def test_resign_in_playout_decides_winner(self):
        from csa_source import GameRef
        record = GameRecord(1, "e", "", "b", "w", 1, None,
                            "7g7f 3c3d".split())
        # 棋譜は決着せず → 指し継ぎ1手目で先手が投了 → 後手勝ち
        engine = StubEngine([0, 0, (0, "resign")])
        book = LearningBook()
        runner = make_runner(self.tmp.name, book, engine, StubSource(record))
        added, _removed, reason = runner._import_one(
            GameRef(1, 0, 0), self._problem(), 1, 1)
        self.assertEqual(reason, "")
        self.assertEqual(added, 1)
        # 後手の手 (3c3d) だけが入る
        self.assertEqual(book.get(board_sfen_main(board_from("7g7f"))),
                         {"3c3d": 1})
        self.assertEqual(book.get(board_sfen_main(cshogi.Board())), {})

    def test_playout_moves_are_registered_too(self):
        from csa_source import GameRef
        record = GameRecord(1, "e", "", "b", "w", 1, None,
                            "7g7f 3c3d".split())
        # 棋譜は決着せず → 指し継ぎで 2g2f を指し、その後 先手優勢で決着
        engine = StubEngine([0, 0, (0, "2g2f"), (-1500, "x")])
        book = LearningBook()
        runner = make_runner(self.tmp.name, book, engine, StubSource(record))
        added, _removed, _ = runner._import_one(
            GameRef(1, 0, 0), self._problem(), 1, 1)
        self.assertEqual(added, 2)
        self.assertEqual(book.get(board_sfen_main(cshogi.Board())),
                         {"7g7f": 1})
        # 指し継ぎで指した手も勝ち側の手として入る
        self.assertEqual(book.get(board_sfen_main(board_from("7g7f 3c3d"))),
                         {"2g2f": 1})

    def test_resign_value_is_restored_after_playout(self):
        """指し継ぎ中だけ投了値を効かせ、検討用の値に戻すこと。"""
        from csa_source import GameRef
        from cycle import ANALYSIS_RESIGN_VALUE

        class RecordingEngine(StubEngine):
            def __init__(self, scores):
                super().__init__(scores)
                self.options = []

            def set_option(self, name, value):
                self.options.append((name, value))

        record = GameRecord(1, "e", "", "b", "w", 1, None, ["7g7f"])
        engine = RecordingEngine([0, (0, "resign")])
        runner = make_runner(self.tmp.name, LearningBook(), engine,
                             StubSource(record))
        runner._import_one(GameRef(1, 0, 0), self._problem(), 1, 1)
        self.assertEqual(engine.options,
                         [("ResignValue", 400),
                          ("ResignValue", ANALYSIS_RESIGN_VALUE)])

    def test_playout_stops_at_max_ply(self):
        from csa_source import GameRef
        record = GameRecord(1, "e", "", "b", "w", 1, None, ["7g7f"])
        # 7g7f の後は後手番。飛車の往復を返し続けるが MaxPly で打ち切られる
        engine = StubEngine([0] + [(0, "8b7b"), (0, "2h3h"),
                                   (0, "7b8b"), (0, "3h2h")] * 10)
        runner = make_runner(self.tmp.name, LearningBook(), engine,
                             StubSource(record), max_ply=5)
        added, _removed, reason = runner._import_one(
            GameRef(1, 0, 0), self._problem(), 1, 1)
        # 打ち切りは引き分け扱い = 既定では後手勝ち
        self.assertEqual(reason, "")
        self.assertGreater(added, 0)


class TestCrashSafety(unittest.TestCase):
    """強制終了に備えた保存順序。

    「定跡を保存してから取得済みにする」を守らないと、落ちたときに
    前例だけ消費されて学習内容が失われる (その棋譜は二度と引けない)。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_book_is_saved_before_marking_seen(self):
        from csa_source import GameRef
        from problem import Problem

        events = []

        class Seen:
            def mark(self, ids, reason=""):
                events.append(("mark", list(ids)))
                return len(ids)

        class Source:
            def games_at_position(self, key, limit, seen=None):
                return [GameRef(7, 0, 0), GameRef(8, 0, 0)]

            def game(self, game_id):
                return GameRecord(game_id, "e", "", "b", "w", 1, None,
                                  "7g7f 3c3d 2g2f".split())

        engine = StubEngine([0, 0, 1500] * 2)
        runner = make_runner(self.tmp.name, LearningBook(), engine, Source())
        runner.seen = Seen()
        runner.save_book = lambda: events.append(("save", None))
        problem = Problem(cshogi.Board().sfen(),
                          board_sfen_main(cshogi.Board()), "t")
        runner.import_precedents(problem)
        self.assertEqual(events,
                         [("save", None), ("mark", [7]),
                          ("save", None), ("mark", [8])])

    def test_unprocessed_precedents_are_not_consumed(self):
        """中断時、未処理の前例は取得済みにしない。"""
        from csa_source import GameRef
        from problem import Problem

        marked = []

        class Seen:
            def mark(self, ids, reason=""):
                marked.extend(ids)
                return len(ids)

        class Source:
            def games_at_position(self, key, limit, seen=None):
                return [GameRef(i, 0, 0) for i in (1, 2, 3)]

            def game(self, game_id):
                return GameRecord(game_id, "e", "", "b", "w", 1, None,
                                  "7g7f 3c3d 2g2f".split())

        runner = make_runner(self.tmp.name, LearningBook(),
                             StubEngine([0, 0, 1500]), Source())
        runner.seen = Seen()
        runner.save_book = lambda: None
        problem = Problem(cshogi.Board().sfen(),
                          board_sfen_main(cshogi.Board()), "t")

        original = runner._import_one

        def stop_after_first(ref, prob, index, total):
            result = original(ref, prob, index, total)
            runner.stop_requested = True      # 1局処理した直後に停止要求
            return result

        runner._import_one = stop_after_first
        runner.import_precedents(problem)
        self.assertEqual(marked, [1])         # 残り2局は消費しない

    def test_self_play_saves_every_game(self):
        """自己対局は1局ごとに保存する (長い対局の学習を失わない)。"""
        from problem import Problem
        saves = []
        runner = make_runner(self.tmp.name, LearningBook(),
                             StubEngine([]), None)
        runner.save_book = lambda: saves.append(1)
        runner.play_game = lambda problem: GameOutcome(
            BLACK, "投了", [PlayedMove("P1", "7g7f", BLACK, "engine")], 1)
        problem = Problem(cshogi.Board().sfen(),
                          board_sfen_main(cshogi.Board()), "t")
        games, _ = runner.self_play(problem)
        self.assertEqual(len(saves), games)


class TestImportReasons(unittest.TestCase):
    """0手だった理由が分かること (長時間運転でログだけが手がかりになる)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_problem_not_in_kifu(self):
        from csa_source import GameRef
        from problem import Problem
        record = GameRecord(1, "e", "", "b", "w", 1, None, ["7g7f"])
        runner = make_runner(self.tmp.name, LearningBook(), StubEngine([]),
                             StubSource(record))
        # 棋譜に現れない局面を課題にする
        problem = Problem("x", board_sfen_main(board_from("2g2f 8c8d")), "t")
        added, removed, reason = runner._import_one(GameRef(1, 0, 0),
                                                    problem, 1, 1)
        self.assertEqual((added, removed), (0, 0))
        self.assertEqual(reason, "課題局面が現れない")

    def test_broken_game_record(self):
        from csa_source import GameRef
        from problem import Problem
        runner = make_runner(self.tmp.name, LearningBook(), StubEngine([]),
                             StubSource(None))
        problem = Problem("x", board_sfen_main(cshogi.Board()), "t")
        self.assertEqual(runner._import_one(GameRef(1, 0, 0), problem, 1, 1),
                         (0, 0, "棋譜を読めない"))


class TestPrecedentPruning(unittest.TestCase):
    """前例取り込みでも負け側の手を削除すること。

    加算だけだと負け筋が定跡に残り続け、候補が増える一方になる
    (自己対局には削除があるのに前例側に無かった)。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _problem(self):
        from problem import Problem
        return Problem(cshogi.Board().sfen(),
                       board_sfen_main(cshogi.Board()), "t")

    def test_loser_move_is_removed(self):
        from csa_source import GameRef
        moves = "7g7f 3c3d 2g2f 8c8d 2f2e".split()
        record = GameRecord(1, "e", "", "b", "w", 1, None, moves)
        book = LearningBook()
        # 後手の 3c3d / 8c8d が定跡に入っている状態
        p1 = board_sfen_main(board_from("7g7f"))
        p3 = board_sfen_main(board_from("7g7f 3c3d 2g2f"))
        book.add(p1, "3c3d")
        book.add(p3, "8c8d")
        book.add(p3, "8b4b")                     # 分岐あり
        # ply4 で先手 +1500 → 後手が負け側
        engine = StubEngine([0, 0, 0, 0, 1500])
        runner = make_runner(self.tmp.name, book, engine, StubSource(record))
        added, removed, _ = runner._import_one(GameRef(1, 0, 0),
                                               self._problem(), 1, 1)
        self.assertEqual(added, 2)               # 先手の 7g7f と 2g2f
        self.assertEqual(removed, 1)             # 末端の 8c8d だけ
        self.assertEqual(book.get(p3), {"8b4b": 1})   # 分岐が残るので停止
        self.assertEqual(book.get(p1), {"3c3d": 1})   # それ以上遡らない

    def test_pruning_walks_back_to_branch_point(self):
        from csa_source import GameRef
        moves = "7g7f 3c3d 2g2f 8c8d 2f2e".split()
        record = GameRecord(1, "e", "", "b", "w", 1, None, moves)
        book = LearningBook()
        p1 = board_sfen_main(board_from("7g7f"))
        p3 = board_sfen_main(board_from("7g7f 3c3d 2g2f"))
        book.add(p1, "3c3d")
        book.add(p1, "8c8d")                     # ここが分岐点
        book.add(p3, "8c8d")                     # 末端 (分岐なし)
        engine = StubEngine([0, 0, 0, 0, 1500])
        runner = make_runner(self.tmp.name, book, engine, StubSource(record))
        _added, removed, _ = runner._import_one(GameRef(1, 0, 0),
                                                self._problem(), 1, 1)
        self.assertEqual(removed, 2)             # 末端を消し、分岐点まで遡る
        self.assertEqual(book.get(p3), {})
        self.assertEqual(book.get(p1), {"8c8d": 1})

    def test_nothing_to_remove_is_fine(self):
        from csa_source import GameRef
        moves = "7g7f 3c3d 2g2f 8c8d 2f2e".split()
        record = GameRecord(1, "e", "", "b", "w", 1, None, moves)
        book = LearningBook()
        engine = StubEngine([0, 0, 0, 0, 1500])
        runner = make_runner(self.tmp.name, book, engine, StubSource(record))
        added, removed, _ = runner._import_one(GameRef(1, 0, 0),
                                               self._problem(), 1, 1)
        self.assertEqual((added, removed), (2, 0))

    def test_removal_alone_counts_as_used(self):
        """登録0でも削除があれば「使った前例」として扱うこと。"""
        from csa_source import GameRef
        moves = "7g7f 3c3d".split()
        record = GameRecord(1, "e", "", "b", "w", 1, None, moves)
        book = LearningBook()
        book.add(board_sfen_main(board_from("7g7f")), "3c3d")
        # ply1 で後手が劣勢 (先手勝ち) → 先手の手は既に登録済みでない
        engine = StubEngine([0, -1500])
        runner = make_runner(self.tmp.name, book, engine, StubSource(record))
        added, removed, reason = runner._import_one(GameRef(1, 0, 0),
                                                    self._problem(), 1, 1)
        self.assertEqual(added, 1)
        self.assertEqual(removed, 0)
        self.assertEqual(reason, "")


class TestLearningRule(unittest.TestCase):
    """makebook4 の更新則: 勝ち側は加算、負け側は分岐点まで遡って削除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _runner(self, book):
        return make_runner(self.tmp.name, book, StubEngine([]), None)

    def test_winner_moves_are_reinforced(self):
        book = LearningBook()
        runner = self._runner(book)
        outcome = GameOutcome(BLACK, "投了", [
            PlayedMove("P1", "7g7f", BLACK, "engine"),
            PlayedMove("P2", "3c3d", WHITE, "engine"),
        ])
        runner.apply_learning(outcome)
        self.assertEqual(book.get("P1"), {"7g7f": 1})
        self.assertEqual(book.get("P2"), {})     # 負け側は追加しない

    def test_loser_deletion_stops_at_branch(self):
        book = LearningBook()
        book.add("P1", "a")                 # 分岐なし
        book.add("P2", "b")
        book.add("P2", "b2")                # ここが分岐点
        book.add("P3", "c")                 # 分岐なし
        runner = self._runner(book)
        outcome = GameOutcome(WHITE, "投了", [
            PlayedMove("P1", "a", BLACK, "book"),
            PlayedMove("P2", "b", BLACK, "book"),
            PlayedMove("P3", "c", BLACK, "book"),
        ])
        runner.apply_learning(outcome)
        self.assertEqual(book.get("P3"), {})      # 末端は消える
        self.assertEqual(book.get("P2"), {"b2": 1})  # 分岐が残り、ここで停止
        self.assertEqual(book.get("P1"), {"a": 1})  # それ以上は遡らない

    def test_absolute_moves_are_untouched(self):
        book = LearningBook()
        book.add("P1", "a")
        runner = self._runner(book)
        outcome = GameOutcome(WHITE, "投了", [
            PlayedMove("P1", "a", BLACK, "absolute"),
        ])
        runner.apply_learning(outcome)
        self.assertEqual(book.get("P1"), {"a": 1})


class TestInfoParsing(unittest.TestCase):
    def test_cp_and_mate(self):
        self.assertEqual(_parse_info("info depth 1 score cp -250 pv 7g7f"),
                         (-250, False))
        self.assertEqual(_parse_info("info score mate 5")[1], True)
        self.assertGreater(_parse_info("info score mate 5")[0], 20000)
        self.assertLess(_parse_info("info score mate -3")[0], -20000)

    def test_bounds_and_missing_are_ignored(self):
        self.assertIsNone(_parse_info("info depth 1 score cp 20 upperbound"))
        self.assertIsNone(_parse_info("info depth 1 nodes 100"))


class TestConfig(unittest.TestCase):
    def test_tunable_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("PrecedentGames 7\n")
            config, _ = Config.load(path)
            self.assertEqual(config.get_int("PrecedentGames"), 7)
            config.set("DecisiveEval", "800")
            self.assertEqual(config.get_int("DecisiveEval"), 800)
            with self.assertRaises(ConfigError):
                config.set("EnginePath", "x")     # 実行中は変更不可
            with self.assertRaises(ConfigError):
                config.set("DecisiveEval", "abc")
            self.assertEqual(config.get_int("DecisiveEval"), 800)  # 前値維持

    def test_name_filters(self):
        """カンマ・空白どちらの区切りでも同じ結果になること。

        カンマ限定だと `NameFilters A B C` が無意味な1フィルタになり
        自動補充が静かに空振りする (実 config で起きかけた)。"""
        for raw in ("Condenser, Tantalum", "Condenser Tantalum",
                    "Condenser,Tantalum", " Condenser  Tantalum "):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "config.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"NameFilters {raw}\n")
                config, _ = Config.load(path)
                self.assertEqual(config.name_filters(),
                                 ["Condenser", "Tantalum"], raw)


if __name__ == "__main__":
    unittest.main()


#: 平手初期局面 (手数なし SFEN)
START = board_sfen_main(cshogi.Board())


class TestHighPriorityMerge(unittest.TestCase):
    """0番台定跡の閲覧用取り込み (毎サイクル最新を複製する)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        # 対象定跡がないと Config.target_priority() が通らない
        self._write("100_x_target.db", {})

    def _write(self, name, entries):
        book = LearningBook("zero", 32)
        for sfen, moves in entries.items():
            for usi, count in moves.items():
                book.add(sfen, usi, count)
        book.save(os.path.join(self.dir, name))

    def _runner(self, book, priority="0"):
        config_path = os.path.join(self.dir, "config.txt")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(f"BookDir {self.dir}\nLogDir {self.dir}\n"
                    f"CsaDbPath {self.dir}\nTargetBook 100_x_target.db\n"
                    f"MergeBookPriority {priority}\n")
        config, _ = Config.load(config_path)
        return CycleRunner(config, book, None, None, None, self.dir,
                           report=lambda msg: None)

    def test_copies_zero_series_into_target(self):
        self._write("000_b_absolute.db", {START: {"7g7f": 3}})
        book = LearningBook("zero", 32)
        pos, moves = self._runner(book).merge_high_priority_books()
        self.assertEqual((pos, moves), (1, 1))
        self.assertEqual(book.get(START), {"7g7f": 3})

    def test_does_not_duplicate_existing_move(self):
        self._write("000_b_absolute.db", {START: {"7g7f": 3}})
        book = LearningBook("zero", 32)
        book.add(START, "7g7f", 9)
        pos, moves = self._runner(book).merge_high_priority_books()
        self.assertEqual((pos, moves), (0, 0))
        # 既存の採用回数を上書きしない (学習結果を壊さない)
        self.assertEqual(book.get(START), {"7g7f": 9})

    def test_restores_move_deleted_by_learning(self):
        """刈り込みで消えても次のサイクルで戻る (消えてよい前提の設計)。"""
        self._write("000_b_absolute.db", {START: {"7g7f": 3}})
        book = LearningBook("zero", 32)
        runner = self._runner(book)
        runner.merge_high_priority_books()
        book.remove(START, "7g7f")
        self.assertEqual(book.get(START), {})
        pos, moves = runner.merge_high_priority_books()
        self.assertEqual((pos, moves), (1, 1))

    def test_skips_books_above_the_limit(self):
        self._write("099_b_other.db", {START: {"2g2f": 4}})
        book = LearningBook("zero", 32)
        pos, moves = self._runner(book).merge_high_priority_books()
        self.assertEqual((pos, moves), (0, 0))
        self.assertEqual(book.get(START), {})

    def test_negative_priority_disables_merge(self):
        self._write("000_b_absolute.db", {START: {"7g7f": 3}})
        book = LearningBook("zero", 32)
        pos, moves = self._runner(book, priority="-1").merge_high_priority_books()
        self.assertEqual((pos, moves), (0, 0))
