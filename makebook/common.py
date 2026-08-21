"""定跡修正システムの共有基盤 (設定・定跡ストア・局面キー・計時)。

DESIGN.md §2。ここは「変えにくい部分」なので小さく保つ。
盤面は cshogi、定跡の読み書きは wrapper/yaneuraou_book_lib.py に一本化する。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass

WRAPPER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wrapper")
if WRAPPER_DIR not in sys.path:
    sys.path.insert(0, WRAPPER_DIR)

import cshogi  # type: ignore  # noqa: E402

import yaneuraou_book_lib as ybl  # noqa: E402
from book_repo import FILE_PATTERN, board_sfen_main  # noqa: E402
from file_swap import replace_file  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

#: 設定の既定値。値はすべて文字列で持ち、取り出すときに型付けする。
DEFAULTS: dict[str, str] = {
    # --- 外部リソース ---
    # KiriCompass の csa.db。将来 Cadence 内へ移す可能性があるため、
    # コードから直接パスを書かず必ずこのキーを経由する (DESIGN.md §2.1)。
    "CsaDbPath": r"C:\shogi\KiriCompass\data\csa.db",
    # 対局用とは別プロセス・別設定にするため makebook 配下のコピーを使う
    "EnginePath": r"engine\suisho11plus"
                  r"\YaneuraOu_SFNN_halfka2_1024_7_64_k3k3-V970DEV_AVX512.exe",
    # --- 出力先 ---
    "BookDir": r"..\book",
    "TargetBook": "100_x_book.db",
    "StateDbPath": "state.db",
    "LockPath": "condenser.lock",
    "TaskDir": "tasks",
    "LogDir": "logs",
    # --- 前例取り込み ---
    "PrecedentGames": "5",          # 1課題局面あたり取得する前例数
    "AnalysisNodes": "10000000",    # 前例検討 1手あたりのノード数
    "DecisiveEval": "1000",         # この評価値に達したら終局扱い
    # --- 自己対局 ---
    "SelfPlayNodes": "50000000",
    "ResignValue": "400",
    "WinStreak": "5",               # 何連勝でサイクル終了か
    "DrawWinner": "white",          # 千日手の勝者 (既定: 後手)
    "MaxPly": "320",                # これを超えたら引き分け扱い
    # --- エンジン ---
    "EngineThreads": "4",
    "EngineHash": "4096",
    "EngineEncoding": "utf-8",
    # --- csa.db からの自動補充 ---
    "NameFilters": "Condenser",     # 部分一致 (カンマ区切りで複数可)
    # --- 定跡の書き出し ---
    # zero  : value 0 固定 (既定)。採用回数は count 欄に入れ、ラッパーは
    #         value が同じとき count の多い手を採るので選択は成り立つ。
    # count : 採用回数を value 欄にも書く (非推奨)。value はエンジンの
    #         評価値の欄なので、それ以外の量を入れるべきではない
    #         (±32000 = VALUE_MATE と見分けが付かなくなる)。
    "ValueMode": "zero",
    "BookDepth": "32",              # 書き出す depth (ラッパーの depth ゲート用)
    # --- 高優先定跡の取り込み (閲覧用) ---
    # この優先度以下の定跡を、毎サイクル対象定跡へ複製する。対象定跡
    # だけを開けば全体が追えるようにするための**閲覧用の複製**で、
    # 採用は元のファイル (優先度が上) で行われるため実戦の手順は
    # 変わらない。複製が学習で削られても構わない — 次のサイクルで
    # 元のファイルから取り込み直される。-1 で無効。
    "MergeBookPriority": "0",
}

INT_KEYS = ("PrecedentGames", "AnalysisNodes",
            "DecisiveEval", "SelfPlayNodes", "ResignValue", "WinStreak",
            "MaxPly", "EngineThreads", "EngineHash", "BookDepth",
            "MergeBookPriority")

#: 実行中に GUI / コマンドから変更してよいキー (パス類は起動時固定)
TUNABLE_KEYS = ("PrecedentGames", "AnalysisNodes", "DecisiveEval",
                "SelfPlayNodes", "ResignValue", "WinStreak", "DrawWinner",
                "MaxPly", "NameFilters", "ValueMode")


class ConfigError(Exception):
    pass


def book_priority(filename: str) -> int | None:
    """定跡ファイル名から優先度 (先頭3桁) を取り出す。規約外なら None。"""
    match = FILE_PATTERN.match(os.path.basename(filename))
    return int(match.group(1)) if match else None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1].strip()
    return value


def parse_option_text(text: str, defaults: dict[str, str]
                      ) -> tuple[dict[str, str], list[str]]:
    """やねうら王 Option 系の `名前 値` 形式 (wrapper/config.py と同じ規則)。"""
    values: dict[str, str] = {}
    warnings: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.lstrip("\ufeff").strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        key = parts[0].strip()
        value = _unquote(parts[1].strip()) if len(parts) > 1 else ""
        if key not in defaults:
            warnings.append(f"未知の設定キーを無視: {key} (line {lineno})")
            continue
        values[key] = value
    return values, warnings


class Config:
    """`名前 値` 設定。GUI からの変更に備え、書き戻しもできる。"""

    def __init__(self, path: str, values: dict[str, str]):
        self.path = os.path.abspath(path)
        self.base_dir = os.path.dirname(self.path)
        self.values = values

    @classmethod
    def load(cls, path: str | None = None) -> tuple["Config", list[str]]:
        path = path or os.environ.get("CONDENSER_CONFIG") or \
            os.path.join(BASE_DIR, "config.txt")
        values = dict(DEFAULTS)
        warnings: list[str] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as f:
                raw, warnings = parse_option_text(f.read(), DEFAULTS)
            # 空値も尊重する (NameFilters を空にすると自動補充を止める)
            values.update(raw)
        config = cls(path, values)
        for key in INT_KEYS:                 # 起動時に型を検証する
            config.get_int(key)
        config.target_priority()             # TargetBook の命名規約も検証
        return config, warnings

    # -- 取り出し --------------------------------------------------------

    def get(self, key: str) -> str:
        return self.values.get(key, DEFAULTS.get(key, ""))

    def get_int(self, key: str) -> int:
        raw = self.get(key)
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{key} は整数が必要: {raw!r}")

    def path_of(self, key: str) -> str:
        """相対パスは設定ファイルの場所基準で解決する。"""
        value = self.get(key)
        if not value:
            return ""
        return (value if os.path.isabs(value)
                else os.path.normpath(os.path.join(self.base_dir, value)))

    def target_priority(self) -> int:
        """対象定跡の優先度。ファイル名 (先頭3桁) から取る。

        課題局面の判定には**これより高優先の定跡だけ**を使う。
        対象定跡自身を含めると、学習で書き足した深い局面がフロンティアと
        して拾われ、課題局面が際限なく奥へ移動してしまう。
        """
        name = self.get("TargetBook")
        priority = book_priority(name)
        if priority is None:
            raise ConfigError(
                f"TargetBook は <3桁優先度>_<b/w/x>_<名前>.<db/ybb> の"
                f"形式が必要です: {name!r}")
        return priority

    def draw_loser(self) -> str:
        """千日手で負け扱いになる側 ("b" / "w")。

        DrawWinner の裏返し。自己対局・指し継ぎ・タスク補充で
        同じ判断を使うため、ここに一本化する。
        """
        return "b" if self.get("DrawWinner").lower() == "white" else "w"

    def name_filters(self) -> list[str]:
        """カンマ・空白のどちらの区切りでも受け付ける。

        floodgate の対局者名に空白は入らないので、空白区切りを1つの
        フィルタとして扱う理由がない。カンマ限定にすると
        `NameFilters A B C` が「A B C」という無意味な1フィルタになり、
        自動補充が静かに空振りする (実際の config で起きかけた)。
        """
        return [token for token in re.split(r"[\s,]+",
                                            self.get("NameFilters"))
                if token]

    # -- 変更 (GUI / コマンド) -------------------------------------------

    def set(self, key: str, value: str) -> None:
        if key not in DEFAULTS:
            raise ConfigError(f"未知の設定キー: {key}")
        if key not in TUNABLE_KEYS:
            raise ConfigError(f"{key} は実行中に変更できません (再起動が必要)")
        old = self.values.get(key)
        self.values[key] = value
        if key in INT_KEYS:
            try:
                self.get_int(key)
            except ConfigError:
                self.values[key] = old if old is not None else DEFAULTS[key]
                raise

    def save(self) -> None:
        """設定を書き戻す (tmp → os.replace)。"""
        lines = ["# Condenser 設定 (`名前 値` 形式)。相対パスはこのファイル基準。"]
        for key in DEFAULTS:
            lines.append(f"{key:<20}{self.get(key)}")
        atomic_write_text(self.path, "\n".join(lines) + "\n")


# -- 二重起動ロック ------------------------------------------------------

class InstanceLock:
    """同じ設定での二重起動を防ぐ排他ロック。

    OS のバイト範囲ロックを使う。プロセスが終了 (クラッシュ・kill 含む)
    すれば OS が必ず解放するので、stale ロックの掃除が要らない。
    これがある限り「動いているのに気づかず状態を書き換える」事故を
    プロセス側から防げる (外部ツールも acquire を試せば動作中か分かる)。
    """

    def __init__(self, path: str):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        try:                     # 参考情報 (判定には使わない)
            handle.truncate(0)
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
        except OSError:
            pass
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


# -- ファイル出力 --------------------------------------------------------

def atomic_write_text(path: str, text: str) -> None:
    """同一ボリュームに tmp を書いてから置換する (DESIGN.md §3)。

    置換は file_swap.replace_file 経由。ラッパーが定跡を読んでいる最中でも
    差し替えられる経路を使い、それでも駄目なときはリトライする。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        replace_file(tmp, path)
    except OSError:
        try:
            os.remove(tmp)      # 中途半端な .tmp を残さない
        except OSError:
            pass
        raise


