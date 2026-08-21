# KiriCadence 定跡修正システム 基盤設計 v0.2

2026-08-19 / 起草 (v0.1) → 第1段階を実装して v0.2 に更新。

> **実装済みの部分は [README.md](README.md) が正**。本書は「なぜそうしたか」と
> 未着手部分 (BookMiner 連携) の設計を残す。
>
> v0.1 との差分:
> - `suisho_gen.py` (連続自己対局の常駐) → **`condenser.py`** (課題局面ごとの
>   逐次サイクル)。タスクを1件ずつ処理する形にしたので、GUI から
>   「いま何をしているか」が追いやすく、途中割り込みも起きない。
> - 課題局面は棋譜から自動導出する (定跡が切れる場所の一歩先)。
> - 前例は csa.db から取り、**エンジンで検討して勝ち側の手だけ**を登録する
>   (実際の勝敗は見ない)。取得済み対局は `state.db` で管理。
> - floodgate 対局は、終局を待たず評価値が決着水準に達した時点で
>   ラッパーがタスクを書き出す (`wrapper/task_emitter.py`)。

実装前のベース技術設計。細部は実装時に決めてよいが、
「**変えにくいところ**」(§2 の共有基盤・§3 のプロセス間規約) だけは
この文書に従うこと。ラッパー本体の設計は `../DESIGN.md` (v1.3) が正。

## 0. 全体像

```
                    ┌────────────────────────────┐
   floodgate ←──────┤ ラッパー (実装済み)          │ 4T
                    │  book/ 参照・decisions.jsonl │
                    └──────┬─────────────────────┘
                           │ decisions.jsonl (どこで定跡が切れたか)
                           ▼
  ┌────────────────────────────────────────────────┐
  │ 定跡修正システム (本書の範囲)                     │
  │                                                │
  │  A. suisho_gen  水匠定跡生成の再実装    4T×1並列 │
  │     自己対局学習 + 棋譜取り込み + 課題局面逐次変更 │
  │  B. miner_bridge  BookMiner 運転手     1T×4並列 │
  │     特定戦型のみ enqueue → 定期 w/p → book/へ    │
  └──────┬─────────────────────────┬───────────────┘
         │ 学習定跡 .db             │ peta化 .db
         ▼                         ▼
       KiriCadence/book/  (優先度付きで配置、ラッパーが毎goで参照)
         ▲
         │ 前例・自分の対局棋譜 (読み取り専用)
       KiriCompass csa.db  (既存。そのままの場所で使う)
```

リソース配分の想定 (config で変更可能にする):
放流ラッパー 4T / suisho_gen 4T×1並列 / BookMiner 1T×4並列。
ほかに KiriCompass の db 更新・MATERIAL・対局監視が同居する前提なので、
**各ツールのスレッド数・並列数は必ず config で絞れるようにする**。

段階: **A を先に完成させる**。B はその後 (A の共有基盤の上に乗せる)。

## 1. 配置と依存

```
KiriCadence/
  wrapper/          実装済み。config.py / flip.py / yaneuraou_book_lib.py を
                    定跡修正側からも import して使う (vendor の二重化はしない)
  makebook/
    DESIGN.md       本書
    common.py       §2 の共有基盤 (小さく保つ)
    suisho_gen.py   A. 水匠定跡生成 (エントリポイント)
    gen_core.py     A. の中身 (自己対局・学習更新・エンジン管理)
    miner_bridge.py B. BookMiner 運転手 (後続フェーズ)
    config_gen.txt  A 用設定
    config_miner.txt B 用設定
    inbox/          実行中の指示投入 (§4.4)。done/ へアーカイブ
    out/            生成した定跡・棋譜の出力先 (book/ へは自動では置かない)
    logs/
    tests/
```

- 依存は wrapper と同じ: Python 3.10 以上 + cshogi + numpy。盤面は **cshogi のみ**
  (attic の Board.py 系は使わない)。定跡の読み書きは
  `wrapper/yaneuraou_book_lib.py` (= BookMiner と同系の実装) に一本化。
- wrapper のモジュールは `sys.path` に `../wrapper` を足して import する。
  wrapper 側を変更する必要が出たら wrapper のテストも回すこと。

## 2. 共有基盤 (makebook/common.py) — 変えにくい部分

小さいユーティリティ集。ここだけ先に固めれば、A/B の中身は
後からいくらでも書き直せる。

### 2.1 設定

wrapper と同じ **`名前 値` 形式** (wrapper/config.py の parse_option_text を
再利用)。ツールごとに独立した config ファイルを持つが、**共通キーは
名前を揃える**:

