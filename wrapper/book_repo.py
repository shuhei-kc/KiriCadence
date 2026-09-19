"""book フォルダの走査と優先順に沿った定跡照会 (DESIGN.md §4-§5)。

ファイル名規約: <3桁優先度>_<採用手番(b/w/x)>_<名前>.<db|ybb>
数値が小さいほど高優先。同じ優先度では b/w (手番一致) を x より先に見る。

probe はルックアップの都度 open → close し、ファイルハンドルを保持しない
(Windows で外部プロセスが対局中でも定跡を差し替えられるようにするため)。
"""

from __future__ import annotations

import os
import re
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cshogi  # type: ignore

import yaneuraou_book_lib as ybl
from file_swap import open_shared
from flip import flipped_move, flipped_sfen_main
from selector import Candidate, ProbeContext

#: 定跡としては指せない終端手 (parse_book_move が resign→none に正規化する
#: ため none/null/win で足りるが、生の値も念のため含める)
TERMINAL_MOVES = frozenset(("none", "None", "null", "resign", "win"))

FILE_PATTERN = re.compile(r"^(\d{3})_([bwx])_(.+)\.(db|ybb)$")

#: miss 内訳 (decisions.jsonl に載る値。DESIGN.md §9.2)
MISS_NO_ENTRY = "no_entry"
MISS_NO_MOVES = "no_moves"          # エントリはあるが指し手行が0本
MISS_TERMINAL_ONLY = "terminal_only"
MISS_ILLEGAL_ONLY = "illegal_only"
MISS_DEPTH_GATE = "depth_gate"
MISS_IO_ERROR = "io_error"
MISS_UNSORTED = "unsorted"          # 実行時ソート検証で順序違反を検出


class UnsortedBookError(Exception):
    """二分探索がサンプルしたキーの順序違反 (= 非ソートの .db)。"""


class CheckedDbProbe(ybl.DbBookProbe):
    """DbBookProbe + 実行時ソート検証。

    二分探索が読んだ (オフセット, キー) を記録し、probe 1回ごとに
    オフセット順の単調性を確認する (追加I/Oゼロ)。非ソートの .db は
    二分探索が静かに no_entry を返し続けるため、実行時に検出して
    ファイルを止める最後の網 (custom_engine3 _check_samples の移植)。
    完全な検証ではない (探索経路外の違反は見えない) ので、
    オフラインの replay.py --check-book と併用する。
    """

    def __init__(self, path):
        super().__init__(path)
        # 定跡は生成側が随時差し替える。既定の open では読み取り中の
        # ファイルを置き換えられない (Windows) ので共有削除つきで開き直す。
        self.file.close()
        self.file = open_shared(path)
        self._samples: list[tuple[int, str]] = []

    def _sfen_line_at_or_after(self, offset):
        entry = super()._sfen_line_at_or_after(offset)
        if entry is not None:
            self._samples.append((entry[0], entry[2]))
        return entry

    def take_order_violation(self):
        """直前の probe で観測した順序違反を返し、サンプルを捨てる。"""
        samples = sorted(self._samples)
        self._samples = []
        for (p1, k1), (p2, k2) in zip(samples, samples[1:]):
            if p1 != p2 and k1 > k2:
                return (p1, k1, p2, k2)
        return None


class SharedYbbProbe(ybl.YbbBookProbe):
    """.ybb も共有削除つきで開く (差し替え中でも読める)。"""

    def __init__(self, path):
        super().__init__(path)
        self.file.close()
        self.file = open_shared(path)


def open_checked_probe(path: str):
    """.db は実行時ソート検証付きで開く (.ybb は機械生成なので検証なし)。

    どちらも共有削除つきで開くので、読んでいる最中に生成側が
    差し替えても双方が失敗しない。
    """
    if ybl.is_ybb_path(path):
        return SharedYbbProbe(path)
    return CheckedDbProbe(path)


@dataclass(frozen=True)
class BookFileInfo:
    path: str
    name: str
    priority: int
    side: str            # 'b' / 'w' / 'x'

    @property
    def side_rank(self) -> int:
        return 0 if self.side in ("b", "w") else 1


