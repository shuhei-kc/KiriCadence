"""プロトコル統合テスト用のfake USIエンジン (custom_engine3 から移植)。

環境変数:
  FAKE_LOG        受信コマンドを追記するファイル (検証用)
  FAKE_CRASH_ARM  このファイルが存在する状態で通常goを受けると、
                  ファイルを消してから即異常終了する (1回だけ死ぬ)
  FAKE_SLOW_ARM   このファイルが存在する状態で通常goを受けると、
                  ファイルを消して1秒後にbestmoveを返す (遅延注入)
  FAKE_NOISE      非空ならisready時に「空白のみの行」と「不正UTF-8の行」を
                  readyokの前に出す (reader堅牢性の回帰テスト用)
"""

import os
import sys
import threading
import time

LOG = os.environ.get("FAKE_LOG")
ARM = os.environ.get("FAKE_CRASH_ARM")
SLOW = os.environ.get("FAKE_SLOW_ARM")
NOISE = os.environ.get("FAKE_NOISE")

_stop_event = threading.Event()
_infinite = False
_pondering = False


def log(line):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def emit(*lines):
    for line in lines:
        print(line)
    sys.stdout.flush()


def emit_noise():
    """空白のみの行 + 不正UTF-8の行 (wrapper側 errors='replace' で
    U+FFFD になる)。wrapperのreaderが死なず readyok を通すことの検証用。"""
    sys.stdout.flush()
    sys.stdout.buffer.write(b"   \n")
    sys.stdout.buffer.write(b"info string \x88\xea broken \xfe bytes\n")
    sys.stdout.buffer.flush()


def main():
    global _infinite, _pondering
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        log(line)
        cmd = line.split()[0]
        if cmd == "usi":
            emit("id name FakeEngine",
                 "id author test",
                 "option name Hash type spin default 16 min 1 max 1024",
                 "option name USI_Ponder type check default true",
                 "option name ClearHash type button",
                 "usiok")
        elif cmd == "isready":
            if NOISE:
                emit_noise()
            emit("readyok")
        elif cmd == "go":
            if "ponder" in line:
                _pondering = True
                emit("info depth 1 score cp 20 pv 5g5f")
            elif "infinite" in line:
                _infinite = True
                emit("info depth 1 score cp 30 pv 8c8d")
            else:
                if ARM and os.path.exists(ARM):
                    os.remove(ARM)
                    log("(crashing)")
                    os._exit(1)
                if SLOW and os.path.exists(SLOW):
                    os.remove(SLOW)
                    log("(slow)")
                    time.sleep(1.0)
                emit("info depth 1 score cp 10 pv 3c3d",
                     "bestmove 3c3d ponder 2f2e")
        elif cmd == "ponderhit":
            if _pondering:
                _pondering = False
                emit("bestmove 5g5f ponder 8c8d")
        elif cmd == "stop":
            if _infinite:
                _infinite = False
                emit("bestmove 8c8d")
            elif _pondering:
                # 放棄された探索の残骸: info→(間)→bestmove の順に出す
                # (wrapperがこのinfoを漏らさないことの回帰テスト用)
                _pondering = False
                emit("info depth 9 score cp 999 pv 5g5f")
                time.sleep(0.3)
                emit("bestmove 5g5f")
        elif cmd == "quit":
            break
    time.sleep(0.05)


if __name__ == "__main__":
    main()