```
# 例: config_gen.txt
CsaDbPath      C:\shogi\KiriCompass\data\csa.db
EnginePath     ..\engine\suisho11plus\YaneuraOu_...exe
Threads        4
Parallel       1
BookDir        ..\book
OutDir         out
LogDir         logs
```

- `CsaDbPath` の既定は `C:\shogi\KiriCompass\data\csa.db`。
  **KiriCompass を将来 Cadence 内へ移す可能性があるため、csa.db への
  参照はすべてこのキー経由**にする (コードへの直書き禁止)。
- 相対パスは config ファイルの場所基準 (wrapper と同じ規則)。

### 2.2 csa.db リーダ (読み取り専用)

KiriCompass が 30 分ごとに更新している SQLite を **`mode=ro` URI +
busy_timeout で読むだけ**。書き込み・スキーマ変更は絶対にしない
(KiriCompass 側の更新と衝突させない)。スキーマは
`makebook/hyohga_engine/custom_engine3/DESIGN.md` §2 が正:

- `games(game_id, result, ...)`  result: 0=引分 1=先手勝 2=後手勝 NULL=中断
- `position_games(position_key, sort_key, game_id, ply, next_move)`
- `position_stats(position_key, next_move, game_count, black_wins,
  white_wins, draws)`

position_key (blake2b 8B) と move16 の計算は KiriCompass kifudb/board.py が
正だが、cshogi 側から同じキーを作る変換関数を common.py に置く
(custom_engine3 の board.py に実装があるので移植し、**KiriCompass の
実物 db と突き合わせるテストを必ず書く**)。

提供する関数 (最小):

- `game_moves(game_id) -> (開始sfen, [usi...], result)` 1局の再生用
- `find_games(player=None, since=None, limit=...)` 取り込み対象の列挙
  (自分の対局 = player 名でフィルタ)
- `position_stats(sfen_main)` 前例統計 (課題局面の選定に使う)

### 2.3 定跡ストア

学習定跡のメモリ表現は「sfen主要部 (手数なし) → {usi: 統計}」の dict。
読み書きは yaneuraou_book_lib で行い、**書き出しは必ず
tmp に書いて os.replace** (ラッパー・BookMiner と同じ規約。§3.1)。
書き出す .db は **SFEN順ソート・depth 列付き** (ラッパーの
BookMinDepth=1 を通すため。既定 depth 32 = makebook4 系と同じ)。

### 2.4 決定ログリーダ

`../logs/decisions.jsonl` を読み、「定跡が切れた局面」「prior_misses で
穴が見えた局面」を課題局面として列挙する。game_id 単位で集計できる
ようにする (スキーマは ../DESIGN.md §9.2)。

## 3. プロセス間の規約 — 変えにくい部分

同居するプロセスが多い (ラッパー・suisho_gen・BookMiner・KiriCompass 更新・
MATERIAL・監視)。守るべきことは3つだけ:

1. **単一ライタ**: 1つの .db ファイルに書くプロセスは常に1つ。
   `book/` 内の各ファイルは「誰が生成主か」を優先度番号で分ける
   (例: 000/100=手動、500=suisho_gen、800=miner、900=peta ベース)。
2. **アトミック差し替え**: 配布物 (book/ に置く .db) は同一ボリュームに
   tmp を書いてから `os.replace`。ラッパーは probe ごとに open/close する
   ので、対局中でも安全に差し替わる (実装済みの前提)。
3. **csa.db は読み取り専用** (`mode=ro`)。

`book/` への昇格 (out/ → book/) は**当面すべて手動**。自動昇格は
両ツールが安定してから検討する (事故ったとき戻すのも手動で単純)。

## 4. A. suisho_gen — 水匠定跡生成の再実装

makebook4.py の学習ルール (勝ち手カウント++ / 敗着の定跡手を削除) を核に、
実行環境を作り直す。**学習ルール自体は選手交代できる形に隔離する**
(ラッパーの TieBreaker と同じ発想。ルールは模索対象)。

### 4.1 構成

```
suisho_gen.py     CLI・設定読み・スレッド起動・inbox 監視
gen_core.py
  EngineWorker    エンジン1プロセスの管理 (wrapper/engine_proxy.py を再利用
                  できるか実装時に判断。ダメなら簡易版をここに書く)
  SelfPlayLoop    連続対局 1本 (Parallel 数だけ生成)
  LearningBook    定跡 dict + 更新ポリシー適用 + 定期書き出し (lock 1本)
  UpdatePolicy    学習ルールの差し替え点 (下記)
```

