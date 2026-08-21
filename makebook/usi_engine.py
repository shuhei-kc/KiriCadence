"""同期式の USI エンジンクライアント (検討・自己対局用)。

ラッパー (wrapper/engine_proxy.py) は GUI との中継のため非同期だが、
定跡生成は「1局面投げて答えを待つ」逐次処理なので同期の方が素直で堅い。
出力は呼び出しスレッドで読み切る。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass

#: 詰みを表すスコアの基準値 (mate N を ±(MATE_SCORE - |N|) に写す)
MATE_SCORE = 30000


@dataclass
class SearchResult:
    bestmove: str            # "resign" / "win" もあり得る
    ponder: str | None
    score: int               # 手番側から見た評価値 (cp)。詰みは ±MATE_SCORE 近傍
    is_mate: bool
    nodes: int
    elapsed: float


class EngineError(RuntimeError):
    pass


class UsiEngine:
    def __init__(self, path: str, encoding: str = "utf-8",
                 log=lambda msg: None):
        self.path = path
        self.encoding = encoding
        self.log = log
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.options: dict[str, str] = {}

    # -- 起動・終了 --------------------------------------------------------

    def start(self, options: dict[str, str] | None = None,
              timeout: float = 120.0) -> None:
        if not self.path or not os.path.exists(self.path):
            raise EngineError(f"エンジンが見つかりません: {self.path}")
        self.proc = subprocess.Popen(
            [self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(os.path.abspath(self.path)),
            bufsize=1, universal_newlines=True,
            encoding=self.encoding, errors="replace")
        self._send("usi")
        self._read_until(lambda line: line == "usiok", timeout)
        for name, value in (options or {}).items():
            self.set_option(name, value)
        self.isready(timeout)

    def set_option(self, name: str, value) -> None:
        self.options[name] = str(value)
        self._send(f"setoption name {name} value {value}")

    def isready(self, timeout: float = 300.0) -> None:
        self._send("isready")
        self._read_until(lambda line: line == "readyok", timeout)

    def newgame(self) -> None:
        self._send("usinewgame")

    def quit(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                self._write_raw(proc, "quit")
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def restart(self, timeout: float = 120.0) -> None:
        """落ちたエンジンを同じオプションで立て直す。"""
        options = dict(self.options)
        self.quit()
        self.options = {}
        self.start(options, timeout)

    # -- 探索 --------------------------------------------------------------

    def search(self, position_cmd: str, nodes: int,
               timeout: float = 3600.0) -> SearchResult:
        """`position ...` を送って `go nodes N`。bestmove まで読み切る。

        position_cmd は "startpos moves ..." / "sfen ... moves ..." の
        position 引数部 (先頭の 'position' は付けない)。
        """
        with self._lock:
            if not self.alive():
                raise EngineError("エンジンが停止しています")
            started = time.monotonic()
            self._send(f"position {position_cmd}")
            self._send(f"go nodes {nodes}")

            score = 0
            is_mate = False
            searched_nodes = 0
            deadline = started + timeout
            while True:
                line = self._readline(deadline)
                if line is None:
                    raise EngineError("bestmove を受け取る前にタイムアウト")
                if not line:
                    continue
                head = line.split(maxsplit=1)[0]
                if head == "info":
                    parsed = _parse_info(line)
                    if parsed is not None:
                        score, is_mate = parsed
                    nodes_seen = _parse_nodes(line)
                    if nodes_seen is not None:
                        searched_nodes = nodes_seen
                elif head == "bestmove":
                    parts = line.split()
                    best = parts[1] if len(parts) > 1 else "resign"
                    ponder = (parts[3] if len(parts) > 3
                              and parts[2] == "ponder" else None)
                    return SearchResult(best, ponder, score, is_mate,
                                        searched_nodes,
                                        time.monotonic() - started)

    # -- 低レベル ----------------------------------------------------------

    def _send(self, cmd: str) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise EngineError("エンジンが起動していません")
        self._write_raw(proc, cmd)

    @staticmethod
    def _write_raw(proc: subprocess.Popen, cmd: str) -> None:
        try:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
        except Exception as e:
            raise EngineError(f"エンジンへの送信に失敗: {e!r}")

    def _readline(self, deadline: float) -> str | None:
        """1行読む。deadline を過ぎたら None。

        readline() はブロックするので、**呼ぶ前に deadline を
        見るだけでは黙り込んだエンジンを検知できない**。
        無人で常駐するので、期限でプロセスを落として
        readline を EOF で戻す watchdog を張る (ラッパー側の
        _wait_token と同じ考え方)。
        """
        proc = self.proc
        if proc is None or proc.stdout is None:
            raise EngineError("エンジンが起動していません")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None

        timed_out = threading.Event()

        def _giveup() -> None:
            timed_out.set()
            _kill_tree(proc)         # readline を EOF で解放させる

        watchdog = threading.Timer(remaining, _giveup)
        watchdog.daemon = True
        watchdog.start()
        try:
            line = proc.stdout.readline()
        finally:
            watchdog.cancel()
        if timed_out.is_set():
            return None
        if line == "":
            raise EngineError("エンジンが異常終了しました (EOF)")
        return line.strip()

    def _read_until(self, predicate, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            line = self._readline(deadline)
            if line is None:
                raise EngineError("エンジンの応答待ちがタイムアウトしました")
            if predicate(line):
                return


def _kill_tree(proc: subprocess.Popen) -> None:
    """プロセスを**子孫ごと**落とす。

    .bat 経由で起動すると cmd.exe の下にエンジン本体がぶら下がるので、
    proc.kill() だけだと孫が生き残って stdout パイプを持ち続ける。
    すると readline() が解放されず、タイムアウトさせたはずが止まったままになる。
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _parse_nodes(line: str) -> int | None:
    parts = line.split()
    if "nodes" in parts:
        index = parts.index("nodes") + 1
        if index < len(parts) and parts[index].lstrip("-").isdigit():
            return int(parts[index])
    return None


def _parse_info(line: str) -> tuple[int, bool] | None:
    """info 行から (評価値, 詰みか) を取り出す。score が無ければ None。

    lowerbound / upperbound 付きの暫定値は採らない (確定値だけを使う)。
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
        if raw in ("+", "+0"):
            return MATE_SCORE, True
        if raw in ("-", "-0"):
            # int("-0") == 0 なので数値として見ると符号が消える。
            # mate -0 は「自分が詰んでいる」なので必ず負にする。
            return -MATE_SCORE, True
        try:
            plies = int(raw)
        except ValueError:
            return None
        magnitude = max(1, MATE_SCORE - abs(plies))
        return (magnitude if plies >= 0 else -magnitude), True
    return None
