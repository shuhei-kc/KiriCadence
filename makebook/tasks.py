"""ファイルベースのタスクキュー (DESIGN.md §4.4 の inbox を踏襲)。

1タスク = 1 JSON ファイル。書き込みは tmp → os.replace なので、
ラッパー・ShogiHome の bat・手作業のどれから積んでも壊れない。
処理はファイル名 (時刻+連番) の昇順 = 積まれた順。

**サイクル中は取り出さない**。1つの課題局面のループが終わってから
次を取り出すので、実行中の割り込みは起きない。
"""

from __future__ import annotations

import itertools
import json
import os
import time
import uuid
from dataclasses import dataclass, field

#: プロセス内の投入連番。Windows の time() は分解能が粗く (実測で
#: 2000回中2値しか出ない) 時刻だけでは同着が避けられないため、
#: 連番を挟んで確実に投入順で並ぶようにする。
_SEQUENCE = itertools.count()

PENDING = ""
DONE = "done"
ERROR = "error"

#: タスク種別
KIND_KIFU = "kifu"        # 棋譜から課題局面を導出する
KIND_PROBLEM = "problem"  # 課題局面を直接指定する


def task_filename(kind: str) -> str:
    """並べ替えるだけで投入順になるファイル名。

    `<日時>-<マイクロ秒>-<プロセス内連番>-<乱数>-<種別>.json`。
    Windows の time() は分解能が粗いので、時刻だけでは同時に積んだ
    タスクの順序が崩れる。プロセス内連番で確実に順序を付け、
    別プロセスとの同着だけ乱数で分ける (同時刻なので順序は任意でよい)。
    """
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    micro = int(round((now - int(now)) * 1_000_000)) % 1_000_000
    seq = next(_SEQUENCE) % 1_000_000
    return (f"{stamp}-{micro:06d}-{seq:06d}-"
            f"{uuid.uuid4().hex[:8]}-{kind}.json")


@dataclass
class Task:
    kind: str
    payload: dict
    path: str = ""
    created_at: str = ""
    origin: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


class TaskQueue:
    def __init__(self, directory: str):
        self.dir = os.path.abspath(directory)
        for sub in ("", DONE, ERROR):
            os.makedirs(os.path.join(self.dir, sub), exist_ok=True)

    # -- 追加 --------------------------------------------------------------

    def push(self, kind: str, payload: dict, origin: str = "") -> str:
        """タスクを積む。戻り値はファイルパス。"""
        record = {
            "kind": kind,
            "payload": payload,
            "origin": origin,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        name = task_filename(kind)
        path = os.path.join(self.dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path

    # -- 取り出し ----------------------------------------------------------

    def pending_files(self) -> list[str]:
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        return sorted(
            os.path.join(self.dir, n) for n in names
            if n.endswith(".json") and os.path.isfile(os.path.join(self.dir, n)))

    def count(self) -> int:
        return len(self.pending_files())

    def take(self) -> Task | None:
        """最も古い未処理タスクを1つ読む (ファイルはまだ動かさない)。

        処理が終わったら complete() / fail() を呼ぶ。途中で落ちた場合は
        未処理のまま残るので次回リトライされる。
        """
        for path in self.pending_files():
            try:
                with open(path, encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, ValueError):
                self._move(path, ERROR)
                continue
            kind = record.get("kind", "")
            if kind not in (KIND_KIFU, KIND_PROBLEM):
                self._move(path, ERROR)
                continue
            return Task(kind, record.get("payload") or {}, path,
                        record.get("created_at", ""),
                        record.get("origin", ""))
        return None

    def complete(self, task: Task) -> None:
        self._move(task.path, DONE)

    def fail(self, task: Task, reason: str = "") -> None:
        self._move(task.path, ERROR, reason)

    def _move(self, path: str, sub: str, reason: str = "") -> None:
        if not path or not os.path.exists(path):
            return
        target = os.path.join(self.dir, sub, os.path.basename(path))
        try:
            if os.path.exists(target):
                target = f"{target}.{uuid.uuid4().hex[:6]}"
            os.replace(path, target)
            if reason:
                with open(target + ".reason.txt", "w", encoding="utf-8") as f:
                    f.write(reason + "\n")
        except OSError:
            pass