```python
class UpdatePolicy(Protocol):
    def on_game_end(self, book, game: GameRecord) -> None: ...
    # GameRecord = 開始sfen・(sfen, usi, 出所)列・結果 (win_b/win_w/draw/中断)
```

既定ポリシー = makebook4 と同じ:
勝った側の採用手 count++、負けた側の**最後の定跡手**を削除
(絶対定跡由来の手は不変)。

### 4.2 makebook4 からの仕様変更 (要件反映)

| 項目 | makebook4 | suisho_gen |
|---|---|---|
| 盤面 | 自前 Board.py | **cshogi** (合法手・千日手判定を信頼できる) |
| 起動 | input() 対話 | **config + CLI 引数** (無人運転可能) |
| 並列 | exec で Shogi() 複製 | Parallel 設定でスレッド生成 (共有 book は lock) |
| 課題局面 | 起動時固定 basesfen | **実行中に逐次変更** (§4.4 inbox) |
| 開始局面 | startpos moves のみ | **`sfen ...` も可** (LearningBook のキーは手数なし sfen なのでどこから育てても合流する) |
| 千日手 | 対話で選択 | **既定 後手勝ち** (= 先手側を敗者として更新)。config `DrawWinner white` で変更可。エンジンの DrawValueBlack/White も同じ向きに自動設定 |
| 棋譜取り込み | なし (手動 position 追加のみ) | **あり** (§4.3) |
| 保存 | 上書き write | tmp→os.replace、定期 + 終了時 |

千日手検出は cshogi の連続王手判定も含めて実装時に確認する
(連続王手の千日手は反則側を敗者にするのが自然。細部は変更容易に)。

### 4.3 棋譜取り込み

「外部で指された対局を、自己対局と同じ更新ルールで定跡に反映する」機能。
入力は2系統、どちらも最終的に GameRecord に正規化して
`UpdatePolicy.on_game_end` に流す (= 学習経路は1本):

1. **csa.db から**: `import_csa player=<自分のbot名> since=<日時>` で
   自分の floodgate 対局を取り込む (取り込み済み game_id は状態ファイルに
   記録して重複適用を防ぐ)。ラッパーの decisions.jsonl と突き合わせれば
   「定跡で指した手」に出所フラグを立てられる (初版は全手 book 扱いでも可)。
2. **ファイルから**: 1行1局の USI 形式 (`startpos moves ...` +
   末尾に結果タグ) を inbox に置く。

### 4.4 実行中の操作 (inbox 方式)

対話 stdin は残しつつ、**無人運転・将来の自動化の主経路は inbox**:
`makebook/inbox/` にテキストファイルを置くと次のループ境界で処理され、
`inbox/done/` (失敗は `inbox/error/`) へ移動する。1行1コマンド:

```
base position startpos moves 2g2f 8c8d      # 課題局面の変更 (以降の対局に適用)
base sfen <sfen> moves ...                  # sfen 起点も可
add  position startpos moves 7g7f 3c3d ...  # 定跡手の手動追加 (count 加算)
import_csa player=KiriCadence since=2026-08-19
import_file path=...
write                                       # いま書き出す
```

課題局面は「次の対局から」反映 (対局中の切り替えはしない)。
複数 base を並べたらラウンドロビン等 — 細部は実装時に。
この inbox が、将来の自動オーケストレーション (decisions.jsonl から
課題局面を自動投入する等) の接続点になる。

## 5. B. miner_bridge — BookMiner の運転手 (後続フェーズ)

BookMiner は改造しない。**GUI と同じ方式 = 子プロセスとして起動し
stdin にコマンドを送る**運転手を書く (BookMiner-gui.py が先行実装として
参考になる)。BookMiner の場所は config `BookMinerDir` で指定
(既存の C:\shogi\... をそのまま指す)。

役割:

1. **戦型フィルタ付き enqueue**: 課題局面 (decisions.jsonl の miss +
   csa.db の前例) から `startpos moves ...` 行を作るとき、config の
   `ThemeRoots` (戦型のルート手順のリスト) の**いずれかを prefix に持つ
   行だけ**を think_sfens に書き、`t <file>` を送る。
2. **定期回収**: 一定間隔で `w` → (必要なら) `p` を送り、
   `book/backup/` の最新 db を out/ へコピー・検証
   (`replay.py --check-book` 相当) して命名規約に沿ってリネーム。
   book/ への昇格は手動 (§3)。
3. BookMiner 側スレッド設定 (1T×4並列) は BookMiner の settings で行う。
   bridge は触らない。

水匠定跡生成との連携 (suisho_gen が見つけた有望手を miner に掘らせる /
miner の評価を suisho_gen の初期値に使う) は、両方が動いてから
inbox / think_sfens という既存の接続点の上で設計する。今は作らない。

