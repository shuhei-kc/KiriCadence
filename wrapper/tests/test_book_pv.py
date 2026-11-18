"""Book PV uses real per-ply selection without changing live game state."""
import json,random,sys,tempfile,unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE))
import cshogi
import config as cfg
import yaneuraou_book_lib as ybl
from book_repo import board_sfen_main
from test_units import make_wrapper,write_db
from bookminer_live import LiveBookMiner

def board(moves=''):
 b=cshogi.Board()
 for m in moves.split():b.push_usi(m)
 return b

class BookPVTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.w=make_wrapper(str(self.root));self.w.config=replace(self.w.config,book_min_depth=0)
  self.w._track_position(['position','startpos']);self.w.last_position_cmd='position startpos'
 def save(self,data,name='000_x_test.db'):
  path=self.root/'book'/name
  if path.suffix=='.ybb':
   ybl.write_ybb_book({board(k).sfen():[ybl.BookMove(m,'none',v,d,1) for m,v,d in rows] for k,rows in data.items()},path)
  else:write_db(str(path),{board_sfen_main(board(k)):[f'{m} none {v} {d}' for m,v,d in rows] for k,rows in data.items()})
  self.w.repo.refresh(str(path.parent))
 def pv(self):
  lines,move=self.w._book_decision()
  tokens=lines[0].split();return move,tokens[tokens.index('pv')+1:tokens.index('string')],lines[0]
 def test_default_eight_and_configurable_length(self):
  moves='2g2f 8c8d 2f2e 8d8e 7g7f 4a3b 8h7g 3c3d 7i6h 2b7g+'.split()
  self.save({' '.join(moves[:i]):[(m,100,5)] for i,m in enumerate(moves)})
  self.assertEqual(self.pv()[1],moves[:8])
  self.w.config=replace(self.w.config,book_pv_max_ply=3);self.assertEqual(self.pv()[1],moves[:3])
  self.w.config=replace(self.w.config,book_pv_max_ply=1)
  with patch.object(self.w.repo,'probe',wraps=self.w.repo.probe) as probe:
   self.assertEqual(self.pv()[1],moves[:1]);self.assertEqual(probe.call_count,1)
 def test_per_ply_priority_switches_files_both_sides(self):
  # Current b book, then a w book outranks the initially used x book;
  # on returning to b a still higher priority b book becomes applicable.
  self.save({'':[('2g2f',10,8)],'2g2f':[('3c3d',999,8)],'2g2f 8c8d':[('7g7f',999,8)]},'478_x_peta.db')
  self.save({'2g2f':[('8c8d',0,478)]},'010_w_absolute.db')
  self.save({'2g2f 8c8d':[('2f2e',0,478)]},'005_b_absolute.db')
  self.assertEqual(self.pv()[1],['2g2f','8c8d','2f2e'])
 def test_a_and_deep2_each_ply_db_and_ybb_no_live_side_effects(self):
  data={'':[('2g2f',-100,10),('7g7f',-110,40)],
        '7g7f':[('8c8d',-20,0),('3c3d',-30,40)],
        '7g7f 3c3d':[('2g2f',100,3),('6g6f',100,3)],
        '7g7f 3c3d 2g2f':[('8c8d',100,1),('4c4d',90,1),('5c5d',80,1)],
        '7g7f 3c3d 6g6f':[('8c8d',101,1),('4c4d',89,1),('5c5d',79,1)]}
  for kind in ('db','ybb'):
   with self.subTest(kind=kind):
    for path in (self.root/'book').glob('*'):path.unlink()
    self.save(data,'000_x_test.'+kind);self.w.config=replace(self.w.config,book_depth_bonus=.5,bookminer_live=True)
    events=[];self.w.live_bookminer=LiveBookMiner(self.w.config,runner=lambda e:events.append(e) or {'state':'accepted'})
    self.w._live_game_active=True;before=self.w.board.sfen();counts=self.w.position_counts.copy();rng=self.w.rng.getstate()
    self.w._handle_go(['go','btime','10000','wtime','10000'],'go btime 10000 wtime 10000')
    self.w.live_bookminer.queue.join();line=next(x for x in reversed(self.w.gui_lines) if 'Book_Hit' in x)
    tokens=line.split();self.assertEqual(tokens[tokens.index('pv')+1:tokens.index('string')],['7g7f','3c3d','6g6f','8c8d'])
    self.assertIn('score cp -110 depth 40',line);self.assertEqual(self.w.gui_lines[-1],'bestmove 7g7f')
    self.assertEqual(self.w.board.sfen(),before);self.assertEqual(self.w.position_counts,counts);self.assertEqual(self.w.rng.getstate(),rng)
    self.assertEqual(self.w._book_probe_hit.usi,'7g7f');self.assertFalse(events)
    records=[json.loads(x) for x in (self.root/'logs/decisions.jsonl').read_text(encoding='utf-8').splitlines()]
    self.assertEqual(records[-1]['move'],'7g7f')
 def test_repetition_guard_uses_past_history_and_stops_loops(self):
  self.save({'':[('2g2f',10,8)],'2g2f':[('8c8d',10,8)],'2g2f 8c8d':[('7g7f',10,8)]})
  self.w.position_counts[board_sfen_main(board('2g2f 8c8d'))]=1
  self.assertEqual(self.pv()[1],['2g2f','8c8d'])
  for path in (self.root/'book').glob('*'):path.unlink()
  moves='2h3h 8b7b 3h2h 7b8b'.split();self.w.position_counts.clear()
  self.w.position_counts[board_sfen_main(board())]=1
  self.save({' '.join(moves[:i]):[(m,10,8)] for i,m in enumerate(moves)})
  self.assertEqual(self.pv()[1],moves)
 def test_depth_gate_stops_and_continuation_failure_keeps_bestmove(self):
  self.save({'':[('2g2f',10,8)],'2g2f':[('8c8d',10,0)]})
  self.w.config=replace(self.w.config,book_min_depth=1);self.assertEqual(self.pv()[1],['2g2f'])
  original=self.w.repo.probe;calls=[]
  def probe(*a,**kw):
   calls.append(1)
   if len(calls)>1:raise OSError('display lookup failed')
   return original(*a,**kw)
  with patch.object(self.w.repo,'probe',side_effect=probe):
   self.assertEqual(self.pv()[:2],('2g2f',['2g2f']))
   self.assertEqual(self.w._book_probe_state,'hit')
 def test_time_budget_stops_before_extra_lookup(self):
  self.save({'':[('2g2f',10,8)],'2g2f':[('8c8d',10,8)]})
  with patch('main.time.perf_counter',side_effect=[0,1]):self.assertEqual(self.pv()[1],['2g2f'])

class BookPVConfigTests(unittest.TestCase):
 def test_defaults_load_and_reload(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'config.txt';path.write_text('')
   config,_=cfg.load_config(str(path));self.assertEqual(config.book_pv_max_ply,8)
   path.write_text('BookPVMaxPly 16\n');config,_=cfg.reload_book_settings(config);self.assertEqual(config.book_pv_max_ply,16)
   for bad in ('0','-1','1.5','abc'):
    path.write_text('BookPVMaxPly '+bad+'\n')
    same,warnings=cfg.reload_book_settings(config);self.assertEqual(same.book_pv_max_ply,16);self.assertTrue(warnings)
    with self.assertRaises(cfg.ConfigError):cfg.load_config(str(path))
   path.write_text('BookPVMaxPly 1\n');config,_=cfg.reload_book_settings(config);self.assertEqual(config.book_pv_max_ply,1)

if __name__=='__main__':unittest.main()
