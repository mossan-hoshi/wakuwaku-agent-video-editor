# wwedit — 「わくわくべんきょ会」収録動画 自動編集＆投稿AIエージェント

勉強会「わくわくべんきょ会」の収録動画（デスクトップ画面録画 + 話者別音声）を、
**無音/不要区間カット → 文字起こし → チャプター/投稿単位分割 → フレーミング →
イントロ/セクション区切り/BGM/字幕付与 → YouTube下書き投稿 → サムネ/概要欄生成**
まで自動化する、Claude Code 主導のエージェント。

YouTube: [@mossan_hoshi](https://www.youtube.com/@mossan_hoshi)

## 全体方針

- **合成の背骨**: ffmpeg合成 + fcpxml書き出し（全編集情報）+ YouTube Data API投稿。
  DaVinci Resolve は緊急の手修正先として残すのみ（常用しない）。
- **中核データ = EDL(Edit Decision List) JSON**: 単一の編集状態のSSOT。
  各工程がこのJSONを読み書きし、合成器と fcpxml 書き出し器が消費する。
  「カット＝削除」ではなく「無効フラグ＋範囲」で表現し、戻し/範囲調整を可能にする。
- **確認・修正**: ローカルWebアプリ（FastAPI + React/Vite）で中間成果を可視化・修正、
  修正ログを将来のカスタムモデル学習データとして蓄積する。

## 要素技術（確定）

| コンポーネント | 採用 |
|---|---|
| 日本語STT(word単位+フィラー) | WhisperX |
| 画面OCR(bbox) | **RapidOCR-ONNX**（PaddleOCRはWindows DLL衝突で不可） |
| メイン領域/フレーミング | OmniParserでno_crop判定。**crop枠は専用モデル学習済＋CLI反映**（DINOv2部分fine-tune＋aug／収録単位CV mean IoU0.653>床0.617・`framing crop-apply`でEDL書き戻し） |
| 動き/シーン変化 | **codec符号化サイズ(ffprobe)** が既定（PySceneDetectも選択可） |
| リップシンク(アニメ・クラウド) | DomoAI（クラウドAPI・未実装） |
| TTS / 画像生成 / 投稿 | AIVis / nano banana / YouTube Data API（未実装・要キー） |

- 詳細仕様（不変）: 承認済みSDD `~/.claude/plans/plan-sequential-harp.md`
- **実装の現状・確定判断・実行手順: [`docs/STATUS.md`](docs/STATUS.md)（圧縮/セッション跨ぎの復元はここを見る）**

## セットアップ

```bash
uv sync                 # 基本依存
uv sync --extra cv      # CV(動き検出/OCR前処理) を使うとき
uv run wwedit --help
```

実行環境: Windows 11 / RTX 2070 (8GB VRAM) / Python 3.11 / CUDA対応ffmpeg。

## マイルストーン

- **M0** 基盤: リポジトリ/EDLスキーマ/時間正規化/ffmpegユーティリティ/ingest/novtube移植
- **M1** 薄いE2E: 取り込み→STT→VADカット→チャプター→単純合成→YouTube下書き
- **M2** 編集品質: フレーミング/30秒ノーマライズ/style字幕/BGM/セクション区切り/fcpxml全情報
- **M3** イントロ: 文生成→AIVis→キャラ画像→リップシンク→字幕/ロゴ合成
- **M4** 仕上げ: サムネ/概要欄/投稿単位分割/チャプター登録/確認ゲート自動化
- **M5** 学習: fcpxml/修正ログ→データセット→カスタムモデル学習・差し替え
