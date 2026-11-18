"""BookMiner GUI helpers. No scheduler, miner startup or background service."""
from __future__ import annotations
import collections, hashlib, json, os, re, threading, time, uuid
from pathlib import Path
import cshogi
from tuyoura_priority import ROOT, PetaView, play, position_id, native_next
from tuyoura_mining import Job, Proc, atomic, read
from tuyoura_production import publish
from ybb_ops import Reader, validate, difference, write
from legal_probes import verify
from modified_peta import patch_snapshot, verify_overrides, publish_white


def decode(command):
    """Keep the complete user route, including startpos history."""
    text = command.strip().removeprefix('position ').split(',')[0].strip()
    parts = text.split(' moves ', 1)
    origin = parts[0].removeprefix('sfen ')
    moves = parts[1].split() if len(parts) > 1 else []
    return play(origin, moves), 'position ' + text


def roots_from(path):
    path = Path(path)
    if not path.exists():
        raise ValueError('peta_start_sfens.txt がありません。開始局面を記入してください。')
    result = []; seen = set()
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        board, command = decode(line); ident = position_id(board)
        if ident in seen:
            continue
        seen.add(ident)
        result.append({'id': ident, 'label': f'行{number} / {board.move_number-1}手後',
                       'sfen': board.sfen(), 'command': command})
    if not result:
        raise ValueError('peta_start_sfens.txt に開始局面がありません。')
    return result


def best_frontiers(root, pv, max_ply=200, max_visits=10000):
    """All tied local best moves on BOTH turns, first missing positions only."""
    todo = collections.deque([(root.sfen(), [])]); visited = set(); result = []
    while todo:
        sfen, route = todo.popleft(); board = cshogi.Board(sfen)
        ident = position_id(board)
        if ident in visited or board.move_number >= max_ply:
            continue
        visited.add(ident)
        if len(visited) > max_visits:
            raise ValueError('最善手順の走査上限。途中までを全件確認済みとは扱いません。')
        if board.is_game_over() or board.is_nyugyoku():
            continue
        moves = pv.moves(board)
        if not moves:
            result.append((sfen, route)); continue
        best = max(value for move, value, depth in moves)
        for move, value, depth in moves:
            if value != best:
                continue
            child = cshogi.Board(sfen); child.push_usi(move)
            todo.append((child.sfen(), route + [move]))
    return result


def extend(command, route):
    return command + ((' ' if ' moves ' in command else ' moves ') + ' '.join(route) if route else '')


def prune_completed(work, snapshot, keep=4):
    """Bound disk use, only exact filenames under this GUI's own directories."""
    work = Path(work).resolve(); snapshot = Path(snapshot).resolve()
    backups = work/'book/backup'; generations = work/'generations'
    complete = sorted(p for p in backups.glob('book_miner-*.ybb')
                      if re.fullmatch(r'book_miner-\d{14}_\d+\.ybb', p.name))
    removed = []
    for path in complete[:-keep]:
        if path.resolve() != snapshot and path.resolve().parent == backups.resolve():
            path.unlink(); removed.append(str(path))
    ready = collections.defaultdict(list)
    for path in sorted(generations.glob('*/READY.json')):
        ready[read(path,{}).get('kind','normal')].append(path.parent)
    for folder in [p for group in ready.values() for p in group[:-2]]:
        if folder.resolve().parent != generations.resolve():
            raise ValueError('GUI世代フォルダが作業場所の外を指しています。')
        for name in ('peta.ybb','mining-delta.ybb','modified-input.ybb'):
            path = folder/name
            if path.exists():
                path.unlink(); removed.append(str(path))
        atomic(folder/'PRUNED.json', {'time':time.time()})
    return removed


