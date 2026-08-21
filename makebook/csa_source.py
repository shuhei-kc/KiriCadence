"""KiriCompass csa.db の読み取りと「取得済み対局」の管理 (DESIGN.md §2.2)。

csa.db は KiriCompass が 30 分ごとに更新している。**mode=ro で読むだけ**で、
書き込み・スキーマ変更は一切しない。取得済みの管理は自前の state.db
(SQLite) に持ち、件数が増えても O(1) で判定できるようにする。
"""

from __future__ import annotations

import os
import sqlite3
import struct
import threading
from dataclasses import dataclass

from common import move16_to_usi

#: games.result
RESULT_DRAW, RESULT_BLACK, RESULT_WHITE = 0, 1, 2


@dataclass
class GameRef:
    game_id: int
    sort_key: int          # 対局開始時刻 (epoch分)。新しいほど大きい
    ply: int               # 課題局面が現れた手数 (name 検索では -1)


@dataclass
class GameRecord:
    game_id: int
    event: str
    started_at: str
    black_name: str
    white_name: str
    result: int | None     # 0=引分 1=先手勝 2=後手勝 None=中断等
    initial_sfen: str | None
    moves: list[str]       # USI


class SeenStore:
    """一度サイクルに使った対局を記録する (次回以降スキップして古い方へ進む)。

    game_id を主キーにした 1 テーブルだけ。位置由来・名前由来を区別せず
    共通で管理する (どちらも「もう見た棋譜」であることに変わりはない)。
    """

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".",
                    exist_ok=True)
        # 作成はメインスレッド、利用はワーカースレッドなので
        # check_same_thread=False + 明示ロックで直列化する。
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_games ("
            " game_id INTEGER PRIMARY KEY,"
            " seen_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " reason  TEXT)")
        self.conn.commit()

    def is_seen(self, game_id: int) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM seen_games WHERE game_id=?",
                (game_id,)).fetchone()
        return row is not None

    def mark(self, game_ids, reason: str = "") -> int:
        rows = [(int(g), reason) for g in game_ids]
        if not rows:
            return 0
        with self.lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO seen_games(game_id, reason) "
                "VALUES (?,?)", rows)
            self.conn.commit()
        return len(rows)

    def count(self) -> int:
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM seen_games").fetchone()[0]

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass


class CsaSource:
    """csa.db 読み取り (read-only)。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"csa.db が見つかりません: {db_path}")
        uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=10000")

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # -- 局面からの前例検索 ------------------------------------------------

    def games_at_position(self, position_key: int, limit: int,
                          seen: SeenStore | None = None,
                          scan_limit: int = 200000) -> list[GameRef]:
        """その局面を含む対局を**新しい順**に最大 limit 件返す。

        seen に載っている対局は飛ばすので、呼ぶたび自然に古い方へ進む。
        主キー (position_key, sort_key, game_id, ply) の逆順スキャンなので
        件数が増えても先頭から必要数だけ読めば済む。
        """
        found: list[GameRef] = []
        picked: set[int] = set()
        cursor = self.conn.execute(
            "SELECT game_id, sort_key, ply FROM position_games "
            "WHERE position_key=? ORDER BY sort_key DESC, game_id DESC",
            (position_key,))
        scanned = 0
        for game_id, sort_key, ply in cursor:
            scanned += 1
            if scanned > scan_limit:
                break
            if game_id in picked:
                continue
            picked.add(game_id)
            if seen is not None and seen.is_seen(game_id):
                continue
            found.append(GameRef(game_id, sort_key, ply))
            if len(found) >= limit:
                break
        cursor.close()
        return found

    # -- 名前からの対局検索 ------------------------------------------------

    def games_by_name(self, name_filters: list[str], limit: int,
                      seen: SeenStore | None = None,
                      losses_only: bool = True, draw_loser: str = "b",
                      scan_limit: int = 200000) -> list[GameRef]:
        """対局者名に指定文字列を含む対局を**新しい順**に最大 limit 件。

        タスクが尽きたときの自動補充用 (既定では自分の bot 名)。

        losses_only=True (既定) では、**その名前が負けた対局だけ**を返す。
        直したいのは負けた定跡であり、勝ち局を積んでも意味が薄いため。

        draw_loser は千日手 (result=0) を負け扱いにする側 ("b"/"w"/"")。
        既定の "b" は DrawWinner=white と対応する — 先手は千日手を避けたい
        ので、先手番での千日手は負けとして拾う。結果なし (NULL) は
        勝敗が付いていないので含めない。
        """
        if not name_filters:
            return []
        black_losses = [RESULT_WHITE]
        white_losses = [RESULT_BLACK]
        if draw_loser == "b":
            black_losses.append(RESULT_DRAW)
        elif draw_loser == "w":
            white_losses.append(RESULT_DRAW)

        clauses: list[str] = []
        params: list[str] = []
        for name in name_filters:
            pattern = f"%{name}%"
            if losses_only:
                # 先手側で名前が一致して負け / 後手側で一致して負け
                for column, results in (("black_name", black_losses),
                                        ("white_name", white_losses)):
                    marks = ",".join("?" * len(results))
                    clauses.append(f"({column} LIKE ? AND result IN ({marks}))")
                    params.append(pattern)
                    params.extend(results)
            else:
                clauses.append("(black_name LIKE ? OR white_name LIKE ?)")
                params.extend([pattern, pattern])
        cursor = self.conn.execute(
            f"SELECT game_id, started_at FROM games "
            f"WHERE ({' OR '.join(clauses)}) "
            "ORDER BY started_at DESC, game_id DESC", params)
        found: list[GameRef] = []
        scanned = 0
        for game_id, _started in cursor:
            scanned += 1
            if scanned > scan_limit:
                break
            if seen is not None and seen.is_seen(game_id):
                continue
            found.append(GameRef(game_id, 0, -1))
            if len(found) >= limit:
                break
        cursor.close()
        return found

    def loser_side(self, game: GameRecord, name_filters: list[str],
                   draw_loser: str = "b") -> str:
        """その対局で、指定名のどちら側が負けたか ("先手"/"後手"/"")。

        千日手は draw_loser 側の負けとして扱う (既定は先手)。
        """
        black_losses = {RESULT_WHITE}
        white_losses = {RESULT_BLACK}
        if draw_loser == "b":
            black_losses.add(RESULT_DRAW)
        elif draw_loser == "w":
            white_losses.add(RESULT_DRAW)
        for name in name_filters:
            if name in game.black_name and game.result in black_losses:
                return "先手"
            if name in game.white_name and game.result in white_losses:
                return "後手"
        return ""

    # -- 1局の取り出し ------------------------------------------------------

    def game(self, game_id: int) -> GameRecord | None:
        row = self.conn.execute(
            "SELECT game_id, event, started_at, black_name, white_name,"
            " result, initial_sfen, moves FROM games WHERE game_id=?",
            (game_id,)).fetchone()
        if row is None:
            return None
        blob = row[7] or b""
        codes = struct.unpack(f"<{len(blob) // 2}H", blob[:len(blob) // 2 * 2])
        moves: list[str] = []
        for code in codes:
            try:
                moves.append(move16_to_usi(code))
            except ValueError:
                break        # 壊れた手以降は捨てる (前例として使えるところまで)
        return GameRecord(row[0], row[1], row[2] or "", row[3], row[4],
                          row[5], row[6], moves)
