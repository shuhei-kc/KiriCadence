"""SFEN・USI指し手の先後反転 (DESIGN.md §5.2)。

custom_engine3 book_yane.py の実証済み実装を移植したもの。
どのファイルを見る際も、直接検索が空振りしたら反転局面で引き、
得た指し手をこの関数群で実局面の向きへ戻す。
"""

from __future__ import annotations

#: 反転時の筋・段の対応
_FLIP = {"1": "9", "2": "8", "3": "7", "4": "6", "5": "5",
         "6": "4", "7": "3", "8": "2", "9": "1",
         "a": "i", "b": "h", "c": "g", "d": "f", "e": "e",
         "f": "d", "g": "c", "h": "b", "i": "a"}


def flipped_move(usi: str) -> str | None:
    """USI手を先後反転する。反転できない文字列なら None。

    打つ手 (P*5e) の駒種文字は色に依らず大文字なのでそのまま残す。
    成りの '+' サフィックスもそのまま。
    """
    if len(usi) not in (4, 5):
        return None
    try:
        if usi[1] == "*":
            return f"{usi[0]}*{_FLIP[usi[2]]}{_FLIP[usi[3]]}{usi[4:]}"
        return (f"{_FLIP[usi[0]]}{_FLIP[usi[1]]}"
                f"{_FLIP[usi[2]]}{_FLIP[usi[3]]}{usi[4:]}")
    except KeyError:
        return None


def flipped_sfen_main(sfen_main: str) -> str | None:
    """'<board> <turn> <hands>' (手数なしSFEN) を先後反転する。壊れていれば None。

    盤面は文字列を逆順にして大小反転すると180度回転になる。逆順にすると
    成駒の '+' が駒の後ろへ回るので、隣と入れ替えて前へ戻す。
    持駒は「大文字群 + 小文字群」の並びなので、最後の大文字で切って
    前後を入れ替えてから大小反転する (枚数の数字は駒文字の直前にあるため
    切れ目は必ず最後の大文字の直後になる)。
    """
    parts = sfen_main.split()
    if len(parts) != 3 or parts[1] not in ("b", "w"):
        return None
    board_text, turn, hands = parts

    chars = list(board_text[::-1].swapcase())
    for index in range(1, len(chars)):
        if chars[index] == "+":
            chars[index], chars[index - 1] = chars[index - 1], chars[index]
    board_text = "".join(chars)

    split_at = next(
        (i for i in range(len(hands) - 1, -1, -1) if hands[i].isupper()), -1)
    hands = (hands[split_at + 1:] + hands[:split_at + 1]).swapcase()

    return f"{board_text} {'w' if turn == 'b' else 'b'} {hands}"
