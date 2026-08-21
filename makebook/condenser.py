"""Condenser — 定跡修正サイクルの実行本体 (GUI から駆動できるコンソール)。

BookMiner と同じく「子プロセスとして起動し stdin にコマンドを送る」形を
想定している。ワーカースレッドがタスクを1つずつ処理し、メインスレッドは
コマンドを受け付ける。**サイクルの途中では次のタスクを取り出さない**。

コマンド:
  s              状態表示
  t <position>   課題局面を直接追加 (startpos moves ... / sfen ... moves ...)
  k <position>   棋譜を追加 (そこから課題局面を導出する)
  set <キー> <値> 設定変更 (実行中に変えられるキーのみ)
  show           設定一覧
  w              いま定跡を書き出す
  pause / resume サイクルの一時停止 / 再開
  q              終了 (実行中のサイクルの区切りで止まる)
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
from common import (Config, ConfigError, InstanceLock,  # noqa: E402
                    LearningBook, Stopwatch, format_duration)
from csa_source import CsaSource, SeenStore  # noqa: E402
from cycle import ANALYSIS_RESIGN_VALUE, CycleRunner  # noqa: E402
from problem import (Problem, PositionError, derive_problem,  # noqa: E402
                     open_book_repo, parse_position_command)
from sfen_guard import validate_sfen  # noqa: E402
from tasks import KIND_KIFU, KIND_PROBLEM, Task, TaskQueue  # noqa: E402
from usi_engine import UsiEngine  # noqa: E402

import cshogi  # type: ignore  # noqa: E402
from book_repo import board_sfen_main  # noqa: E402

VERSION = "0.1"


class Condenser:
    def __init__(self, config: Config):
        self.config = config
        self.log_dir = config.path_of("LogDir")
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_path = os.path.join(self.log_dir, "condenser.log")
        self.print_lock = threading.Lock()

        self.book_dir = config.path_of("BookDir")
        self.book_path = os.path.join(self.book_dir, config.get("TargetBook"))
        self.book = LearningBook.load(self.book_path,
                                      config.get("ValueMode"),
                                      config.get_int("BookDepth"))
        self.report(f"定跡を読み込みました: {os.path.basename(self.book_path)} "
                    f"({len(self.book)}局面 / {self.book.move_count()}手)")

        self.queue = TaskQueue(config.path_of("TaskDir"))
        self.source = CsaSource(config.path_of("CsaDbPath"))
        self.seen = SeenStore(config.path_of("StateDbPath"))
        self.report(f"取得済み対局: {self.seen.count()}局")

        self.engine = UsiEngine(config.path_of("EnginePath"),
                                config.get("EngineEncoding"), self.log)
        self.runner: CycleRunner | None = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.state = "起動中"
        self.cycles = 0
        self.started_at = time.monotonic()

    # -- 出力 ------------------------------------------------------------

    def log(self, message: str) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    def report(self, message: str) -> None:
        with self.print_lock:
            print(message, flush=True)
        self.log(message)

    # -- エンジン --------------------------------------------------------

    def start_engine(self) -> None:
        with Stopwatch(self.report, "エンジン起動"):
            self.engine.start({
                "Threads": self.config.get_int("EngineThreads"),
                "USI_Hash": self.config.get_int("EngineHash"),
                "USI_OwnBook": "false",
                "MultiPV": 1,
                "NetworkDelay": 0,
                "NetworkDelay2": 0,
                "MinimumThinkingTime": 0,
                "MaxMovesToDraw": 0,
                "ResignValue": ANALYSIS_RESIGN_VALUE,
            })

    # -- タスク解決 ------------------------------------------------------

    def resolve(self, task: Task) -> Problem | None:
        """タスクを課題局面に変換する。不正なら None (エラー行きにする)。"""
        payload = task.payload
        if task.kind == KIND_PROBLEM:
            text = payload.get("position") or payload.get("sfen") or ""
            try:
                board = parse_position_command(text)
            except PositionError as e:
                self.report(f"  タスクが不正です: {e}")
                return None
            if board.is_game_over():
                self.report("  タスクの局面は既に終局しています")
                return None
            # 局面直接指定のタスクには由来棋譜が無いので前例だけを使う
            return Problem(board.sfen(), board_sfen_main(board),
                           task.origin or "手動指定")

        text = payload.get("position") or ""
        try:
            board = parse_position_command(text)
        except PositionError as e:
            self.report(f"  棋譜が不正です: {e}")
            return None
        start = cshogi.Board()
        tokens = text.split()
        if tokens and tokens[0] == "position":
            tokens = tokens[1:]
        moves: list[str] = []
        if "moves" in tokens:
            index = tokens.index("moves")
            moves = tokens[index + 1:]
            if tokens[0] == "sfen":
                start.set_sfen(" ".join(tokens[1:index]))
        elif tokens and tokens[0] == "sfen":
            start.set_sfen(" ".join(tokens[1:]))

        # 対象定跡自身は含めない (含めると課題局面が自分の学習結果に
        # 引きずられて奥へ流れる)
        repo = open_book_repo(self.book_dir,
                              self.config.target_priority(),
                              lambda msg: self.log(msg))
        return derive_problem(start, moves, repo, 1, "deep",
                              task.origin or "棋譜", None)

    # -- csa.db からの自動補充 -------------------------------------------

    def refill_from_csa(self) -> int:
        """タスクが尽きたとき、**自分が負けた対局**を新しい順に取り込む。

        勝った対局は直す必要が薄いので積まない (キューも溜まらない)。
        千日手は DrawWinner の裏側 (既定では先手) にとって負け扱い。
        """
        filters = self.config.name_filters()
        if not filters:
            return 0
        draw_loser = self.config.draw_loser()
        refs = self.source.games_by_name(filters, 1, self.seen,
                                         draw_loser=draw_loser)
        if not refs:
            return 0
        pushed = 0
        for ref in refs:
            game = self.source.game(ref.game_id)
            self.seen.mark([ref.game_id], "name")
            if game is None or not game.moves:
                continue
            start = "startpos"
            if game.initial_sfen:
                if validate_sfen(game.initial_sfen) is not None:
                    continue      # 壊れた初期局面の棋譜は取り込まない
                start = f"sfen {game.initial_sfen}"
            position = f"{start} moves {' '.join(game.moves)}"
            side = self.source.loser_side(game, filters, draw_loser)
            self.queue.push(
                KIND_KIFU, {"position": position, "game_id": ref.game_id,
                            "outcome": "loss", "loser_side": side},
                origin=f"csa.db {ref.game_id} 敗戦"
                       f"{f'({side})' if side else ''} "
                       f"{game.black_name} vs {game.white_name}")
            pushed += 1
        return pushed

    # -- ワーカー --------------------------------------------------------

    def worker(self) -> None:
        try:
            self.start_engine()
        except Exception as e:
            self.report(f"エンジンを起動できません: {e!r}")
            self.stop_event.set()
            return

        while not self.stop_event.is_set():
            try:
                self._worker_step()
            except Exception as e:
                # ここで抜けるとワーカーが死んで以後まったく進まなくなる。
                # 1周ぶんを捨てて続行する。
                self.report(f"ワーカーで予期しないエラー (継続): {e!r}")
                time.sleep(2.0)
        self.state = "終了処理中"
        try:
            self.save_book()
        except Exception as e:
            self.report(f"終了時の書き出しに失敗: {e!r}")
        self.engine.quit()

    def _worker_step(self) -> None:
        """タスク1件ぶんの処理。例外は呼び出し側で受ける。"""
        if self.pause_event.is_set():
            self.state = "一時停止中"
            time.sleep(0.5)
            return

        task = self.queue.take()
        if task is None:
            self.state = "タスク補充中"
            if self.refill_from_csa() == 0:
                self.state = "待機中 (タスクなし)"
                time.sleep(3.0)
            return

        self.state = f"処理中: {task.name}"
        self.report(f"\n--- タスク {task.name} ({task.origin}) ---")
        problem = self.resolve(task)
        if problem is None:
            self.queue.fail(task, "課題局面を決定できませんでした")
            return

        try:
            if self.run_cycle(problem):
                self.queue.complete(task)
            else:
                # 中断されたタスクは残す。次回起動でやり直す
                # (取得済みにした前例も処理した分だけなので重複しない)。
                self.report(f"  タスクを残しました: {task.name}")
        except Exception as e:
            detail = traceback.format_exc()
            self.report(f"サイクルが失敗しました: {e!r}")
            self.log(detail)
            self.queue.fail(task, detail)
            if not self.engine.alive():
                self.engine.restart()

    def run_cycle(self, problem: Problem) -> bool:
        """1サイクル回す。最後まで通ったら True、中断されたら False。"""
        runner = CycleRunner(self.config, self.book, self.engine,
                             self.source, self.seen, self.book_dir,
                             self.report, save_book=self.save_book)
        self.runner = runner
        started = time.monotonic()
        try:
            self.engine.set_option("ResignValue", ANALYSIS_RESIGN_VALUE)
            self.engine.isready()
            stats = runner.run(problem)
            interrupted = runner.stop_requested
        finally:
            self.runner = None
        with Stopwatch(self.report, "定跡の書き出し"):
            try:
                self.save_book()
            except OSError as e:
                # 書き出せないままタスクを完了にすると学習が消える。
                # 未完了として残し、次回やり直す。
                self.report(f"定跡を書き出せませんでした ({e}) — "
                            f"タスクは未完了のまま残します")
                return False
        removed = (f"-{stats.precedent_removed}手"
                   if stats.precedent_removed else "")
        summary = (f"前例{stats.precedents_used}局/"
                   f"+{stats.precedent_moves}手{removed}, "
                   f"自己対局{stats.self_play_games}局 → "
                   f"定跡 {len(self.book)}局面 / {self.book.move_count()}手")
        elapsed = format_duration(time.monotonic() - started)
        if interrupted:
            # 連勝条件に達していないので、このタスクは未完了として残す。
            # 学習した分は書き出し済みなので無駄にはならない。
            self.report(f"=== サイクル中断 ({elapsed}) {summary}")
            return False
        self.cycles += 1
        self.report(f"=== サイクル完了 ({elapsed}) {summary}")
        return True

    def save_book(self) -> None:
        self.book.value_mode = self.config.get("ValueMode")
        self.book.depth = self.config.get_int("BookDepth")
        self.book.save(self.book_path)

    # -- コマンド --------------------------------------------------------

    def status(self) -> None:
        uptime = format_duration(time.monotonic() - self.started_at)
        self.report(
            f"[状態] {self.state} / 稼働 {uptime} / 完了サイクル {self.cycles}\n"
            f"       タスク残 {self.queue.count()} / "
            f"取得済み対局 {self.seen.count()}\n"
            f"       定跡 {len(self.book)}局面 / {self.book.move_count()}手 "
            f"→ {self.book_path}")

    def show_settings(self) -> None:
        lines = ["[設定]"]
        for key in common.TUNABLE_KEYS:
            lines.append(f"  {key:<20}{self.config.get(key)}")
        self.report("\n".join(lines))

    def handle_command(self, line: str) -> bool:
        parts = line.strip().split(None, 1)
        if not parts:
            return True
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("q", "quit", "exit"):
            self.report("停止します (実行中のサイクルの区切りまで待ちます)")
            self.stop_event.set()
            if self.runner is not None:
                self.runner.stop_requested = True
            return False
        if cmd in ("s", "status"):
            self.status()
        elif cmd == "show":
            self.show_settings()
        elif cmd == "pause":
            self.pause_event.set()
            self.report("一時停止しました (resume で再開)")
        elif cmd == "resume":
            self.pause_event.clear()
            self.report("再開しました")
        elif cmd == "w":
            with Stopwatch(self.report, "定跡の書き出し"):
                self.save_book()
        elif cmd == "set":
            self._cmd_set(arg)
        elif cmd in ("t", "k"):
            self._cmd_push(cmd, arg)
        else:
            self.report(f"不明なコマンド: {cmd} (s/t/k/set/show/w/pause/"
                        f"resume/q)")
        return True

    def _cmd_set(self, arg: str) -> None:
        parts = arg.split(None, 1)
        if len(parts) < 2:
            self.report("使い方: set <キー> <値>")
            return
        try:
            self.config.set(parts[0], parts[1].strip())
            self.config.save()
            self.report(f"{parts[0]} = {self.config.get(parts[0])}")
        except ConfigError as e:
            self.report(f"設定できません: {e}")

    def _cmd_push(self, cmd: str, arg: str) -> None:
        if not arg:
            self.report("使い方: t <position> / k <position>")
            return
        try:
            parse_position_command(arg)      # 手入力の取り違えを弾く
        except PositionError as e:
            self.report(f"局面が不正です: {e}")
            return
        kind = KIND_PROBLEM if cmd == "t" else KIND_KIFU
        path = self.queue.push(kind, {"position": arg}, origin="手動")
        self.report(f"タスクを追加しました: {os.path.basename(path)} "
                    f"(残 {self.queue.count()})")

    # -- 実行 ------------------------------------------------------------

    def run(self) -> None:
        self.report(f"=== Condenser v{VERSION} 開始 (pid={os.getpid()}) ===")
        self.status()
        thread = threading.Thread(target=self.worker, daemon=True)
        thread.start()
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                try:
                    if not self.handle_command(line):
                        break
                except Exception as e:
                    self.report(f"コマンド処理でエラー: {e!r}")
        except KeyboardInterrupt:
            pass
        self.stop_event.set()
        if self.runner is not None:
            self.runner.stop_requested = True
        thread.join(timeout=120)
        self.source.close()
        self.seen.close()
        self.report("=== Condenser 終了 ===")


def main() -> int:
    try:
        config, warnings = Config.load()
    except ConfigError as e:
        print(f"設定エラー: {e}", flush=True)
        return 1
    # 二重起動防止。同じ設定の Condenser が動いていたら何もせず終了する
    # (定跡・state.db を2プロセスが書くと必ず壊れる)。
    lock = InstanceLock(config.path_of("LockPath"))
    if not lock.acquire():
        print(f"既に別の Condenser が動いています "
              f"(lock: {config.path_of('LockPath')})。終了します", flush=True)
        return 2
    try:
        app = Condenser(config)
        for message in warnings:
            app.report(f"[設定] {message}")
        app.run()
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
