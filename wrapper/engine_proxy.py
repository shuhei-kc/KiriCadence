"""本体USIエンジンのプロセス管理 (起動・送受信・stderr収集・再起動)。

DESIGN.md §8。文字コードは config の EngineEncoding (既定 utf-8、
errors='replace' で落ちない)。cwd はエンジンのあるディレクトリ
(評価関数ファイル等の相対パス解決のため)。

**スレッド安全性** (v1.3): プロセスハンドルは3つのスレッドから触られる —
メインスレッド (send)、reader スレッド (read_line / restart)、
監視タイマー (terminate)。そのため:

- 差し替え (start / terminate) は _proc_lock で直列化する。
- 参照側 (send / read_line / alive) は self.process を**一度だけ
  スナップショット**してから使う。「None チェックの後にもう一度
  self.process を読む」書き方は、その間に別スレッドが None にすると
  AttributeError になり、呼び出し元 (reader スレッドやメインループ) を
  巻き込んで落とす。
- 例外は種類を限定せず捕捉する。ここで漏れた例外は中継の停止 =
  GUIから見て無音のハングに直結する。
"""

from __future__ import annotations

import os
import subprocess
import threading


class EngineProxy:
    def __init__(self, engine_path: str, encoding: str = "utf-8",
                 log=lambda msg: None):
        self.engine_path = engine_path
        self.encoding = encoding
        self.log = log
        self.process: subprocess.Popen | None = None
        self._send_lock = threading.Lock()
        #: プロセスの差し替え (start/terminate) を直列化する。
        #: restart が terminate→start と入れ子で呼ぶため再入可能ロック。
        self._proc_lock = threading.RLock()

    def start(self) -> bool:
        if not self.engine_path or not str(self.engine_path).strip():
            self.log("[Engine] EnginePath が設定されていません "
                     "(定跡専用モードで動作)")
            return False
        engine_dir = os.path.dirname(os.path.abspath(self.engine_path))
        try:
            proc = subprocess.Popen(
                [self.engine_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=engine_dir,
                bufsize=1,
                universal_newlines=True,
                encoding=self.encoding,
                errors="replace",
            )
        except Exception as e:
            self.log(f"[Engine] 起動失敗: {e!r}")
            with self._proc_lock:
                self.process = None
            return False
        with self._proc_lock:
            self.process = proc
        threading.Thread(target=self._stderr_reader, args=(proc,),
                         daemon=True).start()
        self.log(f"[Engine] started pid={proc.pid} "
                 f"(cwd={engine_dir}, encoding={self.encoding})")
        return True

    def restart(self) -> bool:
        """RECOVERY用。旧プロセスを始末して再起動する。"""
        with self._proc_lock:
            self.terminate()
            return self.start()

    def _stderr_reader(self, proc: subprocess.Popen) -> None:
        """起動時のプロセスを引数で受け取る (self.process の差し替えと無関係に
        自分の担当プロセスだけを読む)。"""
        if proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    self.log(f"[Engine stderr] {line}")
        except Exception:
            pass

    def alive(self) -> bool:
        proc = self.process          # スナップショット
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:
            return False

    def send(self, cmd: str) -> None:
        with self._send_lock:
            proc = self.process      # スナップショット
            if proc is None:
                return
            try:
                if proc.poll() is not None or proc.stdin is None:
                    return
                proc.stdin.write(cmd + "\n")
                proc.stdin.flush()
            except Exception as e:
                self.log(f"[Engine] send failed ({cmd!r}): {e!r}")

    def read_line(self) -> str:
        """1行読む。EOF (プロセス終了)・エラーなら空文字列。"""
        proc = self.process          # スナップショット
        if proc is None:
            return ""
        try:
            stdout = proc.stdout
            if stdout is None:
                return ""
            return stdout.readline()
        except Exception as e:
            self.log(f"[Engine] read failed: {e!r}")
            return ""

    def terminate(self) -> None:
        with self._proc_lock:
            proc = self.process
            self.process = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception as e:
            self.log(f"[Engine] terminate failed: {e!r}")
        self.log("[Engine] terminated")
