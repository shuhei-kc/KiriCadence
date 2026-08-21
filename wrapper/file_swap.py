"""定跡ファイルを「読みながら差し替える」ための Windows 対応。

Windows では、既定の `open(path, "rb")` は FILE_SHARE_DELETE を渡さないため、
**読み取りで開いているだけのファイルを他プロセスが置き換えられない**
(`os.replace` が PermissionError [WinError 5])。定跡は生成側が随時
差し替えるので、両側を合わせないと実運用で衝突する。実測:

| 読み手 | 置き換え | 結果 |
|---|---|---|
| 通常 open | `os.replace` | 失敗 (WinError 5) |
| 共有削除つき | `os.replace` | 失敗 (MoveFileEx は対象の排他が要る) |
| 通常 open | `ReplaceFileW` | 失敗 (WinError 32) |
| **共有削除つき** | **`ReplaceFileW`** | **成功** |

成功する組み合わせでは、読み手は古い内容を最後まで読み切れる
(ハンドルが古いファイル実体を保持する = 一貫したスナップショット)。

POSIX では通常の open と os.replace で同じ性質が得られるので、
Windows のときだけ専用経路を使う。
"""

from __future__ import annotations

import os
import time

_WINDOWS = os.name == "nt"

if _WINDOWS:                        # pragma: no cover - プラットフォーム依存
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _SHARE_ALL = 0x1 | 0x2 | 0x4    # READ | WRITE | DELETE
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    _kernel32 = ctypes.windll.kernel32
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.restype = wintypes.HANDLE
    _CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
    _ReplaceFileW = _kernel32.ReplaceFileW
    _ReplaceFileW.restype = wintypes.BOOL
    _ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                              wintypes.LPCWSTR, wintypes.DWORD,
                              ctypes.c_void_p, ctypes.c_void_p]


def open_shared(path):
    """読み取り用に開く。**開いている間も他プロセスが置き換えられる**。

    失敗したら通常の open にフォールバックする (読めることを優先する)。
    """
    if not _WINDOWS:
        return open(path, "rb")
    try:
        handle = _CreateFileW(str(path), _GENERIC_READ, _SHARE_ALL, None,
                              _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
        if not handle or handle == _INVALID_HANDLE:
            return open(path, "rb")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        return os.fdopen(fd, "rb")
    except Exception:
        return open(path, "rb")


def replace_file(src: str, dst: str, attempts: int = 10,
                 delay: float = 0.1) -> None:
    """src で dst を置き換える。読み手がいても成功する経路を使う。

    ウイルス対策や検索インデクサなど、共有削除を付けずに開くプロセスも
    いるのでリトライも入れる (待てば必ず閉じる類のもの)。
    最後まで失敗したら例外をそのまま送出する。
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            if _WINDOWS and os.path.exists(dst):
                if _ReplaceFileW(str(dst), str(src), None, 0, None, None):
                    return
                raise OSError(ctypes.GetLastError(),
                              f"ReplaceFileW に失敗しました: {dst}")
            os.replace(src, dst)
            return
        except OSError as e:
            last = e
            if attempt < attempts - 1:
                time.sleep(delay * (attempt + 1))
    raise last if last is not None else OSError(f"置き換えに失敗: {dst}")
