"""タスク投入 CLI (ShogiHome の bat・手作業から呼ぶ想定)。

  py -3.12 push_task.py --kifu "startpos moves 7g7f 3c3d ..."
  py -3.12 push_task.py --problem "sfen <sfen>"
  py -3.12 push_task.py --kifu-file kifu.txt      (1行1局)

局面は cshogi で合法性を検証してから積む (打ち間違いで定跡を壊さない)。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import Config, ConfigError  # noqa: E402
from problem import PositionError, parse_position_command  # noqa: E402
from tasks import KIND_KIFU, KIND_PROBLEM, TaskQueue  # noqa: E402


def push(queue: TaskQueue, kind: str, text: str, origin: str) -> bool:
    try:
        parse_position_command(text)
    except PositionError as e:
        print(f"NG: {e}", flush=True)
        return False
    path = queue.push(kind, {"position": text}, origin=origin)
    print(f"OK: {os.path.basename(path)}", flush=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kifu", help="棋譜 (課題局面を導出する)")
    parser.add_argument("--problem", help="課題局面を直接指定")
    parser.add_argument("--kifu-file", help="1行1局のファイル")
    parser.add_argument("--origin", default="外部", help="由来の表示名")
    parser.add_argument("--config", help="config.txt のパス")
    args = parser.parse_args()

    try:
        config, _ = Config.load(args.config)
    except ConfigError as e:
        print(f"設定エラー: {e}", flush=True)
        return 1
    queue = TaskQueue(config.path_of("TaskDir"))

    ok = True
    if args.problem:
        ok &= push(queue, KIND_PROBLEM, args.problem, args.origin)
    if args.kifu:
        ok &= push(queue, KIND_KIFU, args.kifu, args.origin)
    if args.kifu_file:
        with open(args.kifu_file, encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if line and not line.startswith("#"):
                    ok &= push(queue, KIND_KIFU, line,
                               f"{args.origin}:{lineno}")
    if not (args.problem or args.kifu or args.kifu_file):
        parser.print_help()
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
