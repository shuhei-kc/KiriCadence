"""Live book exits: episode boundaries, nonblocking USI, durable GUI handoff."""
import hashlib,json,os,sys,tempfile,threading,time,unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE))
import cshogi
import bookminer_live as live
import config as cfg
from test_units import make_wrapper,StubEngine

def position(moves):
    board=cshogi.Board()
    for move in moves.split():board.push_usi(move)
    return 'position startpos'+(' moves '+moves if moves else ''),board.sfen()

class LiveExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.folder=Path(self.tmp.name)
        path=self.folder/'config.txt';path.write_text('BookMinerLive 1\n',encoding='utf-8');self.config=cfg.load_config(str(path))[0]
        self.events=[];self.live=live.LiveBookMiner(self.config,runner=lambda e:self.events.append(e) or {'state':'accepted'})
    def observe(self,state,moves):
        command,sfen=position(moves);self.live.observe(state,command,sfen,'test-game')
    def test_outside_stretch_only_once_reentry_can_add_again_no_count_limit(self):
        self.observe('hit','');self.observe('miss','7g7f');self.observe('miss','7g7f 3c3d')
        self.observe('hit','7g7f 3c3d');self.observe('miss','7g7f 3c3d 2g2f');self.live.queue.join()
        self.assertEqual(len(self.events),2)
        board=cshogi.Board()
        for _ in range(10):
            board.push(next(iter(board.legal_moves)));self.live.observe('hit',*position(''),'test-game')
            self.live.observe('miss','position sfen '+board.sfen(),board.sfen(),'test-game')
        self.live.queue.join();self.assertGreater(len(self.events),3)
    def test_same_position_not_duplicated_within_game(self):
        self.observe('miss','7g7f');self.observe('hit','');self.observe('miss','7g7f');self.live.queue.join()
        self.assertEqual(len(self.events),1)
    def test_newgame_allows_receipt_check_with_same_durable_id(self):
        self.observe('miss','7g7f');self.live.newgame();self.observe('miss','7g7f');self.live.queue.join()
        self.assertEqual(len(self.events),2);self.assertEqual(self.events[0]['id'],self.events[1]['id'])
    def test_slow_sender_does_not_block_caller(self):
        entered=threading.Event();release=threading.Event()
        def slow(e):entered.set();release.wait(3);return {'state':'accepted'}
        self.live.runner=slow
        start=time.monotonic();self.observe('miss','7g7f')
        self.assertLess(time.monotonic()-start,.2);self.assertTrue(entered.wait(1))
        release.set();self.live.queue.join()
    def test_sender_failure_stays_off_usi_thread(self):
        self.live.runner=lambda e:(_ for _ in ()).throw(RuntimeError('offline'))
        self.observe('miss','7g7f');self.live.queue.join()
        self.assertTrue(self.live.thread.is_alive())
    def test_disabled_and_skipped_do_not_send(self):
        self.observe('skip','7g7f');self.live.configure(replace(self.config,bookminer_live=False));self.observe('miss','7g7f')
        self.assertFalse(self.events);self.assertIsNone(self.live.thread)
    def test_settings_reload_and_invalid_value_preserves_previous(self):
        path=Path(self.config.config_path);path.write_text('BookMinerLive 0\nBookMinerWork other/runtime/gui\n')
        new,warnings=cfg.reload_book_settings(self.config);self.assertFalse(new.bookminer_live)
        path.write_text('BookMinerLive nonsense\n');again,warnings=cfg.reload_book_settings(new)
        self.assertFalse(again.bookminer_live);self.assertTrue(warnings)

class WrapperExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.wrapper=make_wrapper(self.tmp.name,StubEngine());self.events=[]
        self.wrapper.live_bookminer=live.LiveBookMiner(replace(self.wrapper.config,bookminer_live=True),runner=lambda e:self.events.append(e) or {'state':'accepted'})
        self.wrapper.game_started=True
        self.wrapper._live_game_active=True
    def set_position(self,moves):
        command,_=position(moves);self.wrapper._handle_gui_command('position',command.split(),command)
    def go(self,text):self.wrapper._handle_go(text.split(),text)
    def test_actual_miss_sends_but_analysis_and_speculative_ponder_do_not(self):
        # Real repository probe miss, including a configured file that has no
        # entry at this board. No actual BookMiner process is contacted.
        from test_units import write_db,board_sfen_main
        book=Path(self.tmp.name)/'book/000_x_test.db'
        write_db(str(book),{board_sfen_main(cshogi.Board()):['7g7f none 30 10']})
        self.wrapper.repo.refresh(str(book.parent));self.set_position('2g2f')
        self.go('go infinite');self.assertFalse(self.events)
        self.go('go ponder btime 10000 wtime 10000');self.assertFalse(self.events)
        self.wrapper._handle_ponderhit();self.wrapper.live_bookminer.queue.join()
        self.assertEqual(len(self.events),1)
        self.assertEqual(self.wrapper.engine.sent[-1],'ponderhit')
    def test_normal_book_reentry_and_repetition_guard(self):
        from test_units import write_db,board_sfen_main
        board=cshogi.Board();book=Path(self.tmp.name)/'book/000_x_test.db'
        write_db(str(book),{board_sfen_main(board):['7g7f none 30 10']})
        self.wrapper.repo.refresh(str(book.parent));self.set_position('2g2f')
        self.go('go btime 10000 wtime 10000');self.wrapper.live_bookminer.queue.join()
        self.assertEqual(len(self.events),1)
        # Reset engine search state as if its bestmove had arrived.
        self.wrapper.state=0;self.set_position('');self.go('go btime 10000 wtime 10000')
        self.wrapper.state=0;self.set_position('7g7f 3c3d');self.go('go btime 10000 wtime 10000')
        self.wrapper.live_bookminer.queue.join();self.assertEqual(len(self.events),2)
        self.wrapper.state=0;self.wrapper.live_bookminer.newgame()
        self.set_position('2h3h 8b7b 3h2h 7b8b');self.go('go btime 10000 wtime 10000')
        self.wrapper.live_bookminer.queue.join();self.assertEqual(len(self.events),2)

    def test_no_live_game_before_newgame_or_after_gameover(self):
        from test_units import write_db,board_sfen_main
        book=Path(self.tmp.name)/'book/000_x_test.db'
        write_db(str(book),{board_sfen_main(cshogi.Board()):['7g7f none 30 10']})
        self.wrapper.repo.refresh(str(book.parent));self.wrapper._live_game_active=False
        self.set_position('2g2f');self.go('go movetime 1000');self.assertFalse(self.events)
        self.wrapper.state=0
        self.wrapper._handle_gui_command('usinewgame',['usinewgame'],'usinewgame')
        self.wrapper.live_bookminer.configure(replace(self.wrapper.config,bookminer_live=True))
        self.set_position('2g2f');self.go('go btime 10000 wtime 10000');self.wrapper.live_bookminer.queue.join()
        self.assertEqual(len(self.events),1);self.wrapper.state=0
        self.wrapper._handle_gui_command('gameover',['gameover','win'],'gameover win')
        self.wrapper.live_bookminer.newgame();self.set_position('7g7f');self.go('go movetime 1000')
        self.wrapper.live_bookminer.queue.join();self.assertEqual(len(self.events),1)

class WorkerDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.folder=Path(self.tmp.name)
        self.work=self.folder/'YOSC/runtime/gui';self.work.mkdir(parents=True)
        command,sfen=position('7g7f')
        self.event={'id':'kc-live-isolated','command':command,'sfen':sfen,'game_id':'fake-game','journal_dir':str(self.folder/'journal'),'work':str(self.work)}
        tools=BASE.parent/'YOSC/tools';sys.path.insert(0,str(tools))
        import gui_control
        self.gc=gui_control;self.status={'gui_alive':True,'running':True,'closing':False,'miner_pid':123,'queue_remaining':999}
    def test_real_inbox_once_despite_backlog_and_restart_observation(self):
        calls=[]
        def transport(work,action,path,ident):
            result=self.gc.submit(work,action,path,ident);inbox=self.gc.ControlInbox(work);req=inbox.claim();rows=inbox.lines(req)
            calls.append(rows);native=work/'book/auto/test.txt';native.parent.mkdir(parents=True);native.write_text('\n'.join(rows)+'\n',encoding='utf-8')
            receipt=inbox.receipt(ident,'accepted',job_id=9,count=1,sha256=req['sha256'],path=str(native));inbox.finish(ident);return receipt
        original=self.gc.submit
        def send(work,action,path,ident):
            with patch('gui_control.submit',original):return transport(work,action,path,ident)
        with patch('gui_control.live_status',return_value=self.status),patch('gui_control.submit',side_effect=send):
            first=live.deliver(self.event);second=live.deliver(self.event)
        self.assertEqual(first['state'],'accepted');self.assertTrue(second['reused']);self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0],self.event['command']+', book_extend_ply=6, eval_limit=2000, game_ply_limit=200')
    def test_stopped_gui_no_enqueue_but_goal_hold_does_not_block_own_game(self):
        with patch('gui_control.live_status',return_value=self.status|{'running':False}),patch('gui_control.submit') as send:
            out=live.deliver(self.event)
        self.assertEqual(out['state'],'not_submitted');send.assert_not_called()
        from tuyoura_priority import position_id
        holdpath=self.work.parents[1]/'config/target-study-20261031.json';holdpath.parent.mkdir()
        b,_=__import__('gui_workflow').decode(self.event['command'])
        holdpath.write_text(json.dumps({'manual_only_holds':[{'position_id':position_id(b)}]}))
        # A previous manual_hold was never sent. Reconsider under the new own-game
        # policy using the same ID, not a newly generated duplicate request.
        journal=Path(self.event['journal_dir']);journal.mkdir(exist_ok=True)
        (journal/(self.event['id']+'.json')).write_text(json.dumps(self.event|{'state':'manual_hold'}))
        with patch('gui_control.live_status',return_value=self.status),patch('gui_control.submit',return_value={'state':'rejected'}) as send:
            out=live.deliver(self.event)
        self.assertEqual(out['state'],'rejected');send.assert_called_once()

    @unittest.skipUnless(os.name=='nt','Windows hidden Tk/owned fake worker')
    @unittest.skipUnless(
        (BASE.parent/'YOSC/tools/bookminer_gui.py').is_file()
        and (BASE.parent/'YOSC/tools/vendor/bookminer_gui.py').is_file(),
        'Optional external BookMiner GUI is not installed')
    def test_cold_subprocess_to_real_gui_once_no_restart(self):
        sys.path.insert(0,str(BASE.parent/'YOSC/tests'))
        from test_gui_control import GuiControlIntegration
        fixture=GuiControlIntegration();fixture.setUp()
        try:
            event=self.event|{'journal_dir':str(fixture.work/'live-journal'),'work':str(fixture.work)}
            results=[];errors=[];before=fixture.process.pid
            def deliver_twice():
                try:
                    results.append(live.LiveBookMiner._run(event));results.append(live.LiveBookMiner._run(event))
                except Exception as exc:errors.append(repr(exc))
            thread=threading.Thread(target=deliver_twice);thread.start()
            fixture.pump(lambda:len(results)==2 or bool(errors),timeout=12);thread.join(1)
            self.assertFalse(errors,errors);self.assertEqual(results[0]['receipt']['job_id'],9)
            self.assertTrue(results[1]['reused']);self.assertEqual((fixture.work/'e-count.txt').read_text(),'1')
            self.assertEqual(fixture.process.pid,before)
        finally:fixture.tearDown()

if __name__=='__main__':unittest.main()
