"""Supplement stock pn frontiers with first missing nodes in recorded game trees.

Only completed, task-local game edges are followed. No evaluation is imported
from game results, and no upstream BookMiner or peta solver code is changed.
"""
from __future__ import annotations
import argparse, collections, json, sqlite3, time
from pathlib import Path
import cshogi
from tuyoura_priority import PetaView, key, position_id
from tuyoura_mining import Allocator, atomic, read


def observed_frontiers(task, db, pv, ordinary_ids, excluded=(), limit=32, max_ply=200, source_kind='tuyoura_game'):
    root = cshogi.Board(task['root_sfen'])
    edge_map = collections.defaultdict(list)
    for parent, move, child, visits in db.execute(
            'select parent,move,child,visits from edges where task=?', (task['id'],)):
        edge_map[parent].append((move, child, visits))
    nodes = {r[0]: r[1:] for r in db.execute(
        'select sfen,visits,black_wins,white_wins,other from nodes where task=?', (task['id'],))}
    root_moves = pv.moves(root)
    root_value = max((m[1] for m in root_moves), default=0)
    todo = collections.deque([(root.sfen(), [], 3, root_value)])
    visited, candidates, excluded = set(), {}, set(excluded)
    stats = collections.Counter()
    while todo:
        sfen, route, modes, bound = todo.popleft()
        board = cshogi.Board(sfen)
        parent = key(board)
        if parent in visited or board.move_number >= max_ply:
            continue
        visited.add(parent)
        moves = pv.moves(board)
        if not moves:
            # Descendants beyond a missing node are not independently submitted.
            continue
        by_move = {m[0]: m[1] for m in moves}
        best = max(by_move.values())
        for move, expected, visits in sorted(edge_map.get(parent, ())):
            child = cshogi.Board(sfen)
            parsed = child.move_from_usi(move)
            if not parsed or not child.is_legal(parsed):
                raise ValueError('Illegal completed-game edge: ' + task['id'] + ' ' + move)
            child.push(parsed)
            if key(child) != expected:
                raise ValueError('Completed-game edge child does not match')
            if child.move_number >= max_ply or child.is_game_over() or child.is_nyugyoku():
                continue
            next_route = route + [move]
            value = by_move.get(move)
            next_modes = 0
            for turn in (0, 1):
                threshold = best if board.move_number % 2 == turn else bound - task['diff']
                if modes & (1 << turn) and value is not None and value >= threshold:
                    next_modes |= 1 << turn
            if pv.moves(child):
                todo.append((child.sfen(), next_route, next_modes, -bound))
                continue
            stats['first_missing_frontiers'] += 1
            ident = position_id(child)
            if ident in ordinary_ids:
                stats['already_in_stock_pn'] += 1
                continue
            if ident in excluded:
                stats['already_submitted_game_candidate'] += 1
                continue
            # This is the stock inline-route eval_limit condition.
            if value is not None and abs(value) > 400:
                stats['outside_stock_eval_limit'] += 1
                continue
            evidence = nodes.get(expected)
            if evidence is None:
                raise ValueError('Observed edge has no observed child')
            item = {
                'id': ident, 'sfen': child.sfen(),
                'position_command': 'position sfen ' + root.sfen() + ' moves ' + ' '.join(next_route),
                'peta_route': next_route, 'source_kind': source_kind,
                'reason': 'recorded_move_missing_in_peta' if value is None else 'recorded_branch_outside_pn',
                'parent_sfen': sfen, 'recorded_move': move, 'edge_games': visits,
                'child_games': evidence[0], 'black_wins': evidence[1],
                'white_wins': evidence[2], 'other_results': evidence[3],
                'parent_move_cp': value, 'pn_path_excluded': next_modes == 0,
                'distance_from_task': len(next_route),
            }
            previous = candidates.get(ident)
            if previous is None or (-visits, len(next_route)) < (-previous['edge_games'], previous['distance_from_task']):
                candidates[ident] = item
    # Alternate frequent and rare observed branches. Wins are evidence only;
    # one fortunate game must not become a synthetic book evaluation.
    frequent = collections.deque(sorted(candidates.values(), key=lambda x: (-x['edge_games'], x['distance_from_task'], x['id'])))
    rare = collections.deque(sorted(candidates.values(), key=lambda x: (x['edge_games'], x['distance_from_task'], x['id'])))
    chosen, chosen_ids = [], set()
    while len(chosen) < min(limit, len(candidates)):
        source = frequent if len(chosen) % 2 == 0 else rare
        while source and source[0]['id'] in chosen_ids:
            source.popleft()
        if not source:
            break
        item = dict(source.popleft(), selection_lane='frequent' if len(chosen) % 2 == 0 else 'rare')
        chosen.append(item)
        chosen_ids.add(item['id'])
    return chosen, dict(stats, observed_parents_scanned=len(visited), eligible=len(candidates), selected=len(chosen))


