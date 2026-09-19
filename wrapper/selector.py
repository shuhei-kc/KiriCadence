"""採用手の選択・タイブレーク (DESIGN.md §6)。差し替え可能な独立層。

book_repo が §5.3 のフィルタを通した「value 最大かつ depth 充足」の候補集合
から1手選ぶ。選択ロジックは模索中なので、この層だけで完結して差し替え
られるよう、TieBreaker プロトコル + 登録制にしている。

新しいロジックを足すには:
  1. choose(candidates, ctx) -> (Candidate, decided_at, reason) を持つクラスを書く
  2. TIE_BREAKERS に登録し、config.py の TIE_BREAK_CHOICES に名前を足す
deep{n} (n>=2) は登録せず、名前から make_tie_breaker が組み立てる。
"""

from __future__ import annotations

import random
import re
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
               ctx: ProbeContext) -> tuple[Candidate, int, str]:
        """candidates (2手以上) から1手選ぶ。

        返り値は (選んだ候補, decided_at, reason)。decided_at は deep 系が
        「何番目の応手で差がついたか」を報告するための値で、
        他の方式は 0 を返す。reason は決め方 (決定ログ用。§9.2)。"""
        ...


class FirstTieBreaker:
    """定跡の記載順で先頭 (決定的)。"""

    name = "first"

    def choose(self, candidates, ctx):
        return candidates[0], 0, "first"


class RandomTieBreaker:
    """同値の全手から等確率で抽選。"""

    name = "random"

    def choose(self, candidates, ctx):
        return ctx.rng.choice(candidates), 0, "random"


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


def _compare_replies_run(lhs: list[int], rhs: list[int],
                         n: int) -> tuple[int, bool]:
    """deep{n} の2候補比較。(値, n 連続で決まったか)。

    値の向き・意味は _compare_replies と同じ (正: rhs の方が得、絶対値は
    決まった応手の順位)。先頭から突き合わせ、**同じ向きの差が n 順位
    連続した最初の地点**で決める。同値の順位で連続は切れ、逆向きの差なら
    その向きの 1 から数え直す。比べられるのは両方にある順位だけ。
    最後まで n 連続がつかなければ、この2候補を従来の deep
    (_compare_replies: 最初の差 → 本数の少ない側) で比べ直す。
    n=1 は従来の deep と同じ結果になる。
    """
    previous = run = 0
    for index, (a, b) in enumerate(zip(lhs, rhs), start=1):
        sign = (a > b) - (a < b)        # 正: rhs の方が小さい = rhs が得
        if sign == 0:
            previous = run = 0
            continue
        run = run + 1 if sign == previous else 1
        previous = sign
        if run >= n:
            return sign * index, True
    return _compare_replies(lhs, rhs), False


def _reply_profiles(candidates, ctx
                    ) -> list[tuple[Candidate, list[int] | None]]:
    """各候補の (候補, 相手の応手 value 列 (良い順) または None=定跡外)。"""
    profiles: list[tuple[Candidate, list[int] | None]] = []
    for cand in candidates:
        key = ctx.child_sfen_main(cand.usi)
        replies = ctx.probe_replies(key) if key is not None else []
        values = [r.value for r in replies] if replies else None
        profiles.append((cand, values))
    return profiles


def _deep_pick(known) -> tuple[Candidate, int]:
    """従来 deep の選び方 (記載順の勝ち残り)。(選んだ候補, decided_at)。

    _compare_replies は全順序なので、応手列が最良の候補 (同じ列が複数なら
    記載順の先頭) が残る。"""
    best = known[0]
    decided_at = 0
    for other in known[1:]:
        rank = _compare_replies(best[1], other[1])
        if rank > 0:        # other の方が相手の選択肢を絞れている
            best = other
            decided_at = max(decided_at, rank)
        elif rank < 0:
            decided_at = max(decided_at, -rank)
    return best[0], decided_at


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
        profiles = _reply_profiles(candidates, ctx)
        known = [p for p in profiles if p[1] is not None]
        if not known:
            # deep が効かない局面 (片手番だけの定跡など)。記載順で固定せず
            # 抽選する — 登録した手が全部使われるように。
            return ctx.rng.choice(candidates), 0, "lottery"

        chosen, decided_at = _deep_pick(known)
        if len(known) < len(profiles) and decided_at == 0:
            decided_at = 1      # 定跡外候補を落とした、という意味で 1
        return chosen, decided_at, "deep"


class DeepNTieBreaker:
    """deep{n}: 応手列の差が n 順位連続で同じ向きについた所で決める
    (DESIGN.md §6.2)。

    従来の deep は最初に差が出た1順位だけで決めるので、小さな差1か所で
    その後の順位の傾向と逆の手を選ぶことがある。deep{n} は同じ向きの差が
    n 順位続くまで判断を待つ。

    1. 候補の全ペアを _compare_replies_run で比べる (n 連続がつかなければ
       そのペアだけ従来 deep で比べ直す)。
    2. 他の既知候補すべてに勝つ手があれば、それを選ぶ。
    3. 無ければ (勝ち負けが循環する等)、既知候補全体を従来 deep で選ぶ。
    応手が引けない候補・全候補で応手が引けないときの扱いは deep と同じ
    (定跡外の候補は最下位、全滅なら抽選)。
    """

    def __init__(self, n: int):
        if n < 2:
            raise ValueError(f"deep{{n}} の n は2以上: {n}")
        self.n = n
        self.name = f"deep{n}"

    def choose(self, candidates, ctx):
        profiles = _reply_profiles(candidates, ctx)
        known = [p for p in profiles if p[1] is not None]
        if not known:
            return ctx.rng.choice(candidates), 0, "lottery"
        dropped = len(known) < len(profiles)
        if len(known) == 1:
            return known[0][0], 1 if dropped else 0, "only_known"

        results = {}
        for i in range(len(known)):
            for j in range(i + 1, len(known)):
                results[i, j] = _compare_replies_run(known[i][1], known[j][1],
                                                     self.n)
        for i in range(len(known)):
            ranks, by_run = [], True
            for j in range(len(known)):
                if i == j:
                    continue
                value, run = results[min(i, j), max(i, j)]
                mine = value if i < j else -value     # 負: i の方が得
                if mine >= 0:
                    break
                ranks.append(-mine)
                by_run = by_run and run
            else:
                return (known[i][0], max(ranks),
                        "run" if by_run else "pair_fallback")

        chosen, decided_at = _deep_pick(known)
        if dropped and decided_at == 0:
            decided_at = 1
        return chosen, decided_at, "global_fallback"


TIE_BREAKERS: dict[str, Callable[[], object]] = {
    "first": FirstTieBreaker,
    "random": RandomTieBreaker,
    "deep": DeepTieBreaker,
    "deep1": DeepTieBreaker,
}

_DEEP_N = re.compile(r"deep([1-9][0-9]*)")


def make_tie_breaker(name: str):
    """deep{n} (n>=2) は DeepNTieBreaker、deep / deep1 は従来 deep。
    未知の名前は deep にフォールバック (呼び出し側で検証済みの前提)。"""
    match = _DEEP_N.fullmatch(name)
    if match and int(match.group(1)) >= 2:
        return DeepNTieBreaker(int(match.group(1)))
    return TIE_BREAKERS.get(name, DeepTieBreaker)()