# -- 局面キー (csa.db と共有) -------------------------------------------

def position_key(sfen_main: str) -> int:
    """KiriCompass kifudb/board.py と同一の 64bit 局面キー (符号付き)。

    キー文字列は手数を除いた '<board> <turn> <hands>' で、cshogi の
    board_sfen_main() と同じ形。実 csa.db との一致はテストで固定する。
    """
    digest = hashlib.blake2b(sfen_main.encode(), digest_size=8).digest()
    value = int.from_bytes(digest, "little")
    return value - (1 << 64) if value >= (1 << 63) else value


def board_key(board) -> int:
    return position_key(board_sfen_main(board))


def move16_to_usi(code: int) -> str:
    """KiriCompass の move16 (csa.db の next_move / games.moves) を USI へ。"""
    to_sq = code & 0x7F
    frm = (code >> 7) & 0x7F
    to_str = f"{to_sq // 9 + 1}{chr(ord('a') + to_sq % 9)}"
    if frm >= 81:
        index = frm - 81
        if index >= 7:
            raise ValueError(f"不正な打ち駒コード: {code}")
        return f"{'PLNSGBR'[index]}*{to_str}"
    frm_str = f"{frm // 9 + 1}{chr(ord('a') + frm % 9)}"
    return f"{frm_str}{to_str}{'+' if code & (1 << 14) else ''}"


