"""採用手の選択・タイブレーク (DESIGN.md §6)。差し替え可能な独立層。

book_repo が §5.3 のフィルタを通した「value 最大かつ depth 充足」の候補集合
から1手選ぶ。選択ロジックは模索中なので、この層だけで完結して差し替え
られるよう、TieBreaker プロトコル + 登録制にしている。

新しいロジックを足すには:
  1. choose(candidates, ctx) -> (Candidate, decided_at) を持つクラスを書く
  2. TIE_BREAKERS に登録し、config.py の TIE_BREAK_CHOICES に名前を足す
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Protocol

import cshogi  # type: ignore


@dataclass(frozen=True)
class Candidate:
    usi: str      # 実局面の向きの指し手 (flip 済み)
    value: int
    depth: int
    count: int = 1   # 採用回数 (定跡の5列目)。makebook 系の補強回数


class ProbeContext:
    """タイブレーカーへ渡す照会文脈。

    probe_replies(sfen_main) は「同じ定跡ファイル」への照会 (flip 込み、
    終端手除外、value 降順)。go 1回の間キャッシュされる。
    """

    def __init__(self, full_sfen: str,
                 probe_replies: Callable[[str], list[Candidate]],
                 rng: random.Random):
        self._full_sfen = full_sfen
        self.probe_replies = probe_replies
        self.rng = rng

    def child_sfen_main(self, usi: str) -> str | None:
        """usi を指した先の手数なしSFEN。進められなければ None。"""
        try:
            board = cshogi.Board()
            board.set_sfen(self._full_sfen)
            board.push_usi(usi)
            return " ".join(board.sfen().split()[:3])
        except Exception:
            return None


class TieBreaker(Protocol):
    name: str

    def choose(self, candidates: list[Candidate],
               ctx: ProbeContext) -> tuple[Candidate, int]:
        """candidates (2手以上) から1手選ぶ。

        返り値は (選んだ候補, decided_at)。decided_at は deep が
        「何番目の応手で差がついたか」を報告するための値で、
        他の方式は 0 を返す。"""
        ...


class FirstTieBreaker:
    """定跡の記載順で先頭 (決定的)。"""

    name = "first"

    def choose(self, candidates, ctx):
        return candidates[0], 0


class RandomTieBreaker:
    """同値の全手から等確率で抽選。"""

    name = "random"

    def choose(self, candidates, ctx):
        return ctx.rng.choice(candidates), 0


def _compare_replies(lhs: list[int], rhs: list[int]) -> int:
    """相手の応手 value 列 (良い順) を突き合わせ、どちらが自分に得かを返す。

    値は相手視点なので**小さいほど自分に得**。先頭 (相手の最善応手) から
    順に比べ、最初に差が出た順位で決める。返り値:

    - 正: rhs の方が得。絶対値は差がついた応手の順位 (1始まり)
    - 負: lhs の方が得。同上
    - 0 : 最後まで同じで、本数も同じ

    比較範囲がすべて同じなら、本数の少ない側 (相手の選択肢が少ない側) が得。
    """
    for index, (a, b) in enumerate(zip(lhs, rhs), start=1):
        if a < b:
            return -index
        if a > b:
            return index
    if len(lhs) == len(rhs):
        return 0
    rank = min(len(lhs), len(rhs)) + 1
    return -rank if len(lhs) < len(rhs) else rank


class DeepTieBreaker:
    """同値手を「相手の応手列」で絞る (DESIGN.md §6.1)。

    各候補を指した先の相手の応手を value 降順に並べ、先頭から突き合わせて
    最初に差が出た順位で value の小さい側 — 相手の2番手以降が悪い側 —
    を選ぶ。比較範囲が同じなら応手の本数が少ない側。それも同じなら
    記載順の先頭 (決定的)。指した先が定跡外の候補は最下位。

    **どの候補からも応手が引けないときは抽選する**。応手は同じ定跡
    ファイルからしか引かないので、片手番だけの定跡 (b または w のみ) では
    「指した先は相手番 = そのファイルに存在しない」となり、deep が原理的に
    働かない。ここを記載順で固定すると2番手以降の手が一生指されないため、
    意図して複数手を登録した局面がランダムにならなかった。
    """

    name = "deep"

    def choose(self, candidates, ctx):
        profiles: list[tuple[Candidate, list[int] | None]] = []
        for cand in candidates:
            key = ctx.child_sfen_main(cand.usi)
            replies = ctx.probe_replies(key) if key is not None else []
            values = [r.value for r in replies] if replies else None
            profiles.append((cand, values))

        known = [p for p in profiles if p[1] is not None]
        if not known:
            # deep が効かない局面 (片手番だけの定跡など)。記載順で固定せず
            # 抽選する — 登録した手が全部使われるように。
            return ctx.rng.choice(candidates), 0

        best = known[0]
        decided_at = 0
        for other in known[1:]:
            rank = _compare_replies(best[1], other[1])
            if rank > 0:        # other の方が相手の選択肢を絞れている
                best = other
                decided_at = max(decided_at, rank)
            elif rank < 0:
                decided_at = max(decided_at, -rank)
        if len(known) < len(profiles) and decided_at == 0:
            decided_at = 1      # 定跡外候補を落とした、という意味で 1
        return best[0], decided_at


TIE_BREAKERS: dict[str, Callable[[], object]] = {
    "first": FirstTieBreaker,
    "random": RandomTieBreaker,
    "deep": DeepTieBreaker,
}


def make_tie_breaker(name: str):
    """未知の名前は deep にフォールバック (呼び出し側で検証済みの前提)。"""
    return TIE_BREAKERS.get(name, DeepTieBreaker)()