def make_plan(root_path, peta, per_root=8, max_diff=30, max_ply=200):
    roots = roots_from(root_path); pv = PetaView(peta)
    try:
        for task in roots:
            board = cshogi.Board(task['sfen']); best = best_frontiers(board, pv, max_ply)
            # pn is the already-verified upstream-compatible implementation.
            # Start narrow; widen only within the user's visible upper bound.
            diffs = sorted({0, min(5, max_diff), min(15, max_diff), max_diff})
            chosen = []; used_diff = 0; warnings = []
            for diff in diffs:
                try:
                    candidates = native_next(board, pv, diff, max_ply, max_visits=20000)
                except RuntimeError as exc:
                    warnings.append(str(exc)); break
                chosen, used_diff = candidates, diff
                if len(chosen) >= per_root:
                    break
            pool = []; unique = set()
            for kind, items in [('best', best), ('pn', chosen)]:
                for sfen, route in items:
                    ident = position_id(cshogi.Board(sfen))
                    if ident in unique:
                        continue
                    unique.add(ident)
                    pool.append({'id': ident, 'sfen': sfen, 'kind': kind,
                                 'command': extend(task['command'], route), 'root': task['id']})
                    # Never truncate best ties merely to meet the pn quota.
                    if kind == 'pn' and len(pool) >= max(per_root, len(best)):
                        break
            task.update(black_cp=pv.black_value(board), diff=used_diff, best_count=len(best),
                        pool=pool, warnings=warnings)
    finally:
        pv.close()
    return {'created_unix': time.time(), 'peta': str(peta), 'tasks': roots,
            'max_diff': max_diff, 'per_root': per_root}


class BalancedQueue:
    """Bounded shared queue. Best leaves first; round-robin roots, global dedup."""
    def __init__(self):
        self.plan = {'tasks': []}; self.seen = set(); self.counts = collections.Counter()
        self.cursors = collections.defaultdict(int); self.slot = 0

    def install(self, plan):
        self.plan = plan

    def take(self, count, low_bias=False):
        tasks = self.plan['tasks']; result = []
        known = sorted((t for t in tasks if t['black_cp'] is not None), key=lambda t:t['black_cp'])
        cutoff = known[(len(known)-1)//2]['black_cp'] if known else None
        groups = {'all': tasks,
                  'low': [t for t in tasks if t['black_cp'] is None or t['black_cp'] <= cutoff],
                  'high': [t for t in tasks if t['black_cp'] is not None and t['black_cp'] > cutoff]}
        for _ in range(count):
            group = ('high' if self.slot % 5 == 4 else 'low') if low_bias else 'all'
            selected = None
            # Exact best frontier coverage precedes broader pn in both groups.
            for kind in ('best', 'pn'):
                for scope in (group, 'all'):
                    members = groups[scope]
                    for _ in members:
                        i = self.cursors[scope] % len(members); self.cursors[scope] += 1
                        task = members[i]
                        selected = next((x for x in task['pool'] if x['kind'] == kind and x['id'] not in self.seen), None)
                        if selected:
                            break
                    if selected:
                        break
                if selected:
                    break
            if not selected:
                break
            self.seen.add(selected['id']); self.counts[selected['root']] += 1
            self.slot += 1; result.append(selected)
        return result

    def rollback(self, batch):
        for item in batch:
            self.seen.discard(item['id']); self.counts[item['root']] -= 1


class InputSpool:
    """Never touch user think_sfens.txt or peta_start_sfens.txt."""
    def __init__(self, work):
        self.work = Path(work)

    def create(self, commands, kind='auto'):
        lines = []
        for line in commands:
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            decode(line)  # Reject an illegal route before sending anything.
            lines.append(line.strip())
        if not lines:
            raise ValueError('投入する局面がありません。')
        path = self.work/'book/auto'/(kind+'-'+uuid.uuid4().hex+'.txt')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines)+'\n', encoding='utf-8')
        return path


