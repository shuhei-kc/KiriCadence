"""config.txt (やねうら王 Option 系の `名前 値` 形式) の読み込み (DESIGN.md §7)。

- 空行と `#` 始まりの行は無視。最初の空白までがキー、残り全体が値。
  キー・値とも前後の空白はトリム。BOM 耐性あり。
- 相対パスは config ファイルのあるディレクトリ基準で解決する。
- 起動時の不正値は ConfigError (起動失敗)。`usinewgame` 再読込では
  reload_book_settings() が前の値を維持して警告のみ返す (無人運用で落ちない)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

CONFIG_ENV = "KIRICADENCE_CONFIG"

TIE_BREAK_CHOICES = ("deep", "random", "first")

#: 既定値 (EnginePath のみ必須)
DEFAULTS = {
    "EnginePath": "",
    "EngineEncoding": "utf-8",
    "LogDir": "logs",
    "BookDir": "book",
    "BookMinDepth": "1",
    "TieBreak": "deep",
    # 定跡修正システムへの連携 (空なら無効)。対局中に自分の評価値が
    # TaskEvalThreshold を超えたら、その時点の棋譜をタスクとして書き出す。
    "TaskDir": "",
    "TaskEvalThreshold": "1000",
    # loss = 自分が負け側のときだけ投入 (既定)。win / both も指定可。
    "TaskEmitOn": "loss",
}

#: usinewgame ごとに再読込するキー (それ以外は起動時のみ)
RELOADABLE_KEYS = ("BookDir", "BookMinDepth", "TieBreak")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    config_path: str
    base_dir: str          # 相対パス解決の基準 (config ファイルの場所)
    engine_path: str       # 空なら定跡専用モード
    engine_encoding: str
    log_dir: str
    book_dir: str
    book_min_depth: int
    tie_break: str
    task_dir: str
    task_eval_threshold: int
    task_emit_on: str


def parse_option_text(text: str) -> tuple[dict[str, str], list[str]]:
    """`名前 値` 形式のテキストを {キー: 値} にする。(値, 警告リスト)。"""
    values: dict[str, str] = {}
    warnings: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.lstrip("﻿").strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        key = parts[0].strip()
        value = _unquote(parts[1].strip()) if len(parts) > 1 else ""
        if key not in DEFAULTS:
            warnings.append(f"未知の設定キーを無視: {key} (line {lineno})")
            continue
        if key in values:
            warnings.append(f"設定キーが重複 (後の値を採用): {key} "
                            f"(line {lineno})")
        values[key] = value
    return values, warnings


def _unquote(value: str) -> str:
    """囲み引用符を外す (Windows でパスを "..." で書きがちなため)。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1].strip()
    return value


def _resolve(base_dir: str, path: str) -> str:
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(base_dir, path)


def _validate_book_min_depth(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"BookMinDepth は整数が必要: {raw!r}")
    if value < 0:
        raise ConfigError(f"BookMinDepth は 0 以上が必要: {value}")
    return value


def _validate_threshold(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"TaskEvalThreshold は整数が必要: {raw!r}")
    if value < 0:
        raise ConfigError(f"TaskEvalThreshold は 0 以上が必要: {value}")
    return value


def _validate_emit_on(raw: str) -> str:
    value = raw.lower()
    if value not in ("loss", "win", "both"):
        raise ConfigError(f"TaskEmitOn は loss/win/both のいずれか: {raw!r}")
    return value


def _validate_tie_break(raw: str) -> str:
    value = raw.lower()
    if value not in TIE_BREAK_CHOICES:
        raise ConfigError(
            f"TieBreak は {'/'.join(TIE_BREAK_CHOICES)} のいずれか: {raw!r}")
    return value


def default_config_path() -> str:
    """既定は wrapper/ の1つ上 (KiriCadence ルート) の config.txt。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.environ.get(CONFIG_ENV) or os.path.join(root, "config.txt")


def load_config(path: str | None = None) -> tuple[Config, list[str]]:
    """起動時読み込み。読めない・不正値は ConfigError (起動失敗)。"""
    path = path or default_config_path()
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        raise ConfigError(f"config を読めません: {path}: {e}")

    raw, warnings = parse_option_text(text)
    values = dict(DEFAULTS)
    values.update(raw)

    if not values["EnginePath"]:
        warnings.append("EnginePath が未設定 (定跡専用モードで動作)")

    base_dir = os.path.dirname(os.path.abspath(path))
    config = Config(
        config_path=os.path.abspath(path),
        base_dir=base_dir,
        engine_path=_resolve(base_dir, values["EnginePath"]),
        engine_encoding=values["EngineEncoding"] or "utf-8",
        log_dir=_resolve(base_dir, values["LogDir"] or DEFAULTS["LogDir"]),
        book_dir=_resolve(base_dir, values["BookDir"] or DEFAULTS["BookDir"]),
        book_min_depth=_validate_book_min_depth(values["BookMinDepth"]),
        tie_break=_validate_tie_break(values["TieBreak"]),
        task_dir=_resolve(base_dir, values["TaskDir"]),
        task_eval_threshold=_validate_threshold(values["TaskEvalThreshold"]),
        task_emit_on=_validate_emit_on(values["TaskEmitOn"]),
    )
    return config, warnings


def reload_book_settings(config: Config) -> tuple[Config, list[str]]:
    """usinewgame 用の再読込。RELOADABLE_KEYS だけ更新する。

    読めない・不正値のキーは**前の値を維持**して警告を返し、決して例外を
    投げない (無人運用中の config 書き損じで落ちない。DESIGN.md §7)。
    """
    try:
        with open(config.config_path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        return config, [f"config 再読込失敗 (前の値で続行): {e}"]

    raw, warnings = parse_option_text(text)
    updated = config

    if "BookDir" in raw and raw["BookDir"]:
        updated = replace(updated,
                          book_dir=_resolve(config.base_dir, raw["BookDir"]))
    if "BookMinDepth" in raw:
        try:
            updated = replace(
                updated,
                book_min_depth=_validate_book_min_depth(raw["BookMinDepth"]))
        except ConfigError as e:
            warnings.append(f"{e} (前の値 {config.book_min_depth} を維持)")
    if "TieBreak" in raw:
        try:
            updated = replace(updated,
                              tie_break=_validate_tie_break(raw["TieBreak"]))
        except ConfigError as e:
            warnings.append(f"{e} (前の値 {config.tie_break!r} を維持)")
    return updated, warnings
