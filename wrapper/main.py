"""KiriCadence 定跡USIラッパー本体 (DESIGN.md §8)。

go を受けたらまず book/ の優先定跡を判定し、ヒットしたらエンジンには
転送せず直接 bestmove を返す。定跡が介入するのは時間制御付きの通常対局
go のみで、go infinite / go mate は素通し。go ponder は素通しして
ponderhit の瞬間に定跡を確認する。

状態: IDLE / SEARCH (goを転送し bestmove/checkmate 待ち) /
RECOVERY (エンジン再起動中 — この間のエンジン出力はGUIへ中継しない)。

状態機械の構造は custom_engine3 v2.1〜v3 (複数回の査読で実バグを潰した
実績のあるもの) を踏襲し、csa.db 前例段を削除、定跡段を book_repo に
差し替えた。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import random
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cshogi  # type: ignore

from book_repo import BookRepository, board_sfen_main
from config import Config, ConfigError, load_config, reload_book_settings
from decision_log import DecisionLogger
from engine_proxy import EngineProxy
from selector import make_tie_breaker
from sfen_guard import validate_sfen
from task_emitter import TaskEmitter

VERSION = "1.3"

IDLE, SEARCH, RECOVERY = 0, 1, 2

#: エンジン再起動のリトライ回数 (1発勝負で resign しない。§8)
RECOVERY_ATTEMPTS = 3
#: 再起動ハンドシェイクの応答待ち上限 (超えたらエンジンを殺して失敗扱い)。
#: readyok は評価関数ロードで時間がかかり得るため長めに取る。
USIOK_TIMEOUT = 30.0
READYOK_TIMEOUT = 180.0
#: reader ループで連続エラーがこの回数に達したら読み取りを諦める
#: (壊れたハンドルでの無限スピンを避ける)。
READER_MAX_ERRORS = 20

OWN_OPTIONS = (
    "option name BookEnabled type check default true",
)

#: 決定ログの一括miss理由 (§5.1)
REASON_REPETITION = "repetition_guard"


class Wrapper:
    def __init__(self, config: Config):
        self.config = config
        os.makedirs(config.log_dir, exist_ok=True)
        self.log_path = os.path.join(config.log_dir, "wrapper.log")
        self.log(f"=== KiriCadence v{VERSION} started (pid={os.getpid()}) ===")
        self.log(f"[Config] {config.config_path}")

        self.engine = EngineProxy(config.engine_path, config.engine_encoding,
                                  self.log)
        self.repo = BookRepository(self.log)
        self.repo.refresh(config.book_dir)
        self.tie_breaker = make_tie_breaker(config.tie_break)
        self.decisions = DecisionLogger(
            os.path.join(config.log_dir, "decisions.jsonl"), self.log)
        self.rng = random.Random()
        # 対局中に勝敗が見えた時点で定跡修正タスクを積む (連携骨格)
        self.task_emitter = TaskEmitter(config.task_dir,
                                        config.task_eval_threshold,
                                        config.task_emit_on, self.log)
        if self.task_emitter.enabled:
            self.log(f"[Task] 連携有効: {config.task_dir} "
                     f"(閾値 {config.task_eval_threshold} / "
                     f"条件 {config.task_emit_on})")

        # --- 状態 ---
        self.state = IDLE
        self.state_lock = threading.Lock()
        self.print_lock = threading.Lock()
        self.recovery_lock = threading.Lock()
        self.in_usi_handshake = False
        self.recovered_this_search = False
        # 放棄した探索の残骸bestmove/checkmateを握りつぶすフラグ
        self.suppress_next_bestmove = False

        # --- 局面追跡 ---
        self.board = None                      # cshogi.Board
        self.ply = 0                           # 対局開始からの消化手数
        self.position_counts: Counter = Counter()  # 手数なしSFEN -> 出現回数
        self.tracking_ok = False

        # --- 再起動リプレイ用 ---
        self.last_position_cmd: str | None = None
        self.last_go_cmd: str | None = None
        self.game_started = False
        self.option_journal: dict[str, str] = {}   # name -> setoption行
        self.button_options: set[str] = set()

        # --- 自前オプション ---
        self.book_enabled = True

        self.reader_thread: threading.Thread | None = None

    # -- 基盤 ---------------------------------------------------------------

    def log(self, message: str) -> None:
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    def send_gui(self, *lines: str) -> None:
        with self.print_lock:
            for line in lines:
                print(line)
            sys.stdout.flush()

    def _start_reader(self) -> None:
        self.reader_thread = threading.Thread(target=self._engine_reader,
                                              daemon=True)
        self.reader_thread.start()

    # -- エンジン出力 (reader thread) ----------------------------------------

    def _engine_reader(self) -> None:
        """エンジン出力の中継ループ。

        **ループ本体を丸ごと例外捕捉する**のが要点 (v1.3)。このスレッドが
        死ぬと以降エンジン出力が一切GUIへ流れず、無音のハングになる。
        行の解釈だけでなく、read_line (プロセスハンドルの差し替えと競合し得る)
        と EOF処理 (再起動・監視タイマーを伴う重い経路) も保護対象に含める。
        """
        errors = 0
        while True:
            try:
                line = self.engine.read_line()
                if not line:
                    if self._handle_engine_eof():
                        errors = 0
                        continue
                    return
                line = line.rstrip("\r\n")
                if line.strip():   # 空白のみの行は head 抽出で落ちるので捨てる
                    self._handle_engine_line(line)
                errors = 0
            except Exception as e:
                errors += 1
                self.log(f"[Engine] 出力処理で予期しないエラー "
                         f"({errors}/{READER_MAX_ERRORS}, 継続): {e!r}")
                if errors >= READER_MAX_ERRORS:
                    self.log("[Engine] 連続エラーが上限に達したため "
                             "reader を終了します")
                    self._resign_if_searching("reader停止")
                    return
                time.sleep(0.05)   # 壊れたハンドルでのスピンを避ける

    def _resign_if_searching(self, reason: str) -> bool:
        """GUIがbestmoveを待ったまま取り残されるのを防ぐ最後の手当て。

        探索中でなければ何もしない。呼ぶ側が「もうbestmoveを出せない」と
        判断した場合にだけ使う (ハングより投了の方がまだ良い)。"""
        with self.state_lock:
            if self.state != SEARCH:
                return False
            self.state = IDLE
        self.log(f"[Wrapper] {reason} のため bestmove resign を返します")
        self.send_gui("bestmove resign")
        return True

    def _handle_engine_line(self, line: str) -> None:
        with self.state_lock:
            state = self.state
        if state == RECOVERY:
            return  # RECOVERY中の出力はGUIへ中継しない (§8)

        head = line.split(maxsplit=1)[0]

        # 握りつぶし待ちの間の info は放棄した探索の出力なので流さない
        # (エンジンは逐次処理のため、残骸bestmoveより前に届くinfoは必ず
        #  放棄した探索のもの。info以外は一切対象にしない — readyok等の
        #  ハンドシェイク応答を巻き込まないため)。
        if head == "info":
            with self.state_lock:
                if self.suppress_next_bestmove:
                    return
            if self.task_emitter.enabled:
                # 例外で中継を止めないこと。タスク投入は対局より優先度が低い
                try:
                    self.task_emitter.observe(
                        line, self.last_position_cmd,
                        self.decisions.game_id)
                except Exception as e:
                    self.log(f"[Task] 投入に失敗 (対局は続行): {e!r}")

        if head in ("bestmove", "checkmate"):
            with self.state_lock:
                if self.suppress_next_bestmove:
                    self.suppress_next_bestmove = False
                    self.log(f"[Engine] 放棄済み探索の{head}を抑制: {line}")
                    return
                if self.state == SEARCH:
                    self.state = IDLE
                    self.recovered_this_search = False
                else:
                    self.log(f"[Engine] 迷子の{head}を破棄: {line}")
                    return
            self.send_gui(line)  # ponder句もそのまま中継
            return

        if self.in_usi_handshake:
            if head == "option":
                name = self._option_name(line)
                if "type button" in line and name:
                    self.button_options.add(name)
            elif line == "usiok":
                self.in_usi_handshake = False
                self.send_gui(*OWN_OPTIONS, "usiok")
                return
        self.send_gui(line)

    @staticmethod
    def _option_name(line: str) -> str | None:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "name":
            return parts[2]
        return None

    def _handle_engine_eof(self) -> bool:
        """reader threadでのEOF。復旧できたら True (ループ継続)。"""
        with self.state_lock:
            searching = (self.state == SEARCH)
        self.log(f"[Engine] EOF (searching={searching})")
        if searching and not self.recovered_this_search:
            self.recovered_this_search = True
            if self._recover(resend_go=True):
                return True
        if searching:
            with self.state_lock:
                self.state = IDLE
            self.send_gui("bestmove resign")
        return False

    # -- エンジン再起動 (§8) -------------------------------------------------

    def _recover(self, resend_go: bool) -> bool:
        with self.recovery_lock:
            with self.state_lock:
                self.state = RECOVERY
            ok = False
            for attempt in range(1, RECOVERY_ATTEMPTS + 1):
                self.log(f"[Recovery] エンジン再起動... "
                         f"({attempt}/{RECOVERY_ATTEMPTS})")
                ok = (self.engine.restart()
                      and self._handshake_after_restart())
                if ok:
                    break
                time.sleep(0.5 * attempt)   # 一時的な起動失敗に備える
            with self.state_lock:
                # 新プロセスに旧探索の残骸は存在しない
                self.suppress_next_bestmove = False
                if ok and resend_go and self.last_go_cmd:
                    self.state = SEARCH
                else:
                    self.state = IDLE
            if not ok:
                self.log("[Recovery] 失敗")
                return False
            if resend_go and self.last_go_cmd:
                self.engine.send(self.last_go_cmd)
            self.log("[Recovery] 完了")
            return True

    def _handshake_after_restart(self) -> bool:
        """usi→usiok, journal再送, isready→readyok, usinewgame, position再送。
        この間の出力はGUIへ渡さない (usiok/readyok は待機解除にのみ使う)。"""
        self.engine.send("usi")
        if not self._wait_token("usiok", USIOK_TIMEOUT):
            return False
        for line in self.option_journal.values():
            self.engine.send(line)
        self.engine.send("isready")
        if not self._wait_token("readyok", READYOK_TIMEOUT):
            return False
        if self.game_started:
            self.engine.send("usinewgame")
        if self.last_position_cmd:
            self.engine.send(self.last_position_cmd)
        return True

    def _wait_token(self, token: str, timeout: float) -> bool:
        """token が来るまで読む。timeout 秒応答が無ければエンジンを殺して
        False (killすると read_line が EOF を返すので安全に抜けられる。
        生きているが無反応のエンジンで RECOVERY に籠城しないための保険)。

        待機終了と発火が競合しても正常なエンジンを殺さないよう、
        「まだ待機中か」を専用ロックで確認してから terminate する
        (Timer.cancel は既に走り出したタイマーを止められないため)。"""
        waiting = [True]
        guard = threading.Lock()

        def on_timeout() -> None:
            with guard:
                if not waiting[0]:
                    return          # 既にtokenが来ている。殺さない
                waiting[0] = False
            self.log(f"[Recovery] {token} 応答待ちタイムアウト。"
                     f"エンジンを強制終了します")
            self.engine.terminate()

        watchdog = threading.Timer(timeout, on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            while True:
                line = self.engine.read_line()
                if not line:
                    return False
                if line.strip() == token:
                    return True
        finally:
            with guard:
                waiting[0] = False
            watchdog.cancel()

    def _abandon_search(self, reason: str) -> None:
        """未完了の探索を放棄する (stop+suppress の安全網)。

        探索中に position / usinewgame / gameover / 新しいgo を受けた場合、
        stopを送り、これから届く残骸のbestmove/checkmateを握りつぶす。
        これが無いと、放棄した探索の応答が次のgoの答えとしてGUIに
        誤送信され得る (致命的)。"""
        with self.state_lock:
            if self.state != SEARCH:
                return
            self.state = IDLE
            self.suppress_next_bestmove = True
        self.log(f"[Wrapper] 未完了の探索を放棄 ({reason})")
        self.engine.send("stop")
        for _ in range(50):  # 最大0.5秒、残骸の握りつぶし完了を待つ
            with self.state_lock:
                if not self.suppress_next_bestmove:
                    return
            time.sleep(0.01)
        self.log("[Wrapper] stop応答待ちタイムアウト (フラグ保持で継続)")

    def _ensure_engine_for_go(self) -> bool:
        """go転送前のエンジン死活確認。死んでいたら1回だけ再起動を試す。"""
        if self.engine.alive():
            return True
        if not self.config.engine_path:
            return False  # 定跡専用モード
        old_reader = self.reader_thread
        if old_reader is not None and old_reader.is_alive() \
                and old_reader is not threading.current_thread():
            old_reader.join(timeout=1.0)
        if not self._recover(resend_go=False):
            return False
        self._start_reader()
        return True

    # -- 局面追跡 -----------------------------------------------------------

    def _track_position(self, parts: list[str]) -> None:
        """position コマンドから現局面と局面出現回数 (§5.1) を再構築する。"""
        self.tracking_ok = False
        try:
            board = cshogi.Board()
            ply0 = 0
            if len(parts) < 2 or parts[1] == "startpos":
                moves_idx = 2
            elif parts[1] == "sfen":
                moves_idx = (parts.index("moves") if "moves" in parts
                             else len(parts))
                sfen_tokens = parts[2:moves_idx]
                sfen = " ".join(sfen_tokens)
                # cshogi の set_sfen は不正な SFEN でプロセスごと落ちるため、
                # 必ず事前検証する (try/except では受けられない)。
                error = validate_sfen(sfen)
                if error:
                    raise ValueError(f"不正なsfen: {error}")
                board.set_sfen(sfen)
                if len(sfen_tokens) >= 4 and sfen_tokens[3].isdigit():
                    ply0 = max(0, int(sfen_tokens[3]) - 1)
            else:
                raise ValueError(f"unknown position kind: {parts[1]}")

            counts: Counter = Counter()
            counts[board_sfen_main(board)] += 1
            count = 0
            if len(parts) > moves_idx and parts[moves_idx] == "moves":
                for token in parts[moves_idx + 1:]:
                    board.push_usi(token)
                    counts[board_sfen_main(board)] += 1
                    count += 1
            self.board = board
            self.ply = ply0 + count
            self.position_counts = counts
            self.tracking_ok = True
        except Exception as e:  # 追跡失敗 → この局面では定跡を引かない (§8)
            self.log(f"[Track] position解析失敗 (定跡を引かずに続行): {e!r}")

    # -- 定跡判定 (§5) -------------------------------------------------------

    def _book_decision(self):
        """定跡判定のみ。(info行list, usi手) を返す。無ければ None。"""
        if not self.book_enabled:
            return None
        sfen_main = board_sfen_main(self.board)
        is_black = (self.board.turn == cshogi.BLACK)
        side = "b" if is_black else "w"

        # §5.1 千日手: 先手番で既出2回目以降の局面は定跡フル無視。
        # 後手番は千日手歓迎なので定跡を使い切る。
        if is_black and self.position_counts.get(sfen_main, 0) >= 2:
            self.log(f"[Book] 既出局面 (x{self.position_counts[sfen_main]}) "
                     f"のため先手は定跡を無視 (千日手回避)")
            self.decisions.miss(sfen_main, self.ply, side,
                                reason=REASON_REPETITION)
            return None

        hit, misses = self.repo.probe(self.board, self.config.book_min_depth,
                                      self.tie_breaker, self.rng)
        if hit is None:
            self.decisions.miss(sfen_main, self.ply, side, files=misses)
            return None

        tie = ""
        if hit.tie_candidates > 1:
            tie = (f" tie={hit.tie_candidates}@第{hit.decided_at}応手"
                   if hit.decided_at else f" tie={hit.tie_candidates}")
        self.log(f"[Book] {hit.file_name}: {hit.usi} (value={hit.value} "
                 f"depth={hit.depth}{' flip' if hit.flipped else ''}{tie})")
        self.decisions.hit(sfen_main, self.ply, side, hit,
                           self.tie_breaker.name, prior_misses=misses)
        return ([f"info multipv 1 score cp {hit.value} depth {hit.depth} "
                 f"nodes 0 pv {hit.usi} string Book_Hit"
                 f"{'_Flipped' if hit.flipped else ''} "
                 f"book={hit.file_name}"], hit.usi)

    def _try_book(self) -> bool:
        """定跡から指せたら True (GUIへ info+bestmove 送信済み)。"""
        result = self._book_decision()
        if result is None:
            return False
        info_lines, usi = result
        self.send_gui(*info_lines, f"bestmove {usi}")
        return True

    # -- GUIコマンド (main thread) -------------------------------------------

    def run(self) -> None:
        if self.engine.start():
            self._start_reader()
        while True:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            line = line.strip()
            parts = line.split()
            if not parts:
                continue
            try:
                keep_going = self._handle_gui_command(parts[0], parts, line)
            except Exception as e:
                # ここで例外が漏れるとラッパープロセスごと終了してしまう。
                # コマンド1つを捨てて対局を続ける (§8 fail-safe)。
                self.log(f"[Wrapper] コマンド処理で予期しないエラー "
                         f"(無視して継続): {line!r}: {e!r}")
                keep_going = True
            if not keep_going:
                break
        self.engine.terminate()
        self.log("=== KiriCadence stopped ===")

    def _handle_gui_command(self, cmd, parts, line) -> bool:
        if cmd == "usi":
            if self.engine.alive():
                self.in_usi_handshake = True
                self.engine.send("usi")
            else:  # 定跡専用モード (エンジンなしでも定跡だけで応答できる)
                self.send_gui(f"id name KiriCadence v{VERSION} (book only)",
                              "id author KiriCadence",
                              *OWN_OPTIONS, "usiok")

        elif cmd == "isready":
            if self.engine.alive():
                self.engine.send("isready")
            else:
                self.send_gui("readyok")

        elif cmd == "usinewgame":
            self._abandon_search("usinewgame")
            self.game_started = True
            self._reload_book_settings()   # config安全部分の再読込 (§7)
            self.repo.refresh(self.config.book_dir)  # book差し替え反映 (§4.4)
            self.decisions.newgame()
            self.task_emitter.newgame()
            self.engine.send(line)

        elif cmd == "setoption":
            self._handle_setoption(parts, line)

        elif cmd == "position":
            self._abandon_search("new position")
            self._track_position(parts)
            self.last_position_cmd = line
            self.engine.send(line)

        elif cmd == "go":
            self._handle_go(parts, line)

        elif cmd == "ponderhit":
            self._handle_ponderhit()

        elif cmd == "stop":
            with self.state_lock:
                searching = (self.state == SEARCH)
            if searching:
                self.engine.send("stop")

        elif cmd == "quit":
            self.engine.send("quit")
            time.sleep(0.2)
            return False

        elif cmd == "gameover":
            self._abandon_search("gameover")
            self.decisions.gameover(parts[1] if len(parts) > 1 else "")
            self.engine.send(line)

        else:  # 未知コマンドは素通し (前方互換)
            self.engine.send(line)
        return True

    def _reload_book_settings(self) -> None:
        """usinewgame ごとの config 再読込。不正値は前の値を維持 (§7)。"""
        new_config, warnings = reload_book_settings(self.config)
        for message in warnings:
            self.log(f"[Config] {message}")
        if (new_config.book_min_depth != self.config.book_min_depth
                or new_config.tie_break != self.config.tie_break
                or new_config.book_dir != self.config.book_dir):
            self.log(f"[Config] 再読込: BookDir={new_config.book_dir} "
                     f"BookMinDepth={new_config.book_min_depth} "
                     f"TieBreak={new_config.tie_break}")
        self.config = new_config
        self.tie_breaker = make_tie_breaker(self.config.tie_break)

    def _handle_setoption(self, parts, line) -> None:
        name = self._option_name(line)
        value = ""
        if "value" in parts:
            value = " ".join(parts[parts.index("value") + 1:])
        if name == "BookEnabled":
            self.book_enabled = value.strip().lower() != "false"
            self.log(f"[Option] BookEnabled = {self.book_enabled}")
        else:
            if name and name not in self.button_options:
                self.option_journal[name] = line  # button型は記録しない (§8)
            self.engine.send(line)

    def _handle_go(self, parts, line) -> None:
        """go の失敗は GUI を bestmove 待ちで固めるため、投了で閉じる。"""
        try:
            self._handle_go_impl(parts, line)
        except Exception as e:
            self.log(f"[Wrapper] go処理で予期しないエラー: {e!r}")
            with self.state_lock:
                forwarded = (self.state == SEARCH)
            if not forwarded:
                # エンジンへ転送できていない = 誰もbestmoveを返さない
                self.send_gui("info string internal error",
                              "bestmove resign")

    def _handle_go_impl(self, parts, line) -> None:
        self._abandon_search("new go")   # 通常来ないが残骸探索の安全網
        special = any(tok in ("infinite", "mate", "ponder")
                      for tok in parts[1:])
        # 検討モードの評価値は対局と無関係なのでタスク投入の対象外にする
        self.task_emitter.set_game_search(
            not any(tok in ("infinite", "mate") for tok in parts[1:]))
        if not special and self.tracking_ok:
            # fail-safe: 定跡側の予期しない例外は「定跡ミス」として
            # エンジンに思考させる。wrapperのバグで対局を壊さない (§8)。
            try:
                if self._try_book():
                    return  # 定跡ヒット: エンジンには何も転送しない
            except Exception as e:
                self.log(f"[Book] 予期しないエラー "
                         f"(エンジンにフォールスルー): {e!r}")

        if not self._ensure_engine_for_go():
            self.send_gui("info string engine unavailable",
                          "bestmove resign")
            return
        with self.state_lock:
            self.state = SEARCH
            self.recovered_this_search = False
        self.last_go_cmd = line
        self.engine.send(line)

    def _handle_ponderhit(self) -> None:
        """予測が当たった瞬間に定跡を確認する (§8)。

        ヒット: 探索を放棄 (stop+握りつぶし) して定跡手を即指し。
        ミス: ponderhit をエンジンへ転送して思考続行。"""
        result = None
        if self.tracking_ok:
            try:
                result = self._book_decision()
            except Exception as e:  # fail-safe: エンジン続行
                self.log(f"[Book] ponderhitで予期しないエラー: {e!r}")
        if result is not None:
            info_lines, usi = result
            self.log("[Wrapper] ponderhit: 定跡ヒット、探索を放棄して即指し")
            self._abandon_search("ponderhit book hit")
            self.send_gui(*info_lines, f"bestmove {usi}")
            return
        with self.state_lock:
            searching = (self.state == SEARCH)
        if searching:
            self.engine.send("ponderhit")
        else:
            self.log("[Wrapper] 探索なしのponderhitを無視")


def harden_stdio() -> None:
    """stdout/stderr を「壊れた文字で例外を出さない」設定にする。

    GUI/floodgateクライアント起動時の stdout はパイプ = コンソール既定の
    cp932 になり得る。一方エンジン出力は errors='replace' で読むため
    U+FFFD が混ざることがあり、これは cp932 にエンコードできず
    UnicodeEncodeError で中継が死ぬ。エンコーディング自体は変えず
    errors='replace' だけ足す (エンコード不能文字は '?' になる)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main() -> None:
    harden_stdio()
    try:
        config, warnings = load_config()
    except ConfigError as e:
        print(f"info string config error: {e}", flush=True)
        sys.exit(1)
    wrapper = Wrapper(config)
    for message in warnings:
        wrapper.log(f"[Config] {message}")
    wrapper.run()


if __name__ == "__main__":
    main()
