"""SFEN 文字列の事前検証。

**cshogi の set_sfen は不正な SFEN でプロセスごと落ちる** (Python の例外に
ならないので try/except では受けられない)。実測: 段のマス数が 9 でない
SFEN を渡すとネイティブ側で異常終了する。

手入力や外部データを set_sfen へ渡す前に、必ずここで検証する。
純 Python なので落ちない。
"""

from __future__ import annotations

#: 盤面に置ける駒 (成りは '+' を前置。玉と金は成れない)
_PIECES = "PLNSGBRK"
_PROMOTABLE = "PLNSBR"
#: 持駒に置ける駒 (玉は持てない)
_HAND_PIECES = "PLNSGBR"


def validate_board(board: str) -> str | None:
    ranks = board.split("/")
    if len(ranks) != 9:
        return f"盤面は9段必要です (実際は{len(ranks)}段)"
    for index, rank in enumerate(ranks, start=1):
        squares = 0
        promoted = False
        if not rank:
            return f"{index}段目が空です"
        for char in rank:
            if char == "+":
                if promoted:
                    return f"{index}段目に '+' が連続しています"
                promoted = True
                continue
            if char.isdigit():
                if char == "0":
                    return f"{index}段目に 0 は使えません"
                if promoted:
                    return f"{index}段目の '+' の後が数字です"
                squares += int(char)
                continue
            if char.upper() not in _PIECES:
                return f"{index}段目に不正な駒文字 {char!r} があります"
            if promoted and char.upper() not in _PROMOTABLE:
                return f"{index}段目の {char!r} は成れません"
            promoted = False
            squares += 1
        if promoted:
            return f"{index}段目が '+' で終わっています"
        if squares != 9:
            return f"{index}段目のマス数が {squares} です (9 が必要)"
    return None


def validate_hands(hands: str) -> str | None:
    if hands == "-":
        return None
    if not hands:
        return "持駒が空文字です ('-' が必要)"
    count = ""
    for char in hands:
        if char.isdigit():
            count += char
            continue
        if char.upper() not in _HAND_PIECES:
            return f"持駒に不正な駒文字 {char!r} があります"
        if count and int(count) == 0:
            return "持駒の枚数に 0 は使えません"
        count = ""
    if count:
        return "持駒が数字で終わっています"
    return None


def validate_sfen(sfen: str) -> str | None:
    """SFEN を検証する。問題なければ None、あればエラー文を返す。

    手数は省略可 (定跡キーは手数なしで扱うため)。
    """
    text = sfen.strip()
    if text.startswith("sfen "):
        text = text[5:].strip()
    parts = text.split()
    if len(parts) < 3:
        return f"SFEN の項目が足りません: {sfen!r}"
    if len(parts) > 4:
        return f"SFEN の項目が多すぎます: {sfen!r}"
    error = validate_board(parts[0])
    if error:
        return error
    if parts[1] not in ("b", "w"):
        return f"手番は b か w です: {parts[1]!r}"
    error = validate_hands(parts[2])
    if error:
        return error
    if len(parts) == 4 and not parts[3].isdigit():
        return f"手数が数値ではありません: {parts[3]!r}"
    # 玉が多すぎる盤面は cshogi 側で不定になり得るので弾く
    for king, label in (("K", "先手玉"), ("k", "後手玉")):
        if parts[0].count(king) > 1:
            return f"{label}が複数あります"
    return None


def safe_set_sfen(board, sfen: str) -> str | None:
    """検証してから set_sfen する。問題があれば設定せずエラー文を返す。"""
    error = validate_sfen(sfen)
    if error is not None:
        return error
    try:
        board.set_sfen(sfen.strip()[5:].strip()
                       if sfen.strip().startswith("sfen ") else sfen.strip())
    except Exception as e:      # 検証を通っても cshogi が拒む場合の保険
        return f"局面を設定できません: {e!r}"
    return None
