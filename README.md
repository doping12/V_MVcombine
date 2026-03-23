## V_MV_combine

音声を使って複数動画の位置を揃え、レイアウト結合するCLIツールです。

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
- `--quality {high|medium|low|testfast}`: 手動画質プリセット
- `--quality-mode {manual|youtube|source}`:
  - `manual`: `--quality`を使用
  - `youtube`: 解像度ベースでYouTube向けに自動調整
  - `source`: 入力側ビットレート寄りで保持優先

`align`:
- `--input-dir <DIR>`: 入力動画ディレクトリ（必須）
- `--output-dir <DIR>`: 揃え後動画の出力先（必須）
- `--no-strict-first`: 厳密一致探索をスキップして相関ベース推定を優先
- `--reference-video <FILE>`: 入力ディレクトリ内の基準動画ファイルを指定
- `--pad-to-reference`: `--reference-video`と併用。基準動画の長さに揃え、不足区間を黒画面/無音で埋める

`combine`:
- `--input-dir <DIR>`: 結合対象動画ディレクトリ（必須）
- `--output <FILE>`: 出力動画ファイル（必須）
- `--layout {row|grid2x2|pyramid5|top1bottom2|grid|file}`: 配置方式（必須）
- `--grid-size <XxY または X*Y>`: `--layout grid`時に必須
- `--layout-file <FILE>`: `--layout file`時のTSV/CSV配置ファイル（省略時はファイル名順）
- `--background-color <black|#RRGGBB|0xRRGGBB>`: 空きスペース背景色（既定: `black`）

`process`:
- `--input-dir <DIR>`: 入力動画ディレクトリ（必須）
- `--aligned-dir <DIR>`: 中間（位置揃え後）動画の出力先（必須）
- `--output <FILE>`: 最終結合動画ファイル（必須）
- `--layout {row|grid2x2|pyramid5|top1bottom2|grid|file}`: 配置方式（必須）
- `--grid-size <XxY または X*Y>`: `--layout grid`時に必須
- `--layout-file <FILE>`: `--layout file`時のTSV/CSV配置ファイル（省略時はファイル名順）
- `--background-color <black|#RRGGBB|0xRRGGBB>`: 空きスペース背景色（既定: `black`）
- `--no-strict-first`: 厳密一致探索をスキップして相関ベース推定を優先
- `--reference-video <FILE>`: 入力ディレクトリ内の基準動画ファイルを指定
- `--pad-to-reference`: `--reference-video`と併用。基準動画の長さに揃え、不足区間を黒画面/無音で埋める

### 3本を左から並べる (YouTube向け品質)

```bash
uv run python main.py process \
  --input-dir testdata \
  --aligned-dir output/aligned \
  --output output/final_row3_youtube.mp4 \
  --layout row \
  --quality high \
  --quality-mode youtube
```

### align の使用例（位置揃えのみ）

```bash
uv run python main.py align \
  --input-dir testdata \
  --output-dir output/aligned_only \
  --quality high \
  --quality-mode youtube \
  --reference-video "testdata/ScreenRecording_03-23-2026 23-50-43_1.mov" \
  --pad-to-reference
```

このコマンドは「位置揃え + トリミング（または黒/無音パディング）」まで行い、結合はしません。

### combine の使用例（結合のみ）

```bash
uv run python main.py combine \
  --input-dir output/aligned_only \
  --output output/final_from_aligned.mp4 \
  --layout grid \
  --grid-size 3x2 \
  --background-color "#112233" \
  --quality high \
  --quality-mode youtube
```

このコマンドは既に揃え済みの動画を結合します（音声アライン処理はしません）。

### 基準動画を指定して合わせる（不足尺は黒/無音で埋める）

```bash
uv run python main.py process \
  --input-dir testdata \
  --aligned-dir output/aligned_ref \
  --output output/final_row3_refpad_youtube.mp4 \
  --layout row \
  --quality high \
  --quality-mode youtube \
  --reference-video "testdata/ScreenRecording_03-23-2026 23-50-43_1.mov" \
  --pad-to-reference
```

### X*Y グリッドで並べる（空きマスは指定色）

```bash
uv run python main.py process \
  --input-dir testdata \
  --aligned-dir output/aligned_grid \
  --output output/final_grid_3x2_color.mp4 \
  --layout grid \
  --grid-size 3x2 \
  --background-color "#112233" \
  --quality high \
  --quality-mode youtube \
  --reference-video "testdata/ScreenRecording_03-23-2026 23-50-43_1.mov" \
  --pad-to-reference
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
  --input-dir output/aligned_only \
  --output output/final_layout_file.mp4 \
  --layout file \
  --layout-file testdata/layout_example.tsv \
  --background-color "#223344" \
  --quality high \
  --quality-mode youtube
```

`--layout-file`を省略した場合は、ファイル名順で横一列に並べます。

### レイアウト

- `row` (X=動画数, Y=1 の横並び)
- `grid2x2` (4本)
- `pyramid5` (上2/下3の5本)
- `top1bottom2` (上1/下2の3本)
- `grid` (任意のX*Y。空きマスは背景色で埋める)
- `file` (TSV/CSVで行列配置。未記載ファイルはファイル名順で末尾に追加)

### サブコマンド差分メモ

- `align`で使える: `--output-dir`, `--no-strict-first`, `--reference-video`, `--pad-to-reference`
- `combine`で使える: `--output`, `--layout`, `--grid-size`, `--layout-file`, `--background-color`
- `process`で使える: `--aligned-dir` + `align`系 + `combine`系（`--layout-file`含む、一括実行）