@dataclass(frozen=True)
class BookHit:
    usi: str
    value: int
    depth: int
    flipped: bool        # 反転局面から引いたか
    file_name: str       # 採用元ファイル名
    tie_candidates: int  # 同値で並んだ候補数 (1なら一意)
    decided_at: int      # deep が差を付けた応手の順位 (0=タイブレーク不要/不成立)
    #: フィルタを通った候補手すべて (usi は実局面の向き)。usi はそのうち
    #: タイブレークで選ばれた1手にすぎないので、「定跡どおりか」を見る
    #: 用途ではこちらと突き合わせる。
    candidates: tuple[str, ...] = ()
    #: タイブレークの決め方 (selector の reason。1手なら空)。§9.2
    tie_reason: str = ""


def order_key(info: BookFileInfo) -> tuple:
    return (info.priority, info.side_rank, info.name)


def board_sfen_main(board) -> str:
    """手数を除いた '<board> <turn> <hands>'。"""
    return " ".join(board.sfen().split()[:3])


def scan_book_dir(book_dir: str, log, warned: set[str]) -> list[BookFileInfo]:
    """book_dir 直下を走査し、規約に合うファイルを照会順で返す。

    規約外のファイル・depth情報の無い .ybb は1回だけ警告する
    (warned に記録して繰り返さない)。
    """
    try:
        names = os.listdir(book_dir)
    except OSError as e:
        if book_dir not in warned:
            warned.add(book_dir)
            log(f"[Book] bookフォルダを読めません: {book_dir}: {e}")
        return []

    infos: list[BookFileInfo] = []
    for name in sorted(names):
        path = os.path.join(book_dir, name)
        if not os.path.isfile(path):
            continue
        m = FILE_PATTERN.match(name)
        if not m:
            if name not in warned:
                warned.add(name)
                log(f"[Book] 命名規約外のファイルを無視: {name} "
                    f"(<3桁>_<b/w/x>_<名前>.<db/ybb> が必要)")
            continue
        if name.endswith(".ybb") and name not in warned:
            try:
                _count, flags = ybl.read_ybb_header(Path(path))
                if not (flags & ybl.YBB_FLAG_MOVE_DEPTH):
                    warned.add(name)
                    log(f"[Book] {name}: depth情報の無い.ybbです。"
                        f"BookMinDepth>=1 では一切採用されません")
            except (OSError, ValueError) as e:
                warned.add(name)
                log(f"[Book] {name}: ybbヘッダを読めません "
                    f"(照会時にio_error扱い): {e}")
        infos.append(BookFileInfo(path, name, int(m.group(1)), m.group(2)))
    return sorted(infos, key=order_key)


