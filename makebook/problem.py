"""課題局面の決定と USI 局面文字列の検証 (合法手チェック込み)。

課題局面の定義 (ユーザー仕様):
  棋譜を終局側から遡り、**対象定跡より優先度の高い定跡だけ**を見て、
  先後や自分が指したかを問わず「採用手が登録されている最後の局面」を探し、
  **そこで実際に指された手を適用した後**の局面を課題局面とする。

  適用するのは定跡の採用手ではなく**棋譜の手**。定跡手を使うと、自分が
  本線だと思っている変化ばかり掘ることになり、実際に相手が外してきた
  変化を調べられない。棋譜がそこで終わっている場合だけ採用手を使う。

  対象定跡自身は判定に含めない。含めるとサイクルが書き足した局面が
  フロンティアになり、課題局面が奥へ流れて定跡が発散する。

手入力の取り違えが定跡を壊さないよう、局面・指し手はすべて cshogi で
合法性を検証してから使う。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import cshogi  # type: ignore

from book_repo import BookRepository, board_sfen_main
from selector import make_tie_breaker
from sfen_guard import validate_sfen


class PositionError(ValueError):
    """手入力・棋譜の局面文字列が不正 (どこで壊れたかを含める)。"""


@dataclass
class Problem:
    """課題局面と、その由来になった棋譜。"""
    sfen: str                 # 手数付きの完全な SFEN (self-play の開始局面)
    sfen_main: str            # 手数なし (定跡キー・csa.db キー)
    origin: str               # 由来の説明 (ログ・タスク表示用)
    source_book: str = ""     # 定跡が切れる直前の局面でヒットしたファイル
    source_move: str = ""     # 実際に適用した手 (原則は棋譜で指された手)
    book_move: str = ""       # タイブレークで選ばれた1手 (表示用)
    #: その局面で定跡が採用し得た手すべて。定跡どおりかの判定はこちらで行う
    #: — book_move は候補が複数あるときタイブレーク結果にすぎず、
    #: それと比べると「登録済みの別の手を指しただけ」まで分岐扱いになる。
    book_candidates: tuple[str, ...] = ()
    # 由来棋譜。前例と同じロジックで取り込むために保持する
    # (放流対局そのものが一番効く材料なので捨てない)。
    source_sfen: str = ""     # 棋譜の開始局面 (空なら平手)
    source_moves: list[str] = field(default_factory=list)


def parse_position_command(text: str) -> cshogi.Board:
    """`startpos moves ...` / `sfen <sfen> moves ...` を検証して盤面を返す。

    先頭の `position` は付いていても無くてもよい。非合法手は
    何手目のどの手かを添えて PositionError にする。
    """
    tokens = text.strip().split()
    if tokens and tokens[0] == "position":
        tokens = tokens[1:]
    if not tokens:
        raise PositionError("局面が空です")

    board = cshogi.Board()
    if tokens[0] == "startpos":
        moves_index = 1
    elif tokens[0] == "sfen":
        moves_index = tokens.index("moves") if "moves" in tokens else len(tokens)
        sfen = " ".join(tokens[1:moves_index])
        if not sfen:
            raise PositionError("sfen の中身がありません")
        # cshogi の set_sfen は不正な SFEN でプロセスごと落ちる。
        # 例外にならないので、渡す前に必ず検証する。
        error = validate_sfen(sfen)
        if error:
            raise PositionError(f"sfen が不正です: {error}")
        try:
            board.set_sfen(sfen)
        except Exception as e:
            raise PositionError(f"sfen を解釈できません: {sfen!r} ({e})")
    else:
        raise PositionError(
            f"startpos か sfen で始めてください: {tokens[0]!r}")

    if moves_index < len(tokens):
        if tokens[moves_index] != "moves":
            raise PositionError(f"moves が必要です: {tokens[moves_index]!r}")
        for offset, token in enumerate(tokens[moves_index + 1:], start=1):
            _push_checked(board, token, offset)
    return board


def _board_is_sane(board: cshogi.Board) -> bool:
    """set_sfen は緩いので、最低限「手番側に合法手がある」ことを見る。"""
    try:
        return board.is_ok() if hasattr(board, "is_ok") else True
    except Exception:
        return False


def _push_checked(board: cshogi.Board, usi: str, index: int) -> None:
    try:
        move = board.move_from_usi(usi)
    except Exception:
        raise PositionError(f"{index}手目 {usi!r} は USI として解釈できません")
    if move == 0 or not board.is_legal(move):
        raise PositionError(
            f"{index}手目 {usi!r} は非合法です "
            f"(局面: {board_sfen_main(board)})")
    board.push(move)


def replay_moves(board: cshogi.Board, moves: list[str]) -> list[str]:
    """合法な範囲だけ進め、実際に適用できた手を返す (棋譜取り込み用)。

    csa.db の棋譜は基本的に合法だが、壊れたデータで落ちないようにする。
    """
    applied: list[str] = []
    for usi in moves:
        try:
            move = board.move_from_usi(usi)
        except Exception:
            break
        if move == 0 or not board.is_legal(move):
            break
        board.push(move)
        applied.append(usi)
    return applied


def open_book_repo(book_dir: str, below_priority: int,
                   log=lambda msg: None) -> BookRepository:
    """優先度が below_priority **より高い** 定跡だけを見るリポジトリ。

    課題局面の判定には対象定跡 (100_x_...) 自身を含めない。含めると、
    サイクルが書き足した深い局面がフロンティアとして拾われ、課題局面が
    際限なく奥へ移動してしまうため (= 学習結果が判定条件に混ざる)。
    自己対局で「学習では触らない層」を作るときにも同じ集合を使う。
    """
    repo = BookRepository(log)
    repo.refresh(book_dir)
    repo.files = [f for f in repo.files if f.priority < below_priority]
    return repo


def derive_problem(board_start: cshogi.Board, moves: list[str],
                   repo: BookRepository, min_depth: int, tie_break: str,
                   origin: str, rng: random.Random | None = None,
                   ) -> Problem | None:
    """棋譜から課題局面を求める。定跡ヒットが1つも無ければ開始局面を返す。

    終局側から遡り、最初に定跡ヒットした局面で**実際に指された手を
    適用した後**が課題局面。定跡がまだ空なら棋譜の開始局面そのものを
    課題局面にする (0 から作る場合のブートストラップ)。
    """
    rng = rng or random.Random()
    tie_breaker = make_tie_breaker(tie_break)

    # 各手数の局面を作って保持する (最大でも数百手なのでコピーで十分)
    boards: list[cshogi.Board] = []
    applied: list[str] = []
    cursor = cshogi.Board()
    cursor.set_sfen(board_start.sfen())
    boards.append(_copy(cursor))
    for usi in moves:
        try:
            move = cursor.move_from_usi(usi)
            if move == 0 or not cursor.is_legal(move):
                break
            cursor.push(move)
        except Exception:
            break
        applied.append(usi)
        boards.append(_copy(cursor))

    for index in range(len(boards) - 1, -1, -1):
        board = boards[index]
        if board.is_game_over():
            continue
        hit, _misses = repo.probe(board, min_depth, tie_breaker, rng)
        if hit is None:
            continue
        # **実際に指された手**を適用する。定跡の採用手を使うと、自分が
        # 本線だと思っている変化しか掘れず、相手が外してきた変化を
        # 調べられない。棋譜がここで終わっているときだけ採用手を使う。
        played = applied[index] if index < len(applied) else ""
        chosen = played or hit.usi
        child = _copy(board)
        try:
            move = child.move_from_usi(chosen)
            if move == 0 or not child.is_legal(move):
                continue
            child.push(move)
        except Exception:
            continue
        if child.is_game_over():
            continue          # そこで終局する局面は課題にしない
        return Problem(child.sfen(), board_sfen_main(child), origin,
                       hit.file_name, chosen, hit.usi,
                       tuple(hit.candidates), boards[0].sfen(), list(applied))

    root = boards[0]
    if root.is_game_over():
        return None
    return Problem(root.sfen(), board_sfen_main(root),
                   f"{origin} (定跡ヒットなし: 開始局面から)",
                   source_sfen=root.sfen(), source_moves=list(applied))


def _copy(board: cshogi.Board) -> cshogi.Board:
    clone = cshogi.Board()
    clone.set_sfen(board.sfen())
    return clone