def augment(plan, frequency, output, excluded=(), limit=32):
    db = sqlite3.connect(Path(frequency).resolve().as_uri() + '?mode=ro', uri=True)
    pv = PetaView(plan['peta'])
    result = dict(plan, tasks=[])
    try:
        row = db.execute("select value from metadata where key='source'").fetchone()
        source = json.loads(row[0]) if row else {}
        ordinary_ids = {p['id'] for t in plan['tasks'] for p in t['pool']}
        for task in plan['tasks']:
            pool, stats = observed_frontiers(task, db, pv, ordinary_ids, excluded, limit, plan['max_ply'], source.get('candidate_source_kind','tuyoura_game'))
            result['tasks'].append(dict(task, game_pool=pool, game_candidate_stats=stats))
    finally:
        pv.close()
        db.close()
    result['game_diversity'] = {
        'created_unix': time.time(), 'completed_games': source.get('completed_games'),
        'reference_source': source.get('source', str(frequency)),
        'selection': 'first missing child via observed task-tree edges; exclude stock pn pool',
        'ordering': 'alternate frequent and rare observed edges; no outcome-based evaluation',
        'quota': 'every fifth submission per task; no extra workers or nodes',
        'candidate_limit_per_task': limit,
    }
    atomic(output, result)
    return result


class GameAllocator(Allocator):
    """Keep the existing low/high RR; reserve one in five task slots for games."""
    def __init__(self, plan, seen=(), cursors=None, slot=0, task_counts=None, game_seen=None, game_counts=None):
        super().__init__(plan, seen, cursors, slot)
        self.task_counts = collections.Counter(task_counts or {})
        self.game_seen = game_seen if game_seen is not None else set()
        self.game_counts = game_counts if game_counts is not None else collections.Counter()
        self.lane_phases = {task['id']: n % 2 for n, task in enumerate(sorted(plan['tasks'], key=lambda x: x['id']))}
        self.game_offsets = collections.defaultdict(int)

    def take(self, tid, game, lane=None):
        pool = self.by_id[tid].get('game_pool', []) if game else self.by_id[tid]['pool']
        if game:
            pool = [x for x in pool if x.get('selection_lane', lane) == lane]
        offsets = self.game_offsets if game else self.offsets
        offset_key = (tid, lane) if game else tid
        while offsets[offset_key] < len(pool):
            item = pool[offsets[offset_key]]
            offsets[offset_key] += 1
            if item['id'] in self.seen or item['id'] in self.game_seen:
                continue
            return item
        return None

    def one(self, group):
        ids = self.plan[group]
        for _ in ids:
            cursor = self.cursors.get(group, 0)
            tid = ids[cursor % len(ids)]
            self.cursors[group] = cursor + 1
            use_game = self.task_counts[tid] % 5 == 4
            lane = 'frequent' if (self.game_counts[tid] + self.lane_phases[tid]) % 2 == 0 else 'rare'
            item = self.take(tid, True, lane) if use_game else None
            if item is None and use_game:
                item = self.take(tid, True, 'rare' if lane == 'frequent' else 'frequent')
            if item is None:
                item = self.take(tid, False)
            if item is None:
                continue
            self.seen.add(item['id'])
            if item.get('source_kind') in ('tuyoura_game', 'kiricompass_sfen'):
                self.game_seen.add(item['id'])
                self.game_counts[tid] += 1
            self.task_counts[tid] += 1
            return dict(item, task=tid, group=group, black_cp=self.by_id[tid]['black_cp'])
        return None


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan', type=Path, required=True)
    ap.add_argument('--frequency', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    result = augment(read(args.plan), args.frequency, args.output)
    print(json.dumps([{'id': t['id'], **t['game_candidate_stats']} for t in result['tasks']], indent=2))