class BookRepository:
    """優先定跡の照会窓口。ファイル一覧は refresh() (usinewgame) で更新。"""

    def __init__(self, log=lambda msg: None):
        self.log = log
        self.files: list[BookFileInfo] = []
        self._warned: set[str] = set()
        self._unsorted: set[str] = set()   # 順序違反を検出したパス
        self.book_dir: str | None = None

    def refresh(self, book_dir: str) -> None:
        self.book_dir = book_dir
        self._unsorted.clear()   # 対局間に修正・差し替えされ得るので再検出
        old_names = [f.name for f in self.files]
        self.files = scan_book_dir(book_dir, self.log, self._warned)
        names = [f.name for f in self.files]
        if names != old_names:
            if names:
                self.log(f"[Book] 照会順: {', '.join(names)}")
            else:
                self.log(f"[Book] 定跡ファイルがありません: {book_dir}")

    # -- 照会 (go 1回あたり) ------------------------------------------------

    def probe(self, board, min_depth: int, tie_breaker,
              rng: random.Random | None = None,
              trace: Callable[[str], None] | None = None,
              ) -> tuple[BookHit | None, dict[str, str]]:
        """現局面の採用手を探す。(BookHit または None, ファイル別miss内訳)。

        board は呼び出し側の追跡局面 (cshogi.Board)。この関数は変更しない。
        採用手番 (b/w) の判定は常にこの board の手番で行う (§4.2)。
        """
        side = "b" if board.turn == cshogi.BLACK else "w"
        target = board_sfen_main(board)
        full_sfen = board.sfen()
        legal = {cshogi.move_to_usi(m) for m in board.legal_moves}
        rng = rng or random.Random()
        misses: dict[str, str] = {}

        for info in self.files:
            if info.side not in (side, "x"):
                if trace:
                    trace(f"{info.name}: 手番対象外 (skip)")
                continue
            if info.path in self._unsorted:
                misses[info.name] = MISS_UNSORTED
                if trace:
                    trace(f"{info.name}: 順序違反検出済み (skip)")
                continue
            try:
                probe = open_checked_probe(info.path)
            except Exception as e:
                misses[info.name] = MISS_IO_ERROR
                self.log(f"[Book] {info.name}: open失敗 (次のファイルへ): "
                         f"{e!r}")
                continue
            try:
                result = self._probe_one(probe, info, target, full_sfen,
                                         legal, min_depth, tie_breaker, rng,
                                         trace)
            except UnsortedBookError as e:
                self._unsorted.add(info.path)
                misses[info.name] = MISS_UNSORTED
                self.log(f"[Book] {info.name}: 順序違反を検出。次の "
                         f"usinewgame まで無効化します。sfen順への修正が"
                         f"必要 (replay.py --check-book で検証): {e}")
                continue
            except Exception as e:
                misses[info.name] = MISS_IO_ERROR
                self.log(f"[Book] {info.name}: 照会エラー (次のファイルへ): "
                         f"{e!r}")
                continue
            finally:
                try:
                    probe.close()
                except Exception:
                    pass
            if isinstance(result, BookHit):
                return result, misses
            misses[info.name] = result
        return None, misses

    # -- ファイル単位 --------------------------------------------------------

    def _probe_one(self, probe, info: BookFileInfo, target: str,
                   full_sfen: str, legal: set[str], min_depth: int,
                   tie_breaker, rng: random.Random,
                   trace: Callable[[str], None] | None):
        """1ファイルの照会。BookHit か miss理由 (str) を返す (§5.2-5.3)。"""
        raw_direct = self._probe_checked(probe, target)
        survivors, reason_direct = (
            self._survivors(raw_direct, False, legal, min_depth, info.name)
            if raw_direct is not None else ([], None))
        flipped = False
        if trace and raw_direct is not None:
            trace(f"{info.name}: 直接ヒット {len(raw_direct)}手 → "
                  f"採用可能 {len(survivors)}手"
                  + (f" ({reason_direct})" if reason_direct else ""))

        raw_flip = None
        if not survivors:
            flip_target = flipped_sfen_main(target)
            if flip_target is not None:
                # flip照会の失敗でファイル全体を io_error にしない
                # (直接照会は正常に動いている可能性がある)
                try:
                    raw_flip = self._probe_checked(probe, flip_target)
                except UnsortedBookError:
                    raise
                except Exception as e:
                    self.log(f"[Book] {info.name}: flip照会エラー "
                             f"(flipなし扱い): {e!r}")
                    raw_flip = None
            if raw_flip is not None:
                survivors, reason_flip = self._survivors(
                    raw_flip, True, legal, min_depth, info.name)
                flipped = bool(survivors)
                if trace:
                    trace(f"{info.name}: flipヒット {len(raw_flip)}手 → "
                          f"採用可能 {len(survivors)}手"
                          + (f" ({reason_flip})" if survivors == [] else ""))
            else:
                reason_flip = None

        if not survivors:
            if raw_direct is None and raw_flip is None:
                return MISS_NO_ENTRY
            # 行が存在した側の理由を優先 (直接 > flip)
            return reason_direct if raw_direct is not None else reason_flip

        if len(survivors) == 1:
            chosen, decided_at, reason = survivors[0], 0, ""
        else:
            ctx = ProbeContext(full_sfen,
                               self._reply_prober(probe), rng)
            chosen, decided_at, reason = tie_breaker.choose(survivors, ctx)
        if trace:
            tie = (f" (同値{len(survivors)}手, tie={tie_breaker.name}"
                   + (f", 第{decided_at}応手で決定" if decided_at else "")
                   + f", {reason})") if len(survivors) > 1 else ""
            trace(f"{info.name}: 採用 {chosen.usi} value={chosen.value} "
                  f"depth={chosen.depth}{' flip' if flipped else ''}{tie}")
        return BookHit(chosen.usi, chosen.value, chosen.depth, flipped,
                       info.name, len(survivors), decided_at,
                       tuple(c.usi for c in survivors), reason)

    def _survivors(self, raw_moves, flip: bool, legal: set[str],
                   min_depth: int, name: str
                   ) -> tuple[list[Candidate], str | None]:
        """§5.3 のフィルタ。(生き残り, 空のときの理由)。

        1. 終端手を除く (flip時は反転できない手も除く)
        2. 非合法手を除く (破損定跡による反則負けを防ぐ最後の砦)
        3. value 最大の集合を取り、depth < min_depth を除く。
           「depthが足りないのでvalue 2番手を指す」ことはしない (§5.3)。
        """
        if not raw_moves:
            return [], MISS_NO_MOVES
        candidates: list[Candidate] = []
        for m in raw_moves:
            usi = m.move
            if usi in TERMINAL_MOVES:
                continue
            if flip:
                back = flipped_move(usi)
                if back is None:
                    self.log(f"[Book] {name}: 反転できない指し手を無視: {usi}")
                    continue
                usi = back
            candidates.append(Candidate(usi, m.value, m.depth,
                                        max(1, int(m.move_count or 1))))
        if not candidates:
            return [], MISS_TERMINAL_ONLY

        legal_cands = [c for c in candidates if c.usi in legal]
        if len(legal_cands) < len(candidates):
            dropped = [c.usi for c in candidates if c.usi not in legal]
            self.log(f"[Book] {name}: 非合法手を除外"
                     f"{' (flip)' if flip else ''}: {' '.join(dropped)}")
        if not legal_cands:
            return [], MISS_ILLEGAL_ONLY

        best_value = max(c.value for c in legal_cands)
        best = [c for c in legal_cands if c.value == best_value]
        deep_enough = [c for c in best if c.depth >= min_depth]
        if not deep_enough:
            return [], MISS_DEPTH_GATE
        # value が同じなら**採用回数の多い手**を採る (makebook 系の補強回数)。
        # value は評価値として正しく保ちたいので、補強の強さは count で見る。
        # 4列の定跡 (peta 等) は count=1 に揃うので、この段は素通りする。
        best_count = max(c.count for c in deep_enough)
        return [c for c in deep_enough if c.count == best_count], None

    @staticmethod
    def _probe_checked(probe, key: str):
        """probe.probe() + 実行時ソート検証 (CheckedDbProbe のみ)。"""
        result = probe.probe(key)
        take = getattr(probe, "take_order_violation", None)
        if take is not None:
            violation = take()
            if violation is not None:
                p1, k1, p2, k2 = violation
                raise UnsortedBookError(
                    f"offset {p1}: {k1!r} > offset {p2}: {k2!r}")
        return result

    def _reply_prober(self, probe) -> Callable[[str], list[Candidate]]:
        """deep タイブレーク用: 同一ファイルへの応手照会 (flip込み・キャッシュ付き)。

        指し手の向きは使わず value しか見ないので、反転して引いた場合も
        反転せずそのまま使える。キャッシュはこの go の該当ファイル照会の
        間だけ生きる (probe と同寿命)。
        """
        cache: dict[str, list[Candidate]] = {}

        def probe_replies(sfen_main: str) -> list[Candidate]:
            cached = cache.get(sfen_main)
            if cached is not None:
                return cached
            try:
                raw = self._probe_checked(probe, sfen_main)
                if not raw:
                    flip_key = flipped_sfen_main(sfen_main)
                    raw = (self._probe_checked(probe, flip_key)
                           if flip_key is not None else None)
            except UnsortedBookError:
                raise
            except Exception as e:
                self.log(f"[Book] 応手照会エラー (定跡外扱い): {e!r}")
                raw = None
            replies = [Candidate(m.move, m.value, m.depth,
                                 max(1, int(m.move_count or 1)))
                       for m in (raw or []) if m.move not in TERMINAL_MOVES]
            replies.sort(key=lambda c: -c.value)
            cache[sfen_main] = replies
            return replies

        return probe_replies