## 5.5 実装で分かったこと (重要)

- **課題局面は「実際に指された手を適用した後」にする**。定跡の採用手を
  適用すると、自分が本線だと思っている変化しか掘れず、相手が外してきた
  変化を調べられない (実測で10局中4局がこの分岐に当たった)。
  副次的に、課題局面が必ず由来棋譜に現れるようになる。
- **課題局面の判定に対象定跡自身を含めてはいけない**。含めると、
  サイクルが登録した深い局面が次回のフロンティアになり、課題局面が
  自分の学習結果に引きずられて奥へ流れていく (正のフィードバック)。
  判定に使うのは対象定跡**より優先度の高い**定跡だけ。境界は
  `TargetBook` のファイル名先頭3桁から取るので、定跡の役割は
  番号を付け替えるだけで変えられる。

- **cshogi の `set_sfen` は不正な SFEN でプロセスごと落ちる**。Python の
  例外にならないので try/except では受けられない。手入力・外部データを
  渡す前に `wrapper/sfen_guard.py` の `validate_sfen()` で必ず弾くこと。
  実測: 段のマス数が 9 でない SFEN で異常終了した。
- `position_key` は `blake2b(手数なしSFEN, 8byte)` で、cshogi 由来の
  `board_sfen_main()` と KiriCompass の `sfen_key_string()` は同一文字列に
  なる。実 csa.db との一致はテストで固定済み。
- csa.db の `games.game_id` は時系列順ではない。新しい順に取るときは
  必ず `started_at` / `sort_key` で並べる。

### 5.6 停止時の上書き事故と二重起動ロック (2026-08-20)

旧コードで動作中の KiriCondencer がいる状態で、ディスク上の定跡を修復・
コードを更新したところ、停止時の保存 (メモリ上の旧データ・旧コード) が
修復を上書きして元に戻してしまった。教訓:

1. **動作中のプロセスがある限り、そのプロセスが書くファイルを
   外から直してはいけない**。停止時の保存で必ず巻き戻る。
2. プロセス確認はエラーを握りつぶさない方法で行う
   (tasklist の空出力を「無し」と読むな)。
3. 対策として **InstanceLock** (OS のバイト範囲ロック) を導入。
   二重起動は拒否され、外部ツールも acquire を試すだけで
   動作中かを確実に判定できる。クラッシュしても OS が解放するので
   stale ロックの掃除は不要。
4. LearningBook は value 欄を読まないので、壊れた value を持つ
   ファイルも「読み込み→保存」で自然に治る (テストで固定)。

バックアップは `../backups/` に集約する:
`100_x_condenser.ORIGINAL.db` が原本 (未ソート)、
`100_x_condenser.CLEAN-baseline.db` がソート済みのクリーン基準。

### 5.7 学習規則は加算と削除で1組 (2026-08-20)

前例取り込みが**加算しかしていなかった**。自己対局には
「負け側を分岐点まで遡って削除」があるのに前例側に無く、定跡は候補が
増える一方になっていた (makebook4 の規則の片側だけを実装していた)。

削除規則を `CycleRunner.prune_losing_line` に切り出し、前例取り込みと
自己対局の両方から呼ぶようにした。加算と削除は手番の違う局面を触るので、
順序を分けても結果は変わらない。

**加算と削除は必ず対で入れること**。片方だけでは定跡が発散するか、
逆に痩せ細るかのどちらかになる。

## 6. テスト方針

- common.py が最重要: position_key/move16 変換は **KiriCompass の実物
  csa.db との突き合わせテスト**を書く (壊れると全部が静かに狂う)。
- gen_core は fake エンジン (wrapper/tests/fake_engine.py 流用) で
  「1局→更新→書き出し→ラッパーで読める」のラウンドトリップを固定。
- 更新ポリシーは純関数的にユニットテスト (勝ち/負け/千日手既定=後手勝ち/
  絶対定跡不変/取り込み棋譜)。
- miner_bridge は BookMiner の代役 (コマンドを記録するだけの fake) で
  コマンド列を検証。

## 7. 決めていないこと (実装時・運用後に決める)

- 学習ポリシーの改良 (評価値の利用、csa.db 勝率との合成など) —
  UpdatePolicy の差し替えで実験する
- 課題局面の自動選定ロジック (decisions.jsonl / csa.db からのスコアリング)
- out/ → book/ の自動昇格と検証ゲート
- suisho_gen ↔ BookMiner の相互連携の中身
- 各ツールの常駐化・スケジューラ (当面は手動起動)
