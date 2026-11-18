"""Actual a-selected raw thresholds, DB/YBB/flip, and child-only delivery."""
import json,random,sys,tempfile,unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE))
import cshogi
import yaneuraou_book_lib as ybl
from book_repo import BookRepository,board_sfen_main
from bookminer_live import LiveBookMiner,deliver
from selector import make_tie_breaker
from flip import flipped_sfen_main,flipped_move
from test_units import make_wrapper,write_db
from test_bookminer_live import WorkerDeliveryTests,position

class Depth0LiveTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.wrapper=make_wrapper(str(self.root));self.events=[]
  self.wrapper.config=replace(self.wrapper.config,bookminer_live=True,book_min_depth=0,book_depth_bonus=.5)
  self.wrapper.live_bookminer=LiveBookMiner(self.wrapper.config,runner=lambda e:self.events.append(e) or {'state':'accepted'})
  self.wrapper.game_started=True;self.wrapper._live_game_active=True
 def save(self,rows,kind='db',moves='',flip=False):
  b=cshogi.Board()
  for m in moves.split():b.push_usi(m)
  sf=flipped_sfen_main(board_sfen_main(b)) if flip else b.sfen()
  data={sf:[ybl.BookMove(flipped_move(m) if flip else m,'none',v,d,1) for m,v,d in rows]}
  folder=self.root/'book';folder.mkdir(exist_ok=True);path=folder/f'000_x_test.{kind}'
  if kind=='db':ybl.write_yaneuraou_db_book(data,str(path))
  else:ybl.write_ybb_book(data,path)
  self.wrapper.repo.refresh(str(folder))
 def go(self,moves='',text='go btime 10000 wtime 10000'):
  command,_=position(moves);self.wrapper._handle_gui_command('position',command.split(),command)
  self.wrapper._handle_go(text.split(),text);self.wrapper.live_bookminer.queue.join()
 def test_a_chosen_raw_value_inclusive_threshold_db_and_ybb(self):
  rows=[('2g2f',-200,400),('7g7f',-50,0),('1g1f',-210,0),('9g9f',-230,0),('3g3f',-231,0),('4g4f',-300,1)]
  for kind in ('db','ybb'):
   with self.subTest(kind=kind):
    # One file per subcase so priority cannot mask a format regression.
    for p in (self.root/'book').glob('*') if (self.root/'book').exists() else ():p.unlink()
    self.events.clear();self.wrapper.live_bookminer.newgame();self.save(rows,kind);self.go()
    self.assertEqual(self.wrapper._book_probe_hit.usi,'2g2f')
    self.assertEqual([e['candidate_move'] for e in self.events],['7g7f','1g1f','9g9f'])
    self.assertTrue(all(e['selected_value']==-200 and e['threshold']==-230 for e in self.events))
    for e in self.events:
     self.assertTrue(e['command'].startswith('position sfen '))
     self.assertNotIn(' moves ',e['command'])
     b=cshogi.Board(e['sfen']);self.assertEqual(b.turn,cshogi.WHITE)
     self.assertEqual(e['source_command'],'position startpos moves '+e['candidate_move'])
 def test_three_per_position_and_each_new_position_no_game_cap(self):
  rows=[('2g2f',100,0),('7g7f',99,0),('1g1f',98,0),('9g9f',97,0)]
  self.save(rows);self.go();self.assertEqual(len(self.events),3)
  self.go();self.assertEqual(len(self.events),3)
  self.save([('8c8d',100,0),('3c3d',90,0),('9c9d',80,0)],moves='2g2f')
  self.go('2g2f');self.assertEqual(len(self.events),6)
  self.assertTrue(all(e['trigger']=='depth0' for e in self.events))
 def test_flip_candidates_are_real_moves_with_side_to_move_raw_value(self):
  self.save([('8c8d',100,8),('3c3d',70,0),('9c9d',69,0)],kind='ybb',moves='2g2f',flip=True)
  self.go('2g2f');self.assertTrue(self.wrapper._book_probe_hit.flipped)
  self.assertEqual([e['candidate_move'] for e in self.events],['3c3d'])
  self.assertEqual(self.events[0]['source_command'],'position startpos moves 2g2f 3c3d')
  self.assertEqual(cshogi.Board(self.events[0]['sfen']).turn,cshogi.BLACK)
 def test_analysis_speculative_ponder_and_gameover_do_not_enqueue(self):
  self.save([('2g2f',100,0)])
  self.go(text='go infinite');self.go(text='go ponder btime 10000 wtime 10000');self.assertFalse(self.events)
  self.wrapper._handle_ponderhit();self.wrapper.live_bookminer.queue.join();self.assertEqual(len(self.events),1)
  self.wrapper._handle_gui_command('gameover',['gameover','win'],'gameover win')
  self.wrapper.live_bookminer.newgame();self.go();self.assertEqual(len(self.events),1)
 def test_illegal_terminal_and_nonzero_depth_are_not_research_candidates(self):
  self.save([('2g2f',100,10),('7g7f',95,1),('1g1f',70,0),('9g9f',69,0),('2a2b',100,0),('none',100,0)])
  self.go();self.assertEqual([e['candidate_move'] for e in self.events],['1g1f'])

class Depth0DeliveryTests(WorkerDeliveryTests):
 # Exercise the existing durable inbox with the newly permitted SFEN input.
 def setUp(self):
  super().setUp();command,sfen=position('7g7f 3c3d')
  self.event.update(command='position sfen '+sfen,sfen=sfen,source_command=command,trigger='depth0')
 def test_parent_goal_hold_does_not_block_own_game_depth0(self):
  from gui_workflow import decode
  from tuyoura_priority import position_id
  hold=self.work.parents[1]/'config/target-study-20261031.json';hold.parent.mkdir()
  parent=decode('position startpos moves 7g7f')[0]
  hold.write_text(json.dumps({'manual_only_holds':[{'position_id':position_id(parent)}]}))
  with patch('gui_control.live_status',return_value=self.status),patch('gui_control.submit',return_value={'state':'rejected'}) as send:
   result=deliver(self.event)
  self.assertEqual(result['state'],'rejected');send.assert_called_once()

if __name__=='__main__':unittest.main()
