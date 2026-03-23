## V_MV_combine

音声を使って複数動画の位置を揃え、レイアウト結合するCLIツールです。

趣味 (デレステ) のために開発しましたが、ほとんど保守するつもりはないです

### セットアップ (uv)

```bash
cd /Users/ykmbp/Workspace/git/V_MVcombine
uv run python --version
```

初回の`uv run`で依存パッケージは自動インストールされます。

### コマンド

```bash
uv run python main.py --help
```

サブコマンド:
- `align`: 音声位置合わせ + 前後トリム
- `combine`: 既に揃えた動画をレイアウト結合
- `process`: `align` + `combine`を一括実行

### オプション一覧

共通画質オプション（`align` / `combine` / `process`）:
- `-q`, `--q`, `--quality {high|medium|low|testfast|youtube|source}`: 画質プリセット
  - `youtube`: 解像度ベースでYouTube向けに自動調整
  - `source`: 入力側ビットレート寄りで保持優先

`align`:
- `--input-dir`, `--in <DIR>`: 入力動画ディレクトリ（必須）
- `--output-dir`, `--out <DIR>`: 揃え後動画の出力先（必須）
- `--strict-first`: 厳密一致探索を先に試す（既定は相関ベース）
- `--reference-video`, `--ref <FILE>`: 入力ディレクトリ内の基準動画ファイルを指定
- `--pad-to-reference`, `--pad`: `--ref`と併用。基準動画の長さに揃え、不足区間を黒画面/無音で埋める

`combine`:
- `--input-dir`, `--in <DIR>`: 結合対象動画ディレクトリ（必須）
- `--output`, `--out <FILE>`: 出力動画ファイル（必須）
- `--layout {row|top2bottom3|top3bottom2|top1bottom2|top2bottom1|grid|file}`: 配置方式（必須）
- `--grid-size <XxY または X*Y>`: `--layout grid`時に必須
- `--layout-file <FILE>`: `--layout file`時のTSV/CSV配置ファイル（省略時はファイル名順）
- `--background-color`, `--bg <black|#RRGGBB|0xRRGGBB>`: 空きスペース背景色（既定: `black`）

`process`:
- `--input-dir`, `--in <DIR>`: 入力動画ディレクトリ（必須）
- `--output`, `--out <FILE>`: 最終結合動画ファイル（必須）
- `--layout {row|top2bottom3|top3bottom2|top1bottom2|top2bottom1|grid|file}`: 配置方式（必須）
- `--grid-size <XxY または X*Y>`: `--layout grid`時に必須
- `--layout-file <FILE>`: `--layout file`時のTSV/CSV配置ファイル（省略時はファイル名順）
- `--background-color`, `--bg <black|#RRGGBB|0xRRGGBB>`: 空きスペース背景色（既定: `black`）
- `--strict-first`: 厳密一致探索を先に試す（既定は相関ベース）
- `--reference-video`, `--ref <FILE>`: 入力ディレクトリ内の基準動画ファイルを指定
- `--pad-to-reference`, `--pad`: `--ref`と併用。基準動画の長さに揃え、不足区間を黒画面/無音で埋める
- `process`実行時の中間動画は、`--out`の親ディレクトリ配下に`aligned/`として自動作成

### 3本を左から並べる (YouTube向け品質)

```bash
uv run python main.py process \
  --in testdata \
  --out output/final_row3_youtube.mp4 \
  --layout row \
  --q youtube
```

### align の使用例（位置揃えのみ）

```bash
uv run python main.py align \
  --in testdata \
  --out output/aligned_only \
  --q youtube \
  --ref "testdata/ScreenRecording_03-23-2026 23-50-43_1.mov" \
  --pad
```

このコマンドは「位置揃え + トリミング（または黒/無音パディング）」まで行い、結合はしません。

### combine の使用例（結合のみ）

```bash
uv run python main.py combine \
  --in output/aligned_only \
  --out output/final_from_aligned.mp4 \
  --layout grid \
  --grid-size 3x2 \
  --bg "#112233" \
  --q youtube
```

このコマンドは既に揃え済みの動画を結合します（音声アライン処理はしません）。

### 基準動画を指定して合わせる（不足尺は黒/無音で埋める）

```bash
uv run python main.py process \
  --in testdata \
  --out output/final_row3_refpad_youtube.mp4 \
  --layout row \
  --q youtube \
  --ref "testdata/ScreenRecording_03-23-2026 23-50-43_1.mov" \
  --pad
```

### X*Y グリッドで並べる（空きマスは指定色）

```bash
uv run python main.py process \
  --in testdata \
  --out output/final_grid_3x2_color.mp4 \
  --layout grid \
  --grid-size 3x2 \
  --bg "#112233" \
  --q youtube \
  --ref "testdata/ScreenRecording_03-23-2026 23-50-43_1.mov" \
  --pad
```

`--grid-size` は `3x2` と `3*2` のどちらでも指定できます。  
動画数が `X*Y` を超える場合はエラーになります（例: 50本を7x7に配置）。

### layout=file で配置ファイルを使う（TSV/CSV）

`--layout file`では、配置ファイルの1行が1段、列が横位置になります。  
セルには「ファイル名のみ」を書いてください（パス不可、空セル可）。
このとき`--grid-size`は不要で、指定するとエラーになります。

TSV例:
```text
movie_c.mp4	movie_a.mov	
	movie_b.mp4	
```

CSV例:
```text
movie_c.mp4,movie_a.mov,
,movie_b.mp4,
```

実行例:
```bash
uv run python main.py combine \
  --in output/aligned_only \
  --out output/final_layout_file.mp4 \
  --layout file \
  --layout-file testdata/layout_example.tsv \
  --bg "#223344" \
  --q youtube
```

`--layout-file`を省略した場合は、ファイル名順で横一列に並べます。

### レイアウト

- `row` (X=動画数, Y=1 の横並び)
- `top2bottom3` (上2/下3の5本)
- `top3bottom2` (上3/下2の5本)
- `top1bottom2` (上1/下2の3本)
- `top2bottom1` (上2/下1の3本)
- `grid` (任意のX*Y。例: 2x2 空きマスは背景色で埋める)
- `file` (TSV/CSVで行列配置。未記載ファイルはファイル名順で末尾に追加)

### サブコマンド差分メモ

- `align`で使える: `--out`, `--strict-first`, `--ref`, `--pad`
- `combine`で使える: `--out`, `--layout`, `--grid-size`, `--layout-file`, `--bg`
- `process`で使える: `align`系 + `combine`系（`--layout-file`含む。一括実行で`aligned/`は自動生成）
