"""対局中に「勝敗が見えた」時点で定跡修正タスクを積む (連携骨格)。

floodgate 対局は終局まで待つと反映が遅れるため、自分の評価値が
決着水準 (既定 ±1000) を超えた瞬間にタスクを書き出す。実際の終局を
待たないので、そのまま指し継いでいる間に Condenser 側が動き出せる。

既定は **負けたときだけ** 投入する (`TaskEmitOn loss`)。直したいのは
負けた定跡であり、勝った対局まで積むとキューが積み上がるため。
勝ちも拾いたくなったら `both` / `win` に変えられる。

出力先は makebook/tasks/ で、形式は makebook/tasks.py と同じ JSON。
書き込みは tmp → os.replace なので、同時に読まれても壊れない。
1対局につき1タスクだけ出す (usinewgame でリセット)。
"""

from __future__ import annotations

import json
import os
import time
import uuid


def parse_score(line: str) -> tuple[int, bool] | None:
    """USI の info 行から (評価値, 詰みか) を取り出す。無ければ None。

    lowerbound / upperbound の暫定値は採らない。
    """
    parts = line.split()
    if "score" not in parts:
        return None
    if "lowerbound" in parts or "upperbound" in parts:
        return None
    index = parts.index("score")
    kind = parts[index + 1] if index + 1 < len(parts) else ""
    raw = parts[index + 2] if index + 2 < len(parts) else ""
    if kind == "cp":
        try:
            return int(raw), False
        except ValueError:
            return None
    if kind == "mate":
        if raw.startswith("+"):
            return 30000, True
        if raw.startswith("-"):
            return -30000, True
        try:
            plies = int(raw)
        except ValueError:
            return None
        return (30000 if plies >= 0 else -30000), True
    return None


#: 投入条件
EMIT_LOSS = "loss"    # 自分が負け側になったときだけ (既定)
EMIT_WIN = "win"      # 勝ち側のときだけ
EMIT_BOTH = "both"    # どちらでも
EMIT_MODES = (EMIT_LOSS, EMIT_WIN, EMIT_BOTH)


class TaskEmitter:
    """評価値が決着水準に達したら1対局に1回だけタスクを書き出す。

    評価値は「探索の手番側から見た値」で、ラッパーが探索させるのは
    自分の手番だけなので、**負の値は自分が悪い**ことを意味する。
    ponder 中も根の手番は自分なので符号の意味は変わらない。
    """

    def __init__(self, task_dir: str, threshold: int,
                 mode: str = EMIT_LOSS, log=lambda msg: None):
        self.task_dir = task_dir
        self.threshold = threshold
        self.mode = mode if mode in EMIT_MODES else EMIT_LOSS
        self.log = log
        self.emitted_this_game = False
        #: 検討モード (go infinite / go mate) の評価値は対局と無関係なので見ない
        self.in_game_search = True
        self.enabled = bool(task_dir) and threshold > 0

    def newgame(self) -> None:
        self.emitted_this_game = False

    def set_game_search(self, active: bool) -> None:
        self.in_game_search = active

    def _should_emit(self, score: int) -> bool:
        if self.mode == EMIT_LOSS:
            return score <= -self.threshold
        if self.mode == EMIT_WIN:
            return score >= self.threshold
        return abs(score) >= self.threshold

    def observe(self, info_line: str, position_cmd: str | None,
                game_id: str = "") -> bool:
        """info 行を1つ見る。タスクを出したら True。

        position_cmd は GUI から受けた最後の `position ...` 行。
        """
        if (not self.enabled or self.emitted_this_game
                or not position_cmd or not self.in_game_search):
            return False
        parsed = parse_score(info_line)
        if parsed is None:
            return False
        score, _is_mate = parsed
        if not self._should_emit(score):
            return False
        self.emitted_this_game = True     # 失敗しても再試行しない (対局優先)
        return self._write(position_cmd, score, game_id)

    def _write(self, position_cmd: str, score: int, game_id: str) -> bool:
        text = position_cmd.strip()
        if text.startswith("position "):
            text = text[len("position "):].strip()
        outcome = "loss" if score < 0 else "win"
        record = {
            "kind": "kifu",
            "payload": {"position": text, "score": score,
                        "outcome": outcome, "wrapper_game_id": game_id},
            "origin": f"floodgate 対局中 ({'劣勢' if score < 0 else '優勢'} "
                      f"{score:+d})",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        # makebook/tasks.py と同じ命名規則 (並べ替えれば投入順になる)。
        # ラッパーは1対局に1件しか出さないので連番は 000000 固定でよい。
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        micro = int(round((now - int(now)) * 1_000_000)) % 1_000_000
        name = (f"{stamp}-{micro:06d}-000000-"
                f"{uuid.uuid4().hex[:8]}-kifu.json")
        path = os.path.join(self.task_dir, name)
        try:
            os.makedirs(self.task_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            self.log(f"[Task] 書き出しに失敗 (対局は続行): {e}")
            return False
        self.log(f"[Task] 決着水準 ({score:+d} = {outcome}) に達したため"
                 f"タスクを投入: {name}")
        return True