# -- 定跡ストア ----------------------------------------------------------

@dataclass
class BookEntry:
    """1局面ぶんの採用手。usi -> 採用回数。"""
    moves: dict[str, int]


class LearningBook:
    """学習定跡 (手数なし sfen -> {usi: count})。

    読み書きは yaneuraou_book_lib、書き出しは tmp → os.replace。
    ラッパーが読めるよう **SFEN順ソート + depth 列付き**で書く。
    """

    def __init__(self, value_mode: str = "count", depth: int = 32):
        self.entries: dict[str, dict[str, int]] = {}
        self.value_mode = value_mode
        self.depth = depth

    # -- 参照 --
    def __len__(self) -> int:
        return len(self.entries)

    def move_count(self) -> int:
        return sum(len(m) for m in self.entries.values())

    def get(self, sfen_main: str) -> dict[str, int]:
        return self.entries.get(sfen_main, {})

    # -- 更新 --
    def add(self, sfen_main: str, usi: str, weight: int = 1) -> None:
        moves = self.entries.setdefault(sfen_main, {})
        moves[usi] = moves.get(usi, 0) + weight

    def remove(self, sfen_main: str, usi: str) -> bool:
        """指し手を1つ消す。局面が空になったら局面ごと消す。"""
        moves = self.entries.get(sfen_main)
        if not moves or usi not in moves:
            return False
        moves.pop(usi)
        if not moves:
            self.entries.pop(sfen_main, None)
        return True

    # -- 入出力 --
    @classmethod
    def load(cls, path: str, value_mode: str = "count",
             depth: int = 32) -> "LearningBook":
        book = cls(value_mode, depth)
        if not path or not os.path.exists(path):
            return book
        for sfen, moves in ybl.read_yaneuraou_book_blocks(
                path, ignore_book_ply=True):
            key = " ".join(sfen.split()[:3])
            target = book.entries.setdefault(key, {})
            for move in moves:
                if move.move in ("none", "None", "null", "resign", "win"):
                    continue
                # 旧形式 (value 0) からの読み込みでは count を重みに使う
                weight = max(1, int(move.move_count or 1))
                target[move.move] = target.get(move.move, 0) + weight
        return book

    def to_text(self) -> str:
        lines = [ybl.YANEURAOU_BOOK_HEADER_V1]
        for sfen in sorted(self.entries):
            moves = sorted(self.entries[sfen].items(),
                           key=lambda kv: (-kv[1], kv[0]))
            if not moves:
                continue
            lines.append(f"sfen {sfen} 0")
            for usi, count in moves:
                # 採用回数は count 欄 (5列目) に書く。value はエンジンの
                # 評価値の欄なので、それ以外の量を入れるべきではない
                # (±32000 = VALUE_MATE と見分けが付かなくなる)。
                value = min(count, 9999) if self.value_mode == "count" else 0
                lines.append(f"{usi} none {value} {self.depth} {count}")
        return "\n".join(lines) + "\n"

    def save(self, path: str) -> None:
        atomic_write_text(path, self.to_text())


# -- 計時 ----------------------------------------------------------------

class Stopwatch:
    """`with Stopwatch(log, 'ラベル'):` で経過時間を表示する。"""

    def __init__(self, report, label: str):
        self.report = report
        self.label = label
        self.started = 0.0
        self.elapsed = 0.0

    def __enter__(self) -> "Stopwatch":
        self.started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.elapsed = time.monotonic() - self.started
        status = "" if exc_type is None else " (中断)"
        self.report(f"[時間] {self.label}: {format_duration(self.elapsed)}"
                    f"{status}")


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}分{sec:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes:02d}分{sec:02d}秒"
