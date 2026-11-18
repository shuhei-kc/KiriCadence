"""Build a verified immutable full-peta release, or install it explicitly.

Building never touches KiriCadence/book. Installing uses the existing Windows
shared-delete/ReplaceFileW protocol. It requires an operator-confirmed idle
window: current KiriCadence does NOT pin a generation for an entire game.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from ybb_ops import Reader, sha256, validate

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
TARGET = PROJECT / "book/650_b_peta_yosc.ybb"


@contextmanager
def lock():
    path = ROOT / "releases/.pipeline.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            # Locking an empty byte range is supported by Windows.
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()


def atomic_json(path, value):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


def build(args):
    started = time.monotonic()
    snapshot = args.snapshot.resolve(strict=True)
    engine = args.engine.resolve(strict=True)
    if snapshot.suffix != ".ybb" or snapshot.name.endswith(".tmp"):
        raise ValueError("A completed immutable .ybb snapshot is required")
    generation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
    folder = ROOT / "releases" / generation
    folder.mkdir(parents=True, exist_ok=False)
    before = snapshot.stat()
    source_info = validate(snapshot)
    if not source_info["positions"]:
        raise ValueError("Empty input")
    # Copy a completed checkpoint, isolating the engine from the live miner DB.
    source = folder / "full.ybb"
    shutil.copyfile(snapshot, source)
    if (before.st_size, before.st_mtime_ns) != (snapshot.stat().st_size, snapshot.stat().st_mtime_ns):
        raise RuntimeError("Snapshot changed during capture")
    auditor = ROOT / "runtime/audit_flip.exe"
    remaining = args.budget_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("Capture exceeded budget")
    audit_run = subprocess.run([str(auditor), str(source)], capture_output=True, text=True,
                               timeout=remaining,
                               creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0)
    if audit_run.returncode:
        raise RuntimeError(f"Flip audit failed: {audit_run.stderr}")
    audit = json.loads(audit_run.stdout)
    if audit["extra_equivalent_records"]:
        raise ValueError("Flip-equivalent duplicate positions must be merged before peta shock")
    audit["source_sha256"] = sha256(source)
    atomic_json(folder / "CANONICAL_INPUT_OK.json", audit)
    book_dir = str(folder.resolve()).replace("\\", "/")
    if any(character.isspace() for character in book_dir):
        raise ValueError("This upstream makebook command requires a BookDir without whitespace")
    commands = (f"usi\nsetoption name BookDir value {book_dir}\n"
                "setoption name BookFile value no_book\n"
                "setoption name FlippedBook value true\n"
                "setoption name USI_Hash value 1\n"
                "setoption name Threads value 1\n"
                f"makebook peta_shock full.ybb peta.ybb threads {args.threads}\nquit\n")
    (folder / "commands.usi").write_text(commands, encoding="utf-8")
    remaining = args.budget_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("Capture exceeded budget; previous release stays active")
    engine_started = time.monotonic()
    with (folder / "engine.log").open("wb") as log:
        child = subprocess.Popen([str(engine)], cwd=folder, stdin=subprocess.PIPE,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0)
        try:
            child.communicate(commands.encode("utf-8"), timeout=remaining)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            raise TimeoutError("Peta deadline exceeded; previous release stays active")
    engine_seconds = time.monotonic() - engine_started
    log_text = (folder / "engine.log").read_text(encoding="utf-8", errors="replace")
    if child.returncode or "Making a peta-shock book has been completed." not in log_text or "Error!" in log_text:
        raise RuntimeError(f"Engine did not confirm successful completion: {folder / 'engine.log'}")
    candidate = folder / "peta.ybb"
    info = validate(candidate, require_depth=True)
    if info["positions"] != source_info["positions"] or not info["moves"]:
        raise ValueError("Unexpected output position count or empty moves")
    report = dict(generation=generation, kind="full-peta", source=str(snapshot),
                  source_sha256=sha256(source), engine=str(engine), engine_sha256=sha256(engine),
                  candidate_sha256=sha256(candidate), input=source_info, output=info,
                  convergence_threads=args.threads, engine_seconds=round(engine_seconds, 3),
                  elapsed_seconds=round(time.monotonic() - started, 3),
                  legal_probe_verified=False, installed=False)
    if report["elapsed_seconds"] > args.budget_seconds:
        raise TimeoutError("Validation exceeded budget; candidate is not READY")
    # Structural readiness is distinct from the cshogi legal-probe gate.
    atomic_json(folder / "STRUCTURAL_READY.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def install(args):
    folder = args.release.resolve(strict=True)
    folder.relative_to((ROOT / "releases").resolve())
    report = json.loads((folder / "STRUCTURAL_READY.json").read_text(encoding="utf-8"))
    gate = json.loads((folder / "LEGAL_PROBES_OK.json").read_text(encoding="utf-8"))
    canonical = json.loads((folder / "CANONICAL_INPUT_OK.json").read_text(encoding="utf-8"))
    if canonical["extra_equivalent_records"] or canonical["source_sha256"] != report["source_sha256"]:
        raise ValueError("Input canonicalization gate failed")
    source = folder / "peta.ybb"
    digest = sha256(source)
    if digest != report["candidate_sha256"] or digest != gate["candidate_sha256"]:
        raise ValueError("Release hash differs from validation records")
    validate(source, require_depth=True)
    if not args.between_games:
        raise ValueError("Installation requires --between-games after confirming wrapper is idle")
    sys.path.insert(0, str(PROJECT / "wrapper"))
    from file_swap import replace_file
    TARGET.parent.mkdir(exist_ok=True)
    # Backup only our own managed output; user-maintained priority books are untouched.
    if TARGET.exists():
        previous = folder / "previous-live.ybb"
        if not previous.exists():
            shutil.copyfile(TARGET, previous)
    temporary = TARGET.with_name(".yosc-peta-install.tmp")
    shutil.copyfile(source, temporary)
    if sha256(temporary) != digest:
        raise ValueError("Staged live copy checksum mismatch")
    replace_file(str(temporary), str(TARGET))
    atomic_json(ROOT / "releases/installed.json", dict(generation=report["generation"],
                target=str(TARGET), sha256=digest, installed_utc=datetime.now(timezone.utc).isoformat()))
    print(f"Installed {TARGET}; use usinewgame to discover it on first installation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--snapshot", type=Path, required=True)
    build_parser.add_argument("--engine", type=Path, default=ROOT / "runtime/material128/YO-MATERIAL.exe")
    build_parser.add_argument("--threads", type=int, choices=range(1, 17), default=2)
    build_parser.add_argument("--budget-seconds", type=int, default=1200)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--release", type=Path, required=True)
    install_parser.add_argument("--between-games", action="store_true")
    args = parser.parse_args()
    with lock():
        build(args) if args.command == "build" else install(args)


if __name__ == "__main__":
    main()
