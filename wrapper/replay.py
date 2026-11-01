"""オフライン検証CLI (DESIGN.md §10)。エンジン不要で定跡照会だけを検証する。

使い方:
  py -3.12 wrapper\\replay.py "startpos moves 7g7f 3c3d"   # 判定過程を表示
  py -3.12 wrapper\\replay.py --sfen "lnsg... b - 1"
  py -3.12 wrapper\\replay.py --check-book                 # 全定跡のソート検証

位置指定は USI position コマンドの引数部と同じ形式
("startpos [moves ...]" / "sfen <sfen> [moves ...]")。
moves 付きで指定すると §5.1 の千日手ガード判定も再現される。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cshogi  # type: ignore

import yaneuraou_book_lib as ybl
from book_repo import BookRepository, board_sfen_main
from config import ConfigError, load_config
from selector import make_tie_breaker
from sfen_guard import validate_sfen

import random


def _set_sfen_checked(board, sfen: str) -> None:
    """cshogi の set_sfen は不正な SFEN で**プロセスごと落ちる**。

    try/except では受けられないので、渡す前に必ず検証する。
    この CLI は手打ちの SFEN を受けるので一番踏みやすい。
    """
    error = validate_sfen(sfen)
    if error:
        raise ValueError(f"sfen が不正です: {error}")
    board.set_sfen(sfen)


def build_board(position_arg: str | None, sfen_arg: str | None):
    """(board, 局面出現Counter) を作る。"""
    board = cshogi.Board()
    counts: Counter = Counter()
    if sfen_arg:
        _set_sfen_checked(board, sfen_arg.strip())
        counts[board_sfen_main(board)] += 1
        return board, counts

    parts = (position_arg or "startpos").split()
    if parts[0] == "startpos":
        moves_idx = 1
    elif parts[0] == "sfen":
        moves_idx = parts.index("moves") if "moves" in parts else len(parts)
        _set_sfen_checked(board, " ".join(parts[1:moves_idx]))
    else:
        raise ValueError(f"unknown position kind: {parts[0]}")
    counts[board_sfen_main(board)] += 1
    if len(parts) > moves_idx and parts[moves_idx] == "moves":
        for index, token in enumerate(parts[moves_idx + 1:], start=1):
            # push_usi は指せない手を黙って無視するので自分で検証する
            move = board.move_from_usi(token)
            if move == 0 or not board.is_legal(move):
                raise ValueError(f"{index}手目 {token!r} は非合法です")
            board.push(move)
            counts[board_sfen_main(board)] += 1
    return board, counts


def run_probe(config, position_arg, sfen_arg) -> int:
    board, counts = build_board(position_arg, sfen_arg)
    sfen_main = board_sfen_main(board)
    is_black = (board.turn == cshogi.BLACK)
    print(f"局面: {sfen_main}")
    print(f"手番: {'先手' if is_black else '後手'} / "
          f"BookDir: {config.book_dir} / "
          f"BookMinDepth: {config.book_min_depth} / "
          f"TieBreak: {config.tie_break}")

    repo = BookRepository(log=lambda m: print(f"  {m}"))
    repo.refresh(config.book_dir)

    if is_black and counts.get(sfen_main, 0) >= 2:
        print(f"→ 既出局面 (x{counts[sfen_main]}) のため先手は定跡フル無視 "
              f"(千日手回避 §5.1) → エンジンへ")
        return 0

    hit, misses = repo.probe(board, config.book_min_depth,
                             make_tie_breaker(config.tie_break),
                             random.Random(0),
                             trace=lambda m: print(f"  {m}"),
                             depth_bonus=config.book_depth_bonus)
    for name, reason in misses.items():
        print(f"  {name}: ミス ({reason})")
    if hit is None:
        print("→ 全ファイルミス → エンジンへ")
    else:
        print(f"→ 採用: {hit.usi} (value={hit.value} depth={hit.depth}"
              f"{' flip' if hit.flipped else ''} book={hit.file_name})")
    return 0


def check_db_sorted(path: str) -> tuple[bool, str]:
    """.db の全走査ソート検証 (SFEN主要部の昇順)。"""
    prev_key = None
    entries = 0
    with open(path, "rb") as f:
        for lineno, raw in enumerate(iter(f.readline, b""), start=1):
            key = ybl.sfen_key_from_db_line(raw)
            if key is None:
                continue
            entries += 1
            if prev_key is not None and key < prev_key:
                return False, (f"NG: line {lineno} で順序違反 "
                               f"({entries}エントリ目)。sfen順ソートが必要")
            prev_key = key
    return True, f"OK: {entries}エントリ、ソート済み"


def check_ybb_sorted(path: str) -> tuple[bool, str]:
    """.ybb の全走査検証 (read_ybb_book_blocks が非ソートで例外を出す)。"""
    entries = 0
    try:
        for _sfen, _moves in ybl.read_ybb_book_blocks(Path(path)):
            entries += 1
    except ValueError as e:
        return False, f"NG: {e}"
    return True, f"OK: {entries}エントリ、ソート済み"


def run_check_book(config) -> int:
    repo = BookRepository(log=lambda m: print(f"  {m}"))
    repo.refresh(config.book_dir)
    if not repo.files:
        print(f"定跡ファイルがありません: {config.book_dir}")
        return 1
    failed = 0
    for info in repo.files:
        size_mb = os.path.getsize(info.path) / (1024 * 1024)
        print(f"{info.name} ({size_mb:,.1f} MB) を検証中...", flush=True)
        checker = (check_ybb_sorted if info.name.endswith(".ybb")
                   else check_db_sorted)
        try:
            ok, message = checker(info.path)
        except OSError as e:
            ok, message = False, f"NG: 読取エラー {e}"
        print(f"  {message}")
        if not ok:
            failed += 1
    if failed:
        print(f"\n{failed}ファイルで問題を検出")
        return 1
    print("\n全ファイル OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("position", nargs="?",
                        help='"startpos moves ..." / "sfen ... moves ..."')
    parser.add_argument("--sfen", help="単一SFENで指定 (moves なし)")
    parser.add_argument("--check-book", action="store_true",
                        help="book内の全ファイルを全走査でソート検証")
    parser.add_argument("--config", help="config.txt のパス "
                        "(既定: KiriCadence/config.txt または "
                        "環境変数 KIRICADENCE_CONFIG)")
    args = parser.parse_args()

    try:
        config, warnings = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}")
        return 1
    for message in warnings:
        print(f"config warning: {message}")

    if args.check_book:
        return run_check_book(config)
    if not args.position and not args.sfen:
        parser.print_help()
        return 1
    return run_probe(config, args.position, args.sfen)


if __name__ == "__main__":
    sys.exit(main())
