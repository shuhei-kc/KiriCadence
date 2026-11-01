"""Fractional depth bonus, unchanged USI value and deep2, db and real YBB."""
import json, os, random, struct, sys, tempfile, unittest
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
import cshogi
import config as config_mod
import yaneuraou_book_lib as ybl
from book_repo import BookRepository, board_sfen_main
from flip import flipped_move, flipped_sfen_main
from selector import make_tie_breaker
from test_integration import WrapperProc


def board(moves=''):
    b=cshogi.Board()
    for move in moves.split():b.push_usi(move)
    return b


def moves(*rows):
    return [ybl.BookMove(move,'none',value,depth,1) for move,value,depth in rows]


class DepthBonusTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def save(self,book,kind):
        folder=self.root/kind;folder.mkdir(exist_ok=True)
        path=folder/f'000_x_test.{kind}'
        if kind=='ybb':ybl.write_ybb_book(book,path)
        else:ybl.write_yaneuraou_db_book(book,str(path))
        repo=BookRepository(log=lambda *a:None);repo.refresh(str(folder))
        return repo,path

    def probe(self,repo,b=None,a=1,min_depth=0):
        hit,_=repo.probe(b or board(),min_depth,make_tie_breaker('deep2'),random.Random(0),depth_bonus=a)
        return hit

    def test_negative_changes_selection_without_changing_value_or_file(self):
        book={board().sfen():moves(('2g2f',-100,10),('7g7f',-110,40))}
        for kind in ('db','ybb'):
            with self.subTest(kind=kind):
                repo,path=self.save(book,kind);before=path.read_bytes()
                self.assertEqual(self.probe(repo,a=0).usi,'2g2f')
                hit=self.probe(repo)
                self.assertEqual((hit.usi,hit.value,hit.depth),('7g7f',-110,40))
                self.assertEqual(before,path.read_bytes())

    def test_adjusted_tie_still_uses_deep2_not_first_difference(self):
        book={board().sfen():moves(('2g2f',-100,10),('7g7f',-110,20)),
              board('2g2f').sfen():moves(('8c8d',100,1),('3c3d',90,1),('4c4d',80,1)),
              board('7g7f').sfen():moves(('8c8d',101,1),('3c3d',89,1),('4c4d',79,1))}
        for kind in ('db','ybb'):
            with self.subTest(kind=kind):
                repo,_=self.save(book,kind);hit=self.probe(repo)
                self.assertEqual((hit.usi,hit.value,hit.depth),('7g7f',-110,20))
                self.assertEqual((hit.tie_candidates,hit.decided_at,hit.tie_reason),(2,3,'run'))

    def test_half_bonus_retains_fraction_in_selection(self):
        book={board().sfen():moves(('2g2f',-100,2),('7g7f',-101,5))}
        for kind in ('db','ybb'):
            with self.subTest(kind=kind):
                repo,path=self.save(book,kind);before=path.read_bytes()
                self.assertEqual(self.probe(repo,a=0).usi,'2g2f')
                hit=self.probe(repo,a=.5)
                self.assertEqual((hit.usi,hit.value,hit.depth),('7g7f',-101,5))
                self.assertEqual(path.read_bytes(),before)

    def test_decimal_ties_still_use_deep2(self):
        # With binary floats -1 + .1*1 and -2 + .1*11 differ slightly.
        # They must remain tied, then deep2 selects the first move at reply 3.
        book={board().sfen():moves(('2g2f',-1,1),('7g7f',-2,11)),
              board('2g2f').sfen():moves(('8c8d',101,1),('3c3d',89,1),('4c4d',79,1)),
              board('7g7f').sfen():moves(('8c8d',100,1),('3c3d',90,1),('4c4d',80,1))}
        for kind in ('db','ybb'):
            with self.subTest(kind=kind):
                repo,_=self.save(book,kind);hit=self.probe(repo,a=.1)
                self.assertEqual((hit.usi,hit.value),('2g2f',-1))
                self.assertEqual((hit.tie_candidates,hit.decided_at,hit.tie_reason),(2,3,'run'))

    def test_nonnegative_position_does_not_promote_negative_high_depth(self):
        for value in (0,10):
            for kind in ('db','ybb'):
                with self.subTest(value=value,kind=kind):
                    repo,_=self.save({board().sfen():moves(('2g2f',value,0),('7g7f',-1,60000))},kind)
                    self.assertEqual(self.probe(repo).usi,'2g2f')

    def test_deep2_reply_profiles_use_bonus_only_at_negative_reply_positions(self):
        book={board().sfen():moves(('2g2f',50,5),('7g7f',50,5)),
              board('2g2f').sfen():moves(('8c8d',-100,0),('3c3d',-110,0),('4c4d',-120,0)),
              board('7g7f').sfen():moves(('8c8d',-101,0),('3c3d',-111,100),('4c4d',-121,100))}
        for kind in ('db','ybb'):
            repo,_=self.save(book,kind)
            self.assertEqual(self.probe(repo,a=0).usi,'7g7f')
            hit=self.probe(repo,a=1)
            self.assertEqual((hit.usi,hit.value),('2g2f',50))

    def test_white_and_flipped_ybb_keep_side_to_move_value_and_uint16_depth(self):
        actual=board('7g7f');reverse=flipped_sfen_main(board_sfen_main(actual))+' 2'
        for kind in ('db','ybb'):
            repo,_=self.save({reverse:moves((flipped_move('3c3d'),-100,300),
                                           (flipped_move('8c8d'),-110,60000))},kind)
            hit=self.probe(repo,actual)
            self.assertTrue(hit.flipped)
            self.assertEqual((hit.usi,hit.value,hit.depth),('8c8d',-110,60000))

    def test_ybb_without_depth_defaults_zero_and_min_depth_gate_stays(self):
        path=self.root/'000_x_nodepth.ybb';b=board();entries=[('2g2f',-100),('7g7f',-110)]
        header=struct.Struct('<16sQQ');index=struct.Struct('<32sQHH')
        path.write_bytes(header.pack(b'YANE-BINBOOK-V1\0',1,0)+index.pack(ybl.pack_sfen(b),0,1,2)+
                         b''.join(struct.pack('<Hh',ybl.usi_to_move16(b,m),v) for m,v in entries))
        repo=BookRepository(log=lambda *a:None);repo.refresh(str(self.root))
        hit=self.probe(repo);self.assertEqual((hit.usi,hit.depth),('2g2f',0))
        self.assertIsNone(self.probe(repo,min_depth=1))

    def test_config_bonus_reload_and_invalid_value(self):
        path=self.root/'config.txt';path.write_text('BookDepthBonus 1\nTieBreak deep2\n')
        cfg,_=config_mod.load_config(str(path));self.assertEqual(cfg.book_depth_bonus,1)
        path.write_text('BookDepthBonus 0\n');cfg,_=config_mod.reload_book_settings(cfg)
        self.assertEqual(cfg.book_depth_bonus,0)
        path.write_text('BookDepthBonus -1\n');same,warnings=config_mod.reload_book_settings(cfg)
        self.assertEqual(same.book_depth_bonus,0);self.assertTrue(warnings)
        with self.assertRaises(config_mod.ConfigError):config_mod.load_config(str(path))

    def test_decimal_config_load_reload_and_nonfinite_rejection(self):
        path=self.root/'config.txt';path.write_text('BookDepthBonus 0.5\nTieBreak deep2\n')
        cfg,_=config_mod.load_config(str(path));self.assertEqual(cfg.book_depth_bonus,.5)
        path.write_text('BookDepthBonus 0.25\n');cfg,_=config_mod.reload_book_settings(cfg)
        self.assertEqual(cfg.book_depth_bonus,.25)
        for raw in ('-0.5','nan','inf','-inf','1e309','oops'):
            with self.subTest(raw=raw):
                path.write_text('BookDepthBonus '+raw+'\n')
                same,warnings=config_mod.reload_book_settings(cfg)
                self.assertEqual(same.book_depth_bonus,.25);self.assertTrue(warnings)
                with self.assertRaises(config_mod.ConfigError):config_mod.load_config(str(path))

    def test_usi_ybb_keeps_raw_output_value_and_deep2(self):
        for bonus,depth_a,depth_b in ((1,10,20),(.5,11,31)):
            with self.subTest(bonus=bonus):
                repo,path=self.save({board().sfen():moves(('2g2f',-100,depth_a),('7g7f',-110,depth_b)),
                      board('2g2f').sfen():moves(('8c8d',100,1),('3c3d',90,1),('4c4d',80,1)),
                      board('7g7f').sfen():moves(('8c8d',101,1),('3c3d',89,1),('4c4d',79,1))},'ybb')
                cfg=self.root/'config.txt';cfg.write_text(f'BookDir {path.parent}\nLogDir {self.root/"logs"}\nBookMinDepth 0\nBookDepthBonus {bonus}\nTieBreak deep2\n')
                proc=WrapperProc(str(cfg))
                try:
                    proc.send('usi');proc.read_until('usiok');proc.send('isready');proc.read_until('readyok')
                    proc.send('usinewgame');proc.send('position startpos');proc.send('go btime 1000 wtime 1000')
                    best,lines=proc.read_until('bestmove')
                    self.assertEqual(best,'bestmove 7g7f')
                    self.assertTrue(any(f'score cp -110 depth {depth_b}' in line for line in lines),lines)
                finally:
                    proc.close();proc.proc.wait(timeout=5)
                    for stream in (proc.proc.stdin,proc.proc.stdout,proc.proc.stderr):stream.close()


if __name__=='__main__':unittest.main()