class PetaPipeline:
    """One GUI-owned resident petad, load once then apply completed snapshots."""
    def __init__(self, config, log=lambda *args:None):
        self.cfg = config; self.work = Path(config['workdir']); self.log = log
        self.job = None; self.proc = None; self.source = None
        self.lock = threading.Lock(); self.cancelled = threading.Event()

    def check_cancel(self):
        if self.cancelled.is_set():
            raise RuntimeError('GUI停止指示により更新を中止しました。')

    def run(self, snapshot, publish_enabled=True, kind='normal'):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('ペタ更新は既に実行中です。')
        try:
            self.check_cancel(); snapshot = Path(snapshot).resolve()
            if kind not in ('normal','white'):raise ValueError('Unknown peta output kind')
            if not re.fullmatch(r'book_miner-\d{14}_\d+\.ybb', snapshot.name):
                raise ValueError('完成した通常snapshotのみ受け付けます。')
            with Reader(snapshot) as reader:
                positions = reader.count
            folder = self.work/'generations'/(time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6])
            folder.mkdir(parents=True); phases = {}; started = time.time()
            source = snapshot; profile = None; overrides = None
            if kind == 'white':
                # Edit a private raw input, then use the unchanged standard solver.
                overrides = folder/'overrides.json'
                overrides.write_bytes(Path(self.cfg['alternating_peta']['overrides']).read_bytes())
                source = folder/'modified-input.ybb'
                profile = patch_snapshot(snapshot,source,overrides,self.check_cancel,self.log)
                positions = profile['positions']
                verify_overrides(source,overrides)
            if not self.proc:
                self.job = Job()
                self.proc = Proc('petad', [self.cfg['petad']], ROOT, self.job, self.log)
                self.proc.wait(r'^\{'); self.proc.command_json('threads '+str(self.cfg.get('petad_threads', 3)))
            operation = 'load' if self.source is None else 'apply'
            self.log('pipeline', operation+' '+str(source)); self.check_cancel()
            phases[operation] = self.proc.command_json(operation+' '+str(source)); self.source = str(source)
            self.check_cancel(); self.log('pipeline', 'solve')
            phases['solve'] = self.proc.command_json('solve')
            self.check_cancel(); peta = folder/'peta.ybb'
            phases['write'] = self.proc.command_json('write '+str(peta))
            self.log('pipeline', 'validation'); self.check_cancel()
            structural = validate(peta, True); legal = verify(peta, self.cfg.get('legal_samples', 1000))
            if structural['positions'] != positions:
                raise ValueError('snapshotとペタの局面数が一致しません。')
            if kind == 'white':phases['overrides_verified'] = verify_overrides(peta,overrides)
            self.check_cancel(); delta = None
            if kind == 'normal' and self.cfg.get('baseline'):
                self.log('pipeline', 'mining-delta')
                delta = write(folder/'mining-delta.ybb', difference(self.cfg['baseline'], snapshot), depth=True)
            self.check_cancel(); publication = None
            if publish_enabled:
                self.log('pipeline', 'publication')
                publication = (publish(peta, self.cfg['target'], legal['candidate_sha256']) if kind == 'normal'
                               else publish_white(peta,self.cfg['alternating_peta']['target'],legal['candidate_sha256']))
            report = {'kind':kind, 'snapshot': str(snapshot), 'input':str(source), 'profile':profile,
                      'started_unix':started, 'peta': str(peta), 'operation': operation,
                      'petad_pid': self.proc.p.pid, 'structural': structural, 'legal': legal,
                      'phases': phases, 'delta': delta, 'publication': publication,
                      'completed_unix': time.time(), 'elapsed_s': time.time()-started}
            atomic(folder/'READY.json', report)
            atomic(self.work/('current-white.json' if kind=='white' else 'current.json'), report)
            prune_completed(self.work, snapshot)
            return report
        except Exception:
            # Error state is explicit; next user retry starts from load, never
            # assumes an unsuccessful apply preserved a usable solver result.
            self.close(); raise
        finally:
            self.lock.release()

    def close(self):
        proc = self.proc
        if self.job:
            self.job.close()
        if proc:
            try:
                proc.p.wait(timeout=10)
            except Exception:
                proc.p.kill(); proc.p.wait(timeout=10)
            for stream in (proc.p.stdin, proc.p.stdout):
                if stream:
                    stream.close()
        self.proc = None; self.job = None; self.source = None

    def cancel(self):
        self.cancelled.set(); self.close()


def check_production_settings(work):
    """Validate user settings without imposing a particular research profile."""
    import json5
    work = Path(work)
    settings = json5.loads((work/'settings/engine_settings.json5').read_text(encoding='utf-8-sig'))
    if not isinstance(settings, list) or not settings:
        raise ValueError('engine_settings.json5 にエンジン設定がありません。')
    for item in settings:
        for field in ('nodes', 'multi'):
            value = item.get(field, 1 if field == 'multi' else None)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f'{field} には正の整数を指定してください。')
        path = Path(item['path'])
        if not path.is_absolute():
            path = work/path
        if not path.is_file():
            raise FileNotFoundError(path)
    return {'engines': settings, 'workers': sum(item.get('multi',1) for item in settings)}
