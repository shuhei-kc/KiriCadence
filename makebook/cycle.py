"""1課題局面あたりの定跡修正サイクル。

  1. 課題局面を決める (タスク由来)
  2. csa.db から未取得の前例を新しい順に N 局取り、検討して勝ち側の手を登録
  3. 課題局面から自己対局し、どちらかが規定連勝したらサイクル終了
  4. 対象定跡 (`TargetBook`) を書き出す

各フェーズの所要時間を報告する。サイクル中はタスクを取り出さないので、
外部からの投入がループに割り込むことはない。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

import cshogi  # type: ignore

from book_repo import board_sfen_main
from common import LearningBook, Stopwatch, format_duration, position_key
from csa_source import CsaSource, GameRef, SeenStore
from problem import Problem, open_book_repo, replay_moves
from selector import make_tie_breaker
from sfen_guard import safe_set_sfen
from usi_engine import EngineError, SearchResult, UsiEngine

#: 検討中はエンジンに投了させない (評価値そのものが欲しいので)
ANALYSIS_RESIGN_VALUE = 99999

#: 取り込みが 0 手で終わった理由 (ログ表示用)
NO_PROBLEM = "課題局面が現れない"
NO_DECISION = "決着せず"
NO_TARGET = "勝ち側の登録対象なし"
BAD_KIFU = "棋譜を読めない"

#: 指し手の出所
SRC_ABSOLUTE = "absolute"   # 高優先の定跡 (学習で書き換えない)
SRC_BOOK = "book"           # 学習定跡
SRC_ENGINE = "engine"       # エンジンの思考

BLACK, WHITE = cshogi.BLACK, cshogi.WHITE


def side_name(turn: int) -> str:
    return "先手" if turn == BLACK else "後手"


def _changes(added: int, removed: int) -> str:
    """「N手を登録 / M手を削除」の表示 (削除0なら省く)。"""
    text = f"{added}手を登録"
    return f"{text} / {removed}手を削除" if removed else text


@dataclass
class PlayedMove:
    sfen_main: str
    usi: str
    turn: int
    source: str


@dataclass
class GameOutcome:
    winner: int | None          # None = 決着なし (打ち切り)
    reason: str
    moves: list[PlayedMove] = field(default_factory=list)
    ply: int = 0


@dataclass
class CycleStats:
    problem: Problem
    precedents_used: int = 0
    precedent_moves: int = 0
    precedent_removed: int = 0
    self_play_games: int = 0
    streak_side: int | None = None
    timings: dict[str, float] = field(default_factory=dict)


class CycleRunner:
    """設定・エンジン・定跡を束ねてサイクルを回す。"""

    def __init__(self, config, book: LearningBook, engine: UsiEngine,
                 source: CsaSource, seen: SeenStore, book_dir: str,
                 report=print, rng: random.Random | None = None,
                 save_book=lambda: None):
        self.config = config
        self.book = book
        # 途中保存。1サイクルは長いので、強制終了で学習を失わないよう
        # 区切りごとに書き出す (tmp→os.replace なので対局中でも安全)。
        self._save_book = save_book
        self.engine = engine
        self.source = source
        self.seen = seen
        self.book_dir = book_dir
        self.report = report
        self.rng = rng or random.Random()
        self.stop_requested = False
        # 高優先定跡 (対象定跡より上) = 学習で触らない層。
        # ラッパーの採用ロジックをそのまま使うので、自己対局が実戦と同じ
        # 手順を辿る。
        self.absolute_repo = open_book_repo(book_dir,
                                            config.target_priority())
        self.tie_breaker = make_tie_breaker("deep")

    def save_book(self) -> None:
        """途中保存。失敗しても中断しない (次の保存点で書ければよい)。

        最終的な書き出しは呼び出し側 (condenser.run_cycle) が行い、
        そこで失敗したときだけタスクを未完了として残す。
        """
        try:
            self._save_book()
        except OSError as e:
            self.report(f"      途中保存に失敗 (続行): {e}")

    # ==================================================================
    # サイクル本体
    # ==================================================================

    def run(self, problem: Problem) -> CycleStats:
        stats = CycleStats(problem)
        self.report(f"\n=== 課題局面: {problem.sfen_main}")
        self.report(f"    由来: {problem.origin}")
        if problem.source_book:
            # 「定跡どおりか」は**候補集合**で見る。タイブレークで選ばれた
            # 1手と比べると、登録済みの別の候補を指しただけで分岐扱いに
            # なってしまう (複数手を意図的に登録している局面がある)。
            candidates = problem.book_candidates
            if candidates and problem.source_move not in candidates:
                others = "/".join(candidates)
                note = f" ※定跡の候補は {others} = 外された変化"
            elif len(candidates) > 1:
                note = f" (定跡の候補{len(candidates)}手のうちの1つ)"
            else:
                note = ""
            self.report(f"    直前手: {problem.source_move} "
                        f"({problem.source_book}){note}")

        merged_pos, merged_moves = self.merge_high_priority_books()
        if merged_moves:
            self.report(f"    高優先定跡を取り込み: +{merged_pos}局面 / "
                        f"+{merged_moves}手")

        with Stopwatch(self.report, "前例の取り込み") as sw:
            used, added, removed = self.import_precedents(problem)
            stats.precedents_used = used
            stats.precedent_moves = added
            stats.precedent_removed = removed
        stats.timings["precedent"] = sw.elapsed

        if self.stop_requested:
            return stats

        with Stopwatch(self.report, "自己対局") as sw:
            games, streak_side = self.self_play(problem)
            stats.self_play_games, stats.streak_side = games, streak_side
        stats.timings["selfplay"] = sw.elapsed
        return stats

    def merge_high_priority_books(self) -> tuple[int, int]:
        """優先度の高い定跡 (既定: 0番台) を対象定跡へ複製する。

        対象定跡ひとつを開けば全体が追えるようにするための**閲覧用の
        複製**。採用は元のファイル (優先度が上) で行われるので、複製が
        学習で削られても書き換わっても実戦の手順は変わらない。毎サイクル
        取り込み直すため、元のファイルを編集すれば次のサイクルで反映
        される。追加した (局面数, 指し手数) を返す。
        """
        limit = self.config.get_int("MergeBookPriority")
        if limit < 0:
            return 0, 0
        added_pos = added_moves = 0
        for info in self.absolute_repo.files:
            # .ybb は別形式なので複製しない (テキスト db のみ)
            if info.priority > limit or not info.path.lower().endswith(".db"):
                continue
            try:
                source = LearningBook.load(info.path)
            except OSError as e:
                self.report(f"    取り込めません ({info.name}): {e}")
                continue
            for sfen_main, moves in source.entries.items():
                fresh = sfen_main not in self.book.entries
                for usi, count in moves.items():
                    if usi in self.book.get(sfen_main):
                        continue
                    self.book.add(sfen_main, usi, max(1, count))
                    added_moves += 1
                    if fresh:
                        added_pos += 1
                        fresh = False
        return added_pos, added_moves

    # ==================================================================
    # 前例の取り込み
    # ==================================================================

    def import_precedents(self, problem: Problem) -> tuple[int, int, int]:
        total_added = 0
        total_removed = 0
        used = 0

        # 1) 課題局面の由来になった棋譜そのもの。放流対局が一番効く材料なので、
        #    前例と同じロジックで取り込む (捨てない)。
        if problem.source_moves:
            started = time.monotonic()
            added, removed, reason = self._import_source_kifu(problem)
            if added or removed:
                self.save_book()      # 1局ぶんずつ確定させる
            elapsed = time.monotonic() - started
            self.report(f"    [由来棋譜] {_changes(added, removed)}"
                        f"{f' ({reason})' if reason else ''} "
                        f"({format_duration(elapsed)})")
            total_added += added
            total_removed += removed
            if added or removed:
                used += 1

        # 2) csa.db の前例
        limit = self.config.get_int("PrecedentGames")
        key = position_key(problem.sfen_main)
        refs = self.source.games_at_position(key, limit, self.seen)
        if not refs:
            self.report("  前例なし (未取得の対局が見つかりません)")
            return used, total_added, total_removed
        self.report(f"  前例 {len(refs)}局を取得")

        for index, ref in enumerate(refs, start=1):
            if self.stop_requested:
                self.report(f"    残り{len(refs) - index + 1}局は未処理のまま "
                            f"(取得済みにしないので次回また引ける)")
                break
            started = time.monotonic()
            added, removed, reason = self._import_one(ref, problem, index,
                                                       len(refs))
            # **順序が大事**: 先に定跡を保存してから取得済みにする。
            # 逆だと、強制終了したときに「前例は消費済みなのに学習内容は
            # 失われた」状態になり、その棋譜を二度と引けなくなる。
            if added or removed:
                self.save_book()
            if self.seen is not None:
                self.seen.mark([ref.game_id], f"pos:{key}")
            elapsed = time.monotonic() - started
            self.report(f"    [{index}/{len(refs)}] game {ref.game_id}: "
                        f"{_changes(added, removed)}"
                        f"{f' ({reason})' if reason else ''} "
                        f"({format_duration(elapsed)})")
            total_added += added
            total_removed += removed
            if added or removed:
                used += 1
        return used, total_added, total_removed

    def _import_source_kifu(self, problem: Problem) -> tuple[int, int, str]:
        """課題局面を決めた棋譜自身を取り込む。"""
        board = cshogi.Board()
        if problem.source_sfen:
            if safe_set_sfen(board, problem.source_sfen) is not None:
                return 0, 0, BAD_KIFU
        return self._analyse_and_register(board, problem.source_moves, problem)

    def _import_one(self, ref: GameRef, problem: Problem,
                    index: int, total: int) -> tuple[int, int, str]:
        game = self.source.game(ref.game_id)
        if game is None or not game.moves:
            return 0, 0, BAD_KIFU

        board = cshogi.Board()
        if game.initial_sfen:
            # 外部データも検証してから渡す (不正SFENはプロセスを落とす)
            if safe_set_sfen(board, game.initial_sfen) is not None:
                return 0, 0, BAD_KIFU
        return self._analyse_and_register(board, game.moves, problem)

    def _analyse_and_register(self, start_board, moves: list[str],
                              problem: Problem) -> tuple[int, int, str]:
        """棋譜を課題局面の最後の出現から検討し、勝ち側の手を登録する。

        前例も由来棋譜も同じ経路を通す (登録規則を1本にしておく)。
        """
        # 棋譜を再生し、課題局面が現れた最後の手数を探す
        boards = [_copy(start_board)]
        replay = _copy(start_board)
        applied = replay_moves(replay, moves)
        cursor = _copy(boards[0])
        for usi in applied:
            cursor.push_usi(usi)
            boards.append(_copy(cursor))

        last = -1
        for i, snapshot in enumerate(boards):
            if board_sfen_main(snapshot) == problem.sfen_main:
                last = i
        if last < 0:
            # 定跡の採用手と実戦の手が違えば課題局面は棋譜に現れない
            # (相手番でヒットした場合など)。
            return 0, 0, NO_PROBLEM

        decisive = self.config.get_int("DecisiveEval")
        nodes = self.config.get_int("AnalysisNodes")
        sequence: list[PlayedMove] = []
        winner: int | None = None

        for i in range(last, len(applied)):
            if self.stop_requested:
                break
            snapshot = boards[i]
            position_cmd = self._position_cmd(boards[0], applied[:i])
            try:
                result = self.engine.search(position_cmd, nodes)
            except EngineError as e:
                self.report(f"      エンジンエラー (この前例を中断): {e}")
                self._recover_engine()
                break
            score_black = (result.score if snapshot.turn == BLACK
                           else -result.score)
            if abs(score_black) >= decisive:
                winner = BLACK if score_black > 0 else WHITE
                self.report(f"      {i - last}手目で決着扱い "
                            f"(評価値 {score_black:+d}, "
                            f"{side_name(winner)}勝ち)")
                break
            sequence.append(PlayedMove(board_sfen_main(snapshot), applied[i],
                                       snapshot.turn, SRC_ENGINE))

        if winner is None and not self.stop_requested:
            # 棋譜が決着しないまま終わった (入玉模様など評価値が伸びない
            # 将棋)。捨てずに、その局面から自己対局と同じ設定で指し継いで
            # 決着させる。投了値が効くので大差なら数手で決まる。
            played = len(sequence)
            winner = self._play_out(boards[0], applied, sequence)
            if winner is not None:
                self.report(f"      棋譜が決着せず → {len(sequence) - played}手"
                            f"指し継いで {side_name(winner)}勝ち")

        if winner is None:
            return 0, 0, NO_DECISION

        # 勝ち側は加算。同一局面は「最後に出てきたときの指し手」だけを採る
        # ので、1つの棋譜から同じ局面に2つの手が入ることはない。
        chosen: dict[str, str] = {}
        for played in sequence:
            if played.turn == winner:
                chosen[played.sfen_main] = played.usi
        for sfen_main, usi in chosen.items():
            self.book.add(sfen_main, usi, 1)

        # 負け側は自己対局と同じ規則で削除する。加算だけだと負け筋が
        # 定跡に残り続け、候補が増える一方になる。
        removed = self.prune_losing_line(sequence, _opponent(winner))
        reason = "" if (chosen or removed) else NO_TARGET
        return len(chosen), removed, reason

    def _play_out(self, start_board, kifu_moves: list[str],
                  sequence: list[PlayedMove]) -> int | None:
        """棋譜の続きを指して決着させる。決まらなければ None。

        自己対局と同じ SelfPlayNodes / ResignValue を使う。指した手は
        sequence に足すので、勝ち側の手として登録対象になる
        (棋譜由来の手と同じ規則で扱う)。
        """
        board = _copy(start_board)
        for usi in kifu_moves:
            board.push_usi(usi)
        moves = list(kifu_moves)
        nodes = self.config.get_int("SelfPlayNodes")
        decisive = self.config.get_int("DecisiveEval")
        max_ply = self.config.get_int("MaxPly")
        draw_winner = (WHITE if self.config.get("DrawWinner").lower() == "white"
                       else BLACK)
        start_sfen = start_board.sfen()

        self._set_resign_value(self.config.get_int("ResignValue"))
        try:
            while not self.stop_requested:
                if board.is_game_over():
                    return _opponent(board.turn)
                repetition = board.is_draw()
                if repetition == cshogi.REPETITION_DRAW:
                    return draw_winner
                if repetition == cshogi.REPETITION_WIN:
                    return board.turn
                if repetition == cshogi.REPETITION_LOSE:
                    return _opponent(board.turn)
                if len(moves) >= max_ply:
                    return draw_winner

                try:
                    result = self.engine.search(
                        self._position_cmd_from(start_sfen, moves), nodes)
                except EngineError as e:
                    self.report(f"      エンジンエラー (指し継ぎ中断): {e}")
                    self._recover_engine()
                    return None

                if result.bestmove == "resign":
                    return _opponent(board.turn)
                if result.bestmove == "win":
                    return board.turn
                score_black = (result.score if board.turn == BLACK
                               else -result.score)
                if abs(score_black) >= decisive:
                    return BLACK if score_black > 0 else WHITE

                try:
                    move = board.move_from_usi(result.bestmove)
                except Exception:
                    move = 0
                if move == 0 or not board.is_legal(move):
                    return None
                sequence.append(PlayedMove(board_sfen_main(board),
                                           result.bestmove, board.turn,
                                           SRC_ENGINE))
                board.push(move)
                moves.append(result.bestmove)
        finally:
            self._set_resign_value(ANALYSIS_RESIGN_VALUE)
        return None

    def _set_resign_value(self, value: int) -> None:
        try:
            self.engine.set_option("ResignValue", value)
            self.engine.isready()
        except Exception as e:
            self.report(f"      ResignValue を設定できません: {e!r}")

    # ==================================================================
    # 自己対局
    # ==================================================================

    def self_play(self, problem: Problem) -> tuple[int, int | None]:
        target = self.config.get_int("WinStreak")
        streak = 0
        streak_side: int | None = None
        games = 0

        self.engine.set_option("ResignValue", self.config.get_int("ResignValue"))
        draw_winner = self.config.get("DrawWinner").lower()
        black_draw = -9999 if draw_winner == "white" else 9999
        self.engine.set_option("DrawValueBlack", black_draw)
        self.engine.set_option("DrawValueWhite", -black_draw)
        self.engine.isready()

        while streak < target and not self.stop_requested:
            started = time.monotonic()
            outcome = self.play_game(problem)
            games += 1
            elapsed = time.monotonic() - started
            if outcome.winner is None:
                self.report(f"    第{games}局: 決着なし ({outcome.reason}) "
                            f"{outcome.ply}手 ({format_duration(elapsed)})")
                streak, streak_side = 0, None
                continue

            self.apply_learning(outcome)
            self.save_book()      # 1局ごとに確定 (強制終了で失わない)
            if outcome.winner == streak_side:
                streak += 1
            else:
                streak_side, streak = outcome.winner, 1
            self.report(
                f"    第{games}局: {side_name(outcome.winner)}勝ち "
                f"({outcome.reason}, {outcome.ply}手) "
                f"→ {streak}連勝 / 定跡 {len(self.book)}局面 "
                f"({format_duration(elapsed)})")
        return games, streak_side

    def play_game(self, problem: Problem) -> GameOutcome:
        board = cshogi.Board()
        board.set_sfen(problem.sfen)
        start_sfen = board.sfen()
        nodes = self.config.get_int("SelfPlayNodes")
        max_ply = self.config.get_int("MaxPly")
        draw_winner = (WHITE if self.config.get("DrawWinner").lower() == "white"
                       else BLACK)
        moves: list[str] = []
        played: list[PlayedMove] = []

        while True:
            if self.stop_requested:
                return GameOutcome(None, "停止要求", played, len(moves))
            if board.is_game_over():
                return GameOutcome(_opponent(board.turn), "詰み",
                                   played, len(moves))
            repetition = board.is_draw()
            if repetition == cshogi.REPETITION_DRAW:
                return GameOutcome(draw_winner, "千日手", played, len(moves))
            if repetition == cshogi.REPETITION_WIN:
                return GameOutcome(board.turn, "連続王手の千日手",
                                   played, len(moves))
            if repetition == cshogi.REPETITION_LOSE:
                return GameOutcome(_opponent(board.turn), "連続王手の千日手",
                                   played, len(moves))
            if len(moves) >= max_ply:
                return GameOutcome(draw_winner, f"最大手数({max_ply})",
                                   played, len(moves))

            usi, source = self.choose_move(board, start_sfen, moves, nodes)
            if usi is None:
                return GameOutcome(None, "エンジン応答なし", played, len(moves))
            if usi == "resign":
                return GameOutcome(_opponent(board.turn), "投了",
                                   played, len(moves))
            if usi == "win":
                return GameOutcome(board.turn, "入玉宣言", played, len(moves))

            try:
                move = board.move_from_usi(usi)
            except Exception:
                move = 0
            if move == 0 or not board.is_legal(move):
                # 定跡の壊れた手で対局を壊さない。その手を消してやり直す。
                if self.book.remove(board_sfen_main(board), usi):
                    self.report(f"      非合法手を定跡から除去: {usi}")
                    continue
                # 定跡に無い = エンジンが返した手。消すものが無いので
                # やり直しても同じ手が返り、同じ局面を延々探索し続ける。
                # 無人で回るので、この対局は捨てて次へ進む。
                self.report(f"      エンジンが非合法手を返しました: {usi} "
                            f"(この対局を打ち切ります)")
                return GameOutcome(None, "エンジンが非合法手を返した",
                                   played, len(moves))

            played.append(PlayedMove(board_sfen_main(board), usi,
                                     board.turn, source))
            board.push(move)
            moves.append(usi)

    def choose_move(self, board, start_sfen: str, moves: list[str],
                    nodes: int) -> tuple[str | None, str]:
        """高優先定跡 → 学習定跡 → エンジン の順で指し手を決める。

        高優先定跡はラッパーと同じ採用ロジックを使うので、自己対局が
        実戦と同じ手順を辿る。
        """
        hit, _ = self.absolute_repo.probe(board, 1, self.tie_breaker, self.rng)
        if hit is not None:
            return hit.usi, SRC_ABSOLUTE

        candidates = self.book.get(board_sfen_main(board))
        if candidates:
            # makebook4 と同じく等確率で選ぶ (負けた手は学習で削られる)
            return self.rng.choice(sorted(candidates)), SRC_BOOK

        position_cmd = self._position_cmd_from(start_sfen, moves)
        try:
            result: SearchResult = self.engine.search(position_cmd, nodes)
        except EngineError as e:
            self.report(f"      エンジンエラー: {e}")
            self._recover_engine()
            return None, SRC_ENGINE
        return result.bestmove, SRC_ENGINE

    # ==================================================================
    # 学習 (makebook4 のルール)
    # ==================================================================

    def prune_losing_line(self, played: list[PlayedMove], loser: int) -> int:
        """負け側の定跡手を末尾から遡って削除する (makebook4 の規則)。

        **消しても選択肢が残る局面** (= 本当の分岐点) に当たったところで
        止める。そこまでは一本道なので、末端だけ消しても意味がない。
        高優先定跡由来の手は触らない。削除した手数を返す。

        前例取り込みと自己対局の両方で使う — どちらも「負けた手順は
        定跡から外す」という同じ判断であるべきなので、規則を1本にしておく。
        """
        stopped = False
        removed = 0
        for move in reversed(played):
            if move.source == SRC_ABSOLUTE or move.turn != loser or stopped:
                continue
            moves = self.book.get(move.sfen_main)
            if move.usi in moves:
                remained = len(moves) > 1
                self.book.remove(move.sfen_main, move.usi)
                removed += 1
                if remained:
                    stopped = True
        return removed

    def apply_learning(self, outcome: GameOutcome) -> None:
        """勝ち側の手は加算、負け側は「最後の定跡手」から遡って削除。

        高優先定跡由来の手は触らない。加算と削除は手番が違う局面を
        触るので、順序を分けても結果は変わらない。
        """
        if outcome.winner is None:
            return
        self.prune_losing_line(outcome.moves, _opponent(outcome.winner))
        for played in outcome.moves:
            if played.source == SRC_ABSOLUTE:
                continue
            if played.turn == outcome.winner:
                self.book.add(played.sfen_main, played.usi, 1)

    # ==================================================================
    # 補助
    # ==================================================================

    def _position_cmd(self, start_board, moves: list[str]) -> str:
        return self._position_cmd_from(start_board.sfen(), moves)

    @staticmethod
    def _position_cmd_from(start_sfen: str, moves: list[str]) -> str:
        base = f"sfen {start_sfen}"
        return f"{base} moves {' '.join(moves)}" if moves else base

    def _recover_engine(self) -> None:
        try:
            self.engine.restart()
            self.report("      エンジンを再起動しました")
        except Exception as e:
            self.report(f"      エンジンの再起動に失敗: {e!r}")
            self.stop_requested = True


def _opponent(turn: int) -> int:
    return WHITE if turn == BLACK else BLACK


def _copy(board):
    clone = cshogi.Board()
    clone.set_sfen(board.sfen())
    return clone
