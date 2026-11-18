# Git版に含むもの・含まないもの

KiriCadenceの対局用ラッパー、KiriCondenser、自戦から既存BookMiner GUIへ
投入するためのPython連携コードを管理する。`tools/`の関連モジュールは
既存の連携コードが参照する依存物であり、起動・採掘・配備を自動で開始しない。

BookMiner本体はこのGitリポジトリに含めない。上流ソースのコピー、
`tools/gui_native/`のBookMiner改造コピー、`tools/vendor/`の上流GUI、
実行ファイル・ビルド成果物は除外する。定跡・棋譜・評価関数、
`data/`・`runtime/`・`reports/`・`releases/`、このPC専用の設定も含めない。

## KiriCadence単体を使う

1. Pythonと`requirements.txt`の依存を用意する。
2. `config-sample.txt`を`config.txt`へコピーし、`EnginePath`・`BookDir`を指定する。
3. 対局ソフトへ`run.bat`を登録する。定跡とエンジンは各自で用意する。

`BookMinerLive`の既定値は`0`なので、BookMinerなしでも対局できる。

## 既存BookMiner GUIへの投入連携

`BookMinerLive 1`と`BookMinerWork`を設定する。この連携先には、
`gui-status.json`と`control/`の受付・結果確認プロトコルに対応した
既存GUIが必要。素のBookMiner実行ファイルへ直接接続する方式ではない。
GUIのソースや採掘本体はこの最小パッケージには含まれない。

```powershell
python YOSC/tools/gui_control.py status --work "D:/BookMiner/runtime/gui"
python YOSC/tools/gui_control.py enqueue --work "D:/BookMiner/runtime/gui" --path "input.txt" --id "my-input-001"
python YOSC/tools/gui_control.py result --work "D:/BookMiner/runtime/gui" --id "my-input-001"
```

入力は全USI手順を保持して凍結し、ネイティブ受付を確認して`accepted`を返す。
結果不明時は同じIDで照会し、新しいIDで送り直さない。
GUI不在・停止中なら拒否し、GUIやBookMinerを勝手に起動しない。

自作GUI・差分ペタ更新全体は、BookMinerの外部依存と配布範囲を分けて扱う。
このパッケージの追加によって、現在動いているGUI・採掘・定期反映は変更しない。
