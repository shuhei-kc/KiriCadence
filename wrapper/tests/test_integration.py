"""fake USIエンジンを使ったプロトコル統合テスト (DESIGN.md §11)。

wrapperをサブプロセスとして起動し、GUI役としてUSI対話を検証する:
usiハンドシェイク / 定跡ヒット時の非転送応答 / 定跡復帰 / ponderhit定跡確認 /
残骸bestmove抑制 / go infinite素通し / クラッシュ復旧 / 千日手ガード /
定跡専用モード。custom_engine3 の回帰群を移植し、KiriCadence 固有の
仕様 (b/w/x・千日手・決定ログ) を追加したもの。
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import cshogi  # noqa: E402

from book_repo import board_sfen_main  # noqa: E402


def sfen_after(moves: str = "") -> str:
    board = cshogi.Board()
    for token in moves.split():
        board.push_usi(token)
    return board_sfen_main(board)


def write_db(path, blocks):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#YANEURAOU-DB2016 1.00\n")
        for sfen in sorted(blocks):
            f.write(f"sfen {sfen} 0\n")
            for line in blocks[sfen]:
                f.write(line + "\n")


class WrapperProc:
    def __init__(self, config_path, extra_env=None):
        env = dict(os.environ, KIRICADENCE_CONFIG=config_path,
                   PYTHONIOENCODING="utf-8")
        if extra_env:
            env.update(extra_env)
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(BASE, "main.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=BASE, env=env, universal_newlines=True, bufsize=1,
            encoding="utf-8")
        self.lines = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))

    def send(self, cmd):
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def read_until(self, prefix, timeout=10.0):
        """prefix で始まる行が来るまで読み、(その行, それまでの全行)。"""
        collected = []
        while True:
            try:
                line = self.lines.get(timeout=timeout)
            except queue.Empty:
                raise AssertionError(
                    f"timeout waiting for {prefix!r}; got {collected}")
            collected.append(line)
            if line.startswith(prefix):
                return line, collected

    def drain(self, wait=0.0):
        if wait:
            time.sleep(wait)
        leaked = []
        while not self.lines.empty():
            leaked.append(self.lines.get_nowait())
        return leaked

    def close(self):
        try:
            self.send("quit")
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


GO = "go btime 60000 wtime 60000 byoyomi 1000"

#: 千日手テスト用の飛車往復 (全手が合法)。
#: 4手で開始局面 (先手番) が2回目の出現になる。
REP_BLACK_2 = "2h3h 8b7b 3h2h 7b8b"
#: 白番版: 「2h3h の後の局面」(後手番) が5手目終了時に2回目の出現になる。
REP_WHITE_2 = "2h3h 8b7b 3h2h 7b8b 2h3h"


class TestIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keep = os.environ.get("KC_TEST_DIR")
        if keep:
            os.makedirs(keep, exist_ok=True)
            cls.tmp = None
            d = keep
        else:
            cls.tmp = tempfile.TemporaryDirectory()
            d = cls.tmp.name
        cls.base_dir = d

        # --- 定跡: b/x 2ファイル構成 ---
        cls.book_dir = os.path.join(d, "book")
        os.makedirs(cls.book_dir)
        write_db(os.path.join(cls.book_dir, "000_b_main.db"), {
            sfen_after(): ["7g7f none 123 24"],
            sfen_after("7g7f 3c3d"): ["2g2f none 90 20"],   # 復帰テスト用
        })
        write_db(os.path.join(cls.book_dir, "100_x_sub.db"), {
            sfen_after("2h3h"): ["4a3b none 10 5"],         # 千日手 (後手)
        })

        # --- fake エンジン起動用 bat ---
        fake = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fake_engine.py")
        cls.engine_bat = os.path.join(d, "fake_engine.bat")
        # .bat は cmd が OEM コードページで読む。ascii 固定だと
        # リポジトリのパスに非 ASCII が入っただけで setUpClass ごと落ちる。
        bat_encoding = "mbcs" if sys.platform == "win32" else "utf-8"
        with open(cls.engine_bat, "w", encoding=bat_encoding) as f:
            f.write(f'@echo off\r\n"{sys.executable}" "{fake}"\r\n')

        cls.log_dir = os.path.join(d, "logs")

        cls.config_path = os.path.join(d, "config.txt")
        with open(cls.config_path, "w", encoding="utf-8") as f:
            f.write(f"EnginePath {cls.engine_bat}\n"
                    f"BookDir {cls.book_dir}\n"
                    f"LogDir {cls.log_dir}\n"
                    f"TieBreak first\n")

    @classmethod
    def tearDownClass(cls):
        if cls.tmp is not None:
            cls.tmp.cleanup()

    def setUp(self):
        """fake エンジンの作業ファイルはテストごとに分ける。

        共有すると、前のテストで終了処理中のエンジンが同じファイルへ
        追記しており、Windows では削除・読み取りが競合する。"""
        stem = os.path.join(self.base_dir, self._testMethodName)
        self.fake_log = f"{stem}.fake.log"
        self.crash_arm = f"{stem}.crash.arm"
        self.slow_arm = f"{stem}.slow.arm"

    def fake_log_size(self) -> int:
        """現時点のログ長。以降の追記だけを読むための基準点。"""
        return (os.path.getsize(self.fake_log)
                if os.path.exists(self.fake_log) else 0)

    def read_fake_log(self, since: int = 0) -> str:
        with open(self.fake_log, encoding="utf-8") as f:
            f.seek(since)
            return f.read()

    def start_wrapper(self, config_path=None, extra_env=None):
        env = {"FAKE_LOG": self.fake_log,
               "FAKE_CRASH_ARM": self.crash_arm,
               "FAKE_SLOW_ARM": self.slow_arm}
        if extra_env:
            env.update(extra_env)
        return WrapperProc(config_path or self.config_path, env)

    def handshake(self, w):
        w.send("usi")
        w.read_until("usiok")
        w.send("isready")
        w.read_until("readyok")
        w.send("usinewgame")

    # -- 基本 ---------------------------------------------------------------

    def test_handshake_exposes_own_option(self):
        w = self.start_wrapper()
        try:
            w.send("usi")
            _, lines = w.read_until("usiok")
            joined = "\n".join(lines)
            self.assertIn("id name FakeEngine", joined)
            self.assertIn("option name Hash", joined)
            self.assertIn("option name BookEnabled", joined)
            self.assertIn("option name USI_Ponder", joined)
        finally:
            w.close()

    def test_engine_noise_does_not_kill_reader(self):
        """空白のみの行・不正UTF-8行・cp932 stdout の複合でも
        reader thread が死なず、中継が継続すること (レビュー critical)。"""
        w = self.start_wrapper(
            extra_env={"FAKE_NOISE": "1", "PYTHONIOENCODING": "cp932"})
        try:
            w.send("usi")
            w.read_until("usiok")
            w.send("isready")
            w.read_until("readyok")     # ノイズ2行の後に届くこと
            w.send("usinewgame")
            w.send("position startpos")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 7g7f")          # 定跡
            w.send("position startpos moves 2g2f 8c8d")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 3c3d ponder 2f2e")  # エンジン
            w.send("isready")           # ノイズ再注入後も応答すること
            w.read_until("readyok")
        finally:
            w.close()

    def test_book_reentry_after_engine_move(self):
        """定跡ヒット→定跡外(エンジン思考)→定跡復帰、が毎goで機能すること。"""
        w = self.start_wrapper()
        try:
            self.handshake(w)
            # 1手目: 定跡ヒット
            w.send("position startpos")
            w.send(GO)
            line, lines = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 7g7f")
            self.assertIn("Book_Hit", "\n".join(lines))
            # 定跡外: エンジンが思考して指す (ponder句も中継)
            w.send("position startpos moves 2g2f 8c8d")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 3c3d ponder 2f2e")
            # 定跡内の局面に戻る → 再び定跡から指せること
            w.send("position startpos moves 7g7f 3c3d")
            w.send(GO)
            line, lines = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 2g2f")
            self.assertIn("Book_Hit", "\n".join(lines))
        finally:
            w.close()

    def test_book_disabled_option(self):
        w = self.start_wrapper()
        try:
            self.handshake(w)
            w.send("setoption name BookEnabled value false")
            w.send("position startpos")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 3c3d ponder 2f2e")  # エンジン手
        finally:
            w.close()

    def test_go_infinite_passes_through(self):
        w = self.start_wrapper()
        try:
            self.handshake(w)
            w.send("position startpos")      # 定跡があっても素通し
            w.send("go infinite")
            w.read_until("info depth 1")
            w.send("stop")
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 8c8d")
        finally:
            w.close()

    # -- 千日手ガード (§5.1) -------------------------------------------------

    def test_repetition_guard_black_skips_book(self):
        w = self.start_wrapper()
        try:
            self.handshake(w)
            # 初出 (開始局面): 定跡ヒット
            w.send("position startpos")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 7g7f")
            # 飛車往復で開始局面が2回目の出現 (先手番):
            # 定跡フル無視 → エンジン手
            w.send(f"position startpos moves {REP_BLACK_2}")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 3c3d ponder 2f2e")
        finally:
            w.close()

    def test_repetition_guard_white_still_uses_book(self):
        w = self.start_wrapper()
        try:
            self.handshake(w)
            # 2回目の出現でも後手番は定跡を使い切る
            w.send(f"position startpos moves {REP_WHITE_2}")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 4a3b")
        finally:
            w.close()

    # -- ponder --------------------------------------------------------------

    def test_ponderhit_uses_book(self):
        """ponderhitの瞬間に定跡を確認する。

        ヒット: エンジンをstopし、その残骸bestmoveを漏らさず定跡手を即指し。
        ミス: ponderhitを転送してエンジンの手 (ponder句付き) を中継。"""
        w = self.start_wrapper()
        try:
            self.handshake(w)
            # --- ヒット: startpos (定跡=7g7f) を先読み中に的中 ---
            w.send("position startpos")
            w.send(f"go ponder {GO[3:]}")
            time.sleep(0.1)
            w.send("ponderhit")
            line, lines = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 7g7f")
            joined = "\n".join(lines)
            self.assertIn("Book_Hit", joined)
            # 放棄した探索の info (depth 9) が定跡bestmoveの前に漏れないこと
            self.assertNotIn("info depth 9", joined)
            # stopされたfake engineの残骸 (info/bestmove) が後から漏れないこと
            leaked = w.drain(wait=0.5)
            self.assertFalse(
                [ln for ln in leaked
                 if ln.startswith(("bestmove", "info depth"))],
                f"aborted search output leaked: {leaked}")

            # --- ミス: 定跡外局面ではエンジンが続行 ---
            w.send("position startpos moves 2g2f 8c8d")
            w.send(f"go ponder {GO[3:]}")
            time.sleep(0.1)
            w.send("ponderhit")
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 5g5f ponder 8c8d")
        finally:
            w.close()

    def test_stale_bestmove_is_suppressed(self):
        """探索中に対局が終わり次局が始まっても、放棄した探索の残骸
        bestmoveがGUIに漏れないこと。"""
        w = self.start_wrapper()
        try:
            self.handshake(w)
            # 定跡外局面で go → fake engineは1秒後にbestmoveを返す
            open(self.slow_arm, "w").close()
            w.send("position startpos moves 2g2f 8c8d")
            w.send(GO)
            time.sleep(0.1)
            # bestmoveを待たずに対局終了 → 次の対局へ
            w.send("gameover lose")
            w.send("usinewgame")
            w.send("position startpos")
            w.send(GO)
            line, _ = w.read_until("bestmove", timeout=10.0)
            self.assertEqual(line, "bestmove 7g7f")   # 新しい対局の定跡手
            # 放棄した探索の info / bestmove 3c3d が後から漏れてこないこと
            leaked = w.drain(wait=1.5)
            self.assertFalse(
                [ln for ln in leaked
                 if ln.startswith(("bestmove", "info depth"))],
                f"stale output leaked: {leaked}")
        finally:
            w.close()

    # -- 復旧 ----------------------------------------------------------------

    def test_crash_recovery_resends_journal_and_position(self):
        w = self.start_wrapper()
        try:
            self.handshake(w)
            w.send("setoption name Hash value 256")   # journal対象
            w.send("setoption name ClearHash")        # button型: journal除外
            # setoption がエンジンに届き、ログに書かれ切ってから基準点を取る
            # (fake エンジンのログ書き込みは非同期なので isready で同期する)
            w.send("isready")
            w.read_until("readyok")
            open(self.crash_arm, "w").close()
            since = self.fake_log_size()
            w.send("position startpos moves 2g2f 8c8d 2f2e")
            w.send(GO)
            line, _ = w.read_until("bestmove", timeout=15.0)
            self.assertEqual(line, "bestmove 3c3d ponder 2f2e")
            log = self.read_fake_log(since)
            # 再起動後のエンジンに journal (Hash) と position が再送された
            self.assertIn("setoption name Hash value 256", log)
            self.assertNotIn("setoption name ClearHash", log)  # button除外
            self.assertIn("position startpos moves 2g2f 8c8d 2f2e", log)
        finally:
            w.close()

    # -- 定跡専用モード + 決定ログ -------------------------------------------

    def test_book_only_mode_and_decision_log(self):
        d = self.base_dir
        log_dir = os.path.join(d, "logs_noengine")
        config_path = os.path.join(d, "config_noengine.txt")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(f"BookDir {self.book_dir}\n"
                    f"LogDir {log_dir}\n"
                    f"TieBreak first\n")
        w = WrapperProc(config_path)
        try:
            w.send("usi")
            _, lines = w.read_until("usiok")
            self.assertIn("book only", "\n".join(lines))
            w.send("isready")
            w.read_until("readyok")
            w.send("usinewgame")
            w.send("position startpos")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 7g7f")
            w.send("position startpos moves 2g2f 8c8d")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove resign")   # 定跡なし+エンジンなし
            w.send("gameover lose")
        finally:
            w.close()
        with open(os.path.join(log_dir, "decisions.jsonl"),
                  encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        events = [r["event"] for r in rows]
        self.assertEqual(events, ["newgame", "hit", "miss", "gameover"])
        game_ids = {r["game_id"] for r in rows}
        self.assertEqual(len(game_ids), 1)              # 全行同じ対局ID
        self.assertTrue(next(iter(game_ids)).endswith("-0001"))
        self.assertEqual(rows[1]["move"], "7g7f")
        self.assertEqual(rows[1]["side"], "b")
        self.assertEqual(rows[2]["files"].get("000_b_main.db"), "no_entry")

    def test_config_reload_on_usinewgame(self):
        """usinewgame で BookMinDepth を再読込し、不正値なら前の値を維持。"""
        d = self.base_dir
        book_dir = os.path.join(d, "book_reload")
        os.makedirs(book_dir, exist_ok=True)
        write_db(os.path.join(book_dir, "000_b_a.db"),
                 {sfen_after(): ["7g7f none 100 0"]})   # depth 0
        config_path = os.path.join(d, "config_reload.txt")
        log_dir = os.path.join(d, "logs_reload")

        def write_config(min_depth):
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(f"BookDir {book_dir}\nLogDir {log_dir}\n"
                        f"BookMinDepth {min_depth}\nTieBreak first\n")

        write_config(1)
        w = WrapperProc(config_path)
        try:
            self.handshake(w)
            w.send("position startpos")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove resign")   # depth gate でミス
            # BookMinDepth 0 に書き換え → 次の usinewgame から depth 無視
            write_config(0)
            w.send("usinewgame")
            w.send("position startpos")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 7g7f")
            # 不正値に書き換え → 前の値 (0) を維持して続行
            write_config("broken")
            w.send("usinewgame")
            w.send("position startpos")
            w.send(GO)
            line, _ = w.read_until("bestmove")
            self.assertEqual(line, "bestmove 7g7f")
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
