"""Bounded-memory YANE-BINBOOK-V1 validation, merge and mining-delta extraction.

No SFEN conversion, canonicalization, minimax or peta shock is performed here.
Inputs must be completed, immutable snapshots. Later merge inputs win per move.
Overlay inputs replace the entire position. Diff refuses position deletions.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import heapq
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import time

MAGIC = b"YANE-BINBOOK-V1\0"
HEADER = struct.Struct("<16sQQ")
INDEX = struct.Struct("<32sQHH")
MOVE = struct.Struct("<Hh")
MOVE_DEPTH = struct.Struct("<HhH")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class Reader:
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.index = self.path.open("rb", buffering=4 * 1024 * 1024)
        try:
            self.moves = self.path.open("rb", buffering=4 * 1024 * 1024)
            data = self.index.read(HEADER.size)
            if len(data) != HEADER.size:
                raise ValueError(f"Truncated header: {self.path}")
            magic, self.count, self.flags = HEADER.unpack(data)
            if magic != MAGIC or self.flags & ~1:
                raise ValueError(f"Unsupported YBB magic/flags: {self.path}")
            self.move_struct = MOVE_DEPTH if self.flags & 1 else MOVE
            self.moves_base = HEADER.size + INDEX.size * self.count
            self.size = self.path.stat().st_size
            if self.moves_base > self.size:
                raise ValueError(f"Truncated index: {self.path}")
            self.moves.seek(self.moves_base)
            return self
        except BaseException:
            self.index.close()
            if hasattr(self, "moves"):
                self.moves.close()
            raise

    def __exit__(self, *args):
        self.index.close()
        self.moves.close()

    def __iter__(self):
        previous = None
        expected_offset = 0
        for number in range(self.count):
            data = self.index.read(INDEX.size)
            if len(data) != INDEX.size:
                raise ValueError(f"Truncated index record {number}")
            key, offset, ply, count = INDEX.unpack(data)
            if previous is not None and key <= previous:
                raise ValueError(f"Index is not strictly sorted at {number}")
            if offset != expected_offset:
                raise ValueError(f"Noncanonical, overlapping or gapped moves offset at {number}")
            expected_offset += count * self.move_struct.size
            if self.moves_base + expected_offset > self.size:
                raise ValueError(f"Moves outside file at {number}")
            blob = self.moves.read(count * self.move_struct.size)
            moves = {}
            for record in self.move_struct.iter_unpack(blob):
                move, value = record[:2]
                if move in moves:
                    raise ValueError(f"Duplicate move at position {number}")
                moves[move] = (value, record[2] if self.flags & 1 else 0)
            previous = key
            yield key, ply, moves
        if self.moves_base + expected_offset != self.size:
            raise ValueError(f"Unexpected trailing bytes: {self.path}")


def write(path, records, depth=True):
    """Sequential spools avoid alternating index/move seeks on a huge output."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Immutable output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    move_count = 0
    previous = None
    with tempfile.TemporaryDirectory(prefix=".ybb-", dir=path.parent) as directory:
        folder = Path(directory)
        with (folder / "index").open("wb") as index, (folder / "moves").open("wb") as moves:
            for key, ply, entries in records:
                if len(key) != 32 or (previous is not None and key <= previous):
                    raise ValueError("Output keys must be strictly sorted")
                if not 0 <= ply <= 65535 or len(entries) > 65535:
                    raise ValueError("Invalid ply/move count")
                index.write(INDEX.pack(key, moves.tell(), ply, len(entries)))
                # BookMiner's branch traversal also uses the first N moves.
                # Keep best eval first; never serialize in numeric Move16 order.
                for move, (value, move_depth) in sorted(entries.items(), key=lambda item: (-item[1][0], item[0])):
                    moves.write(MOVE_DEPTH.pack(move, value, move_depth) if depth else MOVE.pack(move, value))
                previous = key
                count += 1
                move_count += len(entries)
        completed = folder / "complete"
        with completed.open("wb") as output:
            output.write(HEADER.pack(MAGIC, count, int(depth)))
            for name in ("index", "moves"):
                with (folder / name).open("rb") as source:
                    shutil.copyfileobj(source, output, 8 * 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        # Unique output paths are a caller contract; no overwrite/reuse of generations.
        if path.exists():
            raise FileExistsError(path)
        os.rename(completed, path)
    return {"positions": count, "moves": move_count, "bytes": path.stat().st_size, "depth": depth}


def combined(paths, overlay=False):
    with ExitStack() as stack:
        streams = [iter(stack.enter_context(Reader(path))) for path in paths]
        heap = []
        for number, stream in enumerate(streams):
            if (record := next(stream, None)) is not None:
                heapq.heappush(heap, (record[0], number, record))
        while heap:
            key = heap[0][0]
            group = []
            while heap and heap[0][0] == key:
                _, number, record = heapq.heappop(heap)
                group.append((number, record))
                if (following := next(streams[number], None)) is not None:
                    heapq.heappush(heap, (following[0], number, following))
            group.sort(key=lambda item: item[0])
            if overlay:
                yield group[-1][1]
            else:
                entries = {}
                for _, record in group:
                    entries.update(record[2])
                yield key, min(record[1] for _, record in group), entries


def difference(base, snapshot, raw=False):
    """Whole-position replacements preserve changed and removed moves, not just new keys."""
    with Reader(base) as before, Reader(snapshot) as after:
        old_iter, new_iter = iter(before), iter(after)
        old = next(old_iter, None)
        for new in new_iter:
            if old is not None and old[0] < new[0]:
                raise ValueError("Snapshot removed a base position; deletion/tombstone support is required")
            if old is None or new[0] < old[0]:
                yield new
                continue
            old_moves = {move: value[0] for move, value in old[2].items()} if raw else old[2]
            new_moves = {move: value[0] for move, value in new[2].items()} if raw else new[2]
            if old[1] != new[1] or old_moves != new_moves:
                yield new
            old = next(old_iter, None)
        if old is not None:
            raise ValueError("Snapshot removed trailing base positions")


def validate(path, require_depth=False):
    with Reader(path) as reader:
        if require_depth and not reader.flags & 1:
            raise ValueError("Peta release must contain MOVE_DEPTH")
        moves = sum(len(record[2]) for record in reader)
        return {"positions": reader.count, "moves": moves, "bytes": reader.size, "flags": reader.flags}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate")
    check.add_argument("input", type=Path)
    check.add_argument("--require-depth", action="store_true")
    for name in ("merge", "overlay"):
        command = sub.add_parser(name)
        command.add_argument("inputs", nargs="+", type=Path)
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--raw", action="store_true", help="Drop depth for a BookMiner seed")
    delta = sub.add_parser("diff")
    delta.add_argument("base", type=Path)
    delta.add_argument("snapshot", type=Path)
    delta.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    if args.command == "validate":
        result = validate(args.input, args.require_depth)
    elif args.command == "diff":
        result = write(args.output, difference(args.base, args.snapshot), depth=True)
        result.update(base_sha256=sha256(args.base), snapshot_sha256=sha256(args.snapshot), kind="raw-position-overlay-no-deletions")
    else:
        result = write(args.output, combined(args.inputs, overlay=args.command == "overlay"), depth=not args.raw)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if args.command != "validate":
        result["sha256"] = sha256(args.output)
        # Manifest appears last and is the completion marker for downstream jobs.
        manifest = args.output.with_suffix(args.output.suffix + ".json")
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
