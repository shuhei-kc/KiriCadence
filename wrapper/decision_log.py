"""定跡判定の決定ログ decisions.jsonl (DESIGN.md §9.2)。

1判定 = 1行の JSON。将来の定跡修正システムが「定跡がどこで切れたか・
どのファイルの何を指したか」を CSA 棋譜と付き合わせずに読むための出力。

全行に game_id (起動時UUID + 対局連番) を付ける。複数インスタンス並走や
クラッシュ再起動があっても行を対局に紐付けられるようにするため。
usinewgame を送らないクライアントでも全対局が -0000 に潰れないよう、
newgame なしで判定が始まったら暗黙に採番する (implicit newgame 行を書く)。
既存フィールドは変えない (スキーマ拡張は追記のみ)。
"""

from __future__ import annotations

import json
import time
import uuid


def _now() -> str:
    """ローカル時刻 + UTCオフセット (例 2026-08-19T12:34:56.789+0900)。"""
    ms = int(time.time_ns() // 1_000_000) % 1000
    return time.strftime(f"%Y-%m-%dT%H:%M:%S.{ms:03d}%z")


class DecisionLogger:
    def __init__(self, path: str | None, log=lambda msg: None):
        self.path = path
        self.log = log
        self.instance_id = uuid.uuid4().hex
        self.game_seq = 0            # usinewgame (明示/暗黙) ごとに +1
        self._game_open = False      # gameover で閉じる
        self._write_failed = False   # 書けない環境で警告を繰り返さない

    @property
    def game_id(self) -> str:
        return f"{self.instance_id}-{self.game_seq:04d}"

    def newgame(self) -> None:
        self.game_seq += 1
        self._game_open = True
        self._write({"event": "newgame"})

    def _ensure_game(self) -> None:
        """usinewgame を経ずに判定/終局が来た場合の暗黙採番。"""
        if not self._game_open:
            self.game_seq += 1
            self._game_open = True
            self._write({"event": "newgame", "implicit": True})

    def gameover(self, result: str) -> None:
        self._ensure_game()
        self._write({"event": "gameover", "result": result})
        self._game_open = False

    def hit(self, sfen: str, ply: int, side: str, hit, tie_break: str,
            prior_misses: dict[str, str] | None = None) -> None:
        """hit: book_repo.BookHit。prior_misses = 採用ファイルより高優先で
        ミスしたファイルの内訳 (修正システムが優先定跡の穴を探すための情報)。"""
        self._ensure_game()
        record = {"event": "hit", "sfen": sfen, "ply": ply, "side": side,
                  "file": hit.file_name, "move": hit.usi, "value": hit.value,
                  "depth": hit.depth, "flipped": hit.flipped}
        if hit.tie_candidates > 1:
            record["tie"] = tie_break
            record["tie_candidates"] = hit.tie_candidates
            record["decided_at"] = hit.decided_at
        if prior_misses:
            record["prior_misses"] = prior_misses
        self._write(record)

    def miss(self, sfen: str, ply: int, side: str,
             files: dict[str, str] | None = None,
             reason: str | None = None) -> None:
        """全ファイルミス (files=ファイル別内訳) または
        reason='repetition_guard' 等の一括理由。"""
        self._ensure_game()
        record = {"event": "miss", "sfen": sfen, "ply": ply, "side": side}
        if reason is not None:
            record["reason"] = reason
        else:
            record["files"] = files or {}
        self._write(record)

    def _write(self, record: dict) -> None:
        if not self.path:
            return
        line = {"ts": _now(), "game_id": self.game_id}
        line.update(record)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            self._write_failed = False
        except OSError as e:
            if not self._write_failed:
                self._write_failed = True
                self.log(f"[Decisions] 書き込み失敗 (対局は続行): {e}")
