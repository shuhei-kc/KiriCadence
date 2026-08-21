# KiriCadence

floodgate に放流しながら、自動で定跡を修正し続けるシステム。
2つの部品からなる。

| 部品 | 役割 |
|---|---|
| **定跡USIラッパー** (`wrapper/`) | 将棋GUI から見ると1つのエンジン。優先度順に定跡を引き、無ければ本体エンジンに渡す。対局中に自分の評価値が決着水準に達したら、その棋譜を修正タスクとして書き出す |
| **KiriCondencer** (`makebook/`) | 書き出されたタスクを1件ずつ処理し、定跡を修正し続ける常駐プロセス |

設計は [DESIGN.md](DESIGN.md)。

## KiriCondencer — 定跡の自動修正

ラッパーが「負けた」と書き出した棋譜から**課題局面**を1つ決め、
そこを起点に定跡を直す。これを1サイクルとして、タスクが尽きるまで回し続ける。

1. **課題局面を決める** — 棋譜を終局側から遡り、対象定跡より優先度の高い
   定跡だけを見て「採用手が登録されている最後の局面」を探す。そこで
   **実際に指された手**を適用した後が課題局面。定跡を外された変化こそ
   調べたいので、定跡の採用手ではなく実戦の手を使う。
2. **前例を取り込む** — 課題局面を含む対局を棋譜DBから新しい順に取得し、
   評価値が決着水準に達するまで検討する。**勝ち側の手を登録し、負け側の手は
   分岐点まで遡って削除する** (実際の対局結果は見ない)。一度使った対局は
   記録して次から飛ばすので、呼ぶたび自然に古い方へ進む。
3. **自己対局する** — 課題局面から連続対局し、どちらかが規定連勝するまで続ける。
   学習規則は前例取り込みと共通。
4. **書き出す** — ラッパーが対局中に読んでいても衝突しない形で置き換える。

```bash
py -3.12 makebook\condenser.py
```

常駐して `s` (状態) / `t` (課題局面を追加) / `q` (終了) などのコマンドを受け付ける。
詳細と設定は [makebook/README.md](makebook/README.md)。

## セットアップ

リポジトリにはソースだけが入っている。**定跡・エンジン・棋譜DB・運用データは
含まれない**ので、各自で用意する。

```bash
copy config-sample.txt config.txt
copy makebook\config-sample.txt makebook\config.txt
```

複製した `config.txt` の `EnginePath` / `BookDir` / `CsaDbPath` を自分の環境に
合わせる。あとは以下を置けば動く。

| 置くもの | 場所 | 備考 |
|---|---|---|
| USI エンジン | `engine/` | やねうら王系の実行ファイルと評価関数一式。`EnginePath` で指す |
| 定跡ファイル | `book/` | 命名規約は下記。同梱していない |
| 棋譜DB (`csa.db`) | 任意 | `CsaDbPath` で指す。定跡修正機能を使う場合のみ |

依存は **Python 3.10 以上** + cshogi + numpy。
同梱の `.bat` は `py -3.12` を呼ぶので、別のバージョンを使うなら
そこを書き換える。

## 使い方

将棋GUI / floodgate クライアントにはエンジンとして `run.bat` を登録する
(内部で `py -3.12 wrapper\main.py` を起動)。

```
KiriCadence/
  config.txt   設定 (やねうら王 Option 系の「名前 値」形式)
  run.bat      エンジン登録用
  book/        優先定跡 (下記の命名規約)
  engine/      USIエンジン (実行ファイルと評価関数)
  logs/        wrapper.log / decisions.jsonl
  wrapper/     ラッパー本体
  makebook/    KiriCondencer (定跡の自動修正)
```

## book/ の命名規約

```
<3桁優先度>_<採用手番(b/w/x)>_<名前>.<db|ybb>
例: 000_b_absolute.db  100_x_book.db  900_b_peta1501plus.db
```

- 数値が小さいほど高優先。優先度の高い順に照会し、採用手が無ければ次へ。
- `b`=先手番の局面のみ / `w`=後手番のみ / `x`=両方。
  同じ優先度では b/w を x より先に見る。
- **.db は SFEN 文字列順ソート必須** (二分探索の前提)。非ソートを実行時に
  検出したファイルは次の usinewgame までスキップされ、ログに残る。
  `replay.py --check-book` で事前検証できる。
- **手書き定跡は depth 列 (4列目) を忘れずに**。省略すると depth 0 となり
  既定の `BookMinDepth 1` では一切採用されない。
- どのファイルも直接検索→先後反転 (flip) 検索の順で必ず両方引く。
- 対局の合間 (usinewgame) にファイルの追加・削除・差し替えが反映される。
  照会の都度 open/close するため、対局中でも外部からの差し替えは安全。

## 主な仕様

- depth < `BookMinDepth` (既定1) の最善手は指さず、局面ごと次のファイル→
  エンジンへ渡す (ペタショック定跡の未伝播の葉を弾く)。
- value 同値は `TieBreak deep` (既定): 相手の応手列を突き合わせ、
  相手の有力な選択肢が少ない側を選ぶ。`random` / `first` も選択可。
- **千日手**: 先手番で同一局面2回目以降は定跡をフル無視してエンジンに
  打開させる。後手番は定跡を使い切る (千日手歓迎)。
- 非合法手・終端手 (none/resign/win/null) は採用前に除外。
- 判定は毎go行うため、定跡を外れても合流すれば復帰する。
- 全判定を `logs/decisions.jsonl` (game_id付きJSONL) に記録
  → 将来の自動定跡修正システムの入力。

## 検証・テスト

```bash
py -3.12 wrapper\replay.py "startpos moves 7g7f 3c3d"
```

```bash
py -3.12 wrapper\replay.py --check-book
```

```bash
py -3.12 -m unittest discover -s wrapper\tests
```

依存: Python 3.10 以上 + cshogi + numpy。定跡読み書きは
`wrapper/yaneuraou_book_lib.py` (YaneuraOu-ScriptCollection CommonLib の
vendor コピー、[wrapper/VENDOR.md](wrapper/VENDOR.md) 参照) を使用。

## ライセンス

MIT License ([LICENSE](LICENSE))。ただし `wrapper/yaneuraou_book_lib.py` は
YaneuraOu-ScriptCollection からの vendor コピーで、こちらも MIT
(Copyright (c) 2025 yaneurao) —
[wrapper/LICENSE.YaneuraOu-ScriptCollection.txt](wrapper/LICENSE.YaneuraOu-ScriptCollection.txt)
を参照。

## 先行例・参考

本システムは独自の発明ではない。中心的な発想も学習規則も先行実装に依っている。

- **[HiraganaSuisho](https://github.com/tayayan/HiraganaSuisho)** (tayayan 氏, MIT)
  — 「読み込んだ定跡の悪い変化を連続対局で自動的に特定して潰す」という、
  本システムの根幹にあたる発想の先行実装。**実装にあたって同ソフトのコードを
  参照している。**
- **[YaneuraOu-ScriptCollection](https://github.com/yaneurao/YaneuraOu-ScriptCollection)**
  (yaneurao 氏, MIT) — `wrapper/yaneuraou_book_lib.py` は同リポジトリ CommonLib の
  **無改変コピー** ([wrapper/VENDOR.md](wrapper/VENDOR.md))。定跡の学習規則は
  makebook4 の「勝ち手は加算 / 負け手は分岐点まで遡って削除」をそのまま採っており、
  KiriCondencer のプロセス駆動方式 (子プロセスとして起動し stdin でコマンドを送る) は
  BookMiner に倣っている。
