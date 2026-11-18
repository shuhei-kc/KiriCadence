"""Verify candidate moves through the same vendor reader used by KiriCadence.

Run under the existing Python 3.12 with cshogi/numpy. This is a sampled legality
and binary-reader compatibility gate; it is not a proof of minimax correctness.
"""
import argparse
import json
from pathlib import Path
import struct
import sys

from release import ROOT, PROJECT, atomic_json
from ybb_ops import HEADER, INDEX, Reader, sha256

sys.path.insert(0, str(PROJECT / "wrapper"))
import cshogi
import numpy as np
from yaneuraou_book_lib import YbbBookProbe
from flip import flipped_sfen_main, flipped_move


def verify(path, samples=1000):
    before = sha256(path)
    with Reader(path) as reader:
        count = reader.count
    if count == 0:
        raise ValueError("Empty book cannot pass the release gate")
    indices = sorted({i * (count - 1) // max(1, samples - 1) for i in range(min(samples, count))})
    board = cshogi.Board()
    checked_moves = 0
    flips = 0
    with Path(path).open("rb") as stream, YbbBookProbe(path) as probe:
        for index in indices:
            stream.seek(HEADER.size + index * INDEX.size)
            key, _, _, _ = INDEX.unpack(stream.read(INDEX.size))
            board.set_psfen(np.frombuffer(key, dtype=np.uint8).copy())
            original_sfen = board.sfen()
            moves = probe.probe(original_sfen)
            if not moves:
                raise ValueError(f"Empty or unreadable sampled position {index}")
            for move in moves:
                parsed = board.move_from_usi(move.move)
                if not parsed or not board.is_legal(parsed):
                    raise ValueError(f"Illegal sampled move {move.move} at {original_sfen}")
                checked_moves += 1
            # Confirm the wrapper's actual flip functions produce legal reverse-side moves.
            flipped = flipped_sfen_main(' '.join(original_sfen.split()[:3]))
            board.set_sfen(flipped + ' 1')
            for move in moves:
                transformed = flipped_move(move.move)
                parsed = board.move_from_usi(transformed)
                if not parsed or not board.is_legal(parsed):
                    raise ValueError("Illegal flipped move")
                flips += 1
    if sha256(path) != before:
        raise ValueError("Candidate changed during validation")
    return dict(candidate_sha256=before, positions_sampled=len(indices), moves_checked=checked_moves,
                flipped_moves_checked=flips, scope="sampled-legality-and-vendor-probe")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--release-gate", action="store_true")
    args = parser.parse_args()
    if args.samples < 2:
        raise ValueError("At least two sample positions are required")
    result = verify(args.candidate, args.samples)
    if args.release_gate:
        folder = args.candidate.resolve().parent
        folder.relative_to((ROOT / "releases").resolve())
        if args.candidate.name != "peta.ybb":
            raise ValueError("Release candidate must be peta.ybb")
        atomic_json(folder / "LEGAL_PROBES_OK.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
