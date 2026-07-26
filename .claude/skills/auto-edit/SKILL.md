---
name: auto-edit
description: わくわくべんきょ会の新規収録1本を、収録を渡すだけで投稿可能動画まで自動編集する司令塔(マスター・オーケストレータ)。各工程のCLI呼び出し・LLM工程のスキル/サブエージェントへのdispatch・目視QA・継続学習をClaude Code側で完結し、ユーザーが要るのは判断ゲート(素材/キー不足・編集確認・投稿前承認)だけ。新規収録を編集して投稿したい時に最初に使う。
---

# auto-edit — 収録1本を投稿可能動画まで自動駆動する司令塔

新規収録（`data/<date>/` または取り込み元）を渡されたら、**§8 RUNBOOK を端から自動実行**する。
CLI呼び出し・LLM工程の判断/dispatch・目視QA・尺ループ・継続学習の判断は**全部このスキル（Claude）が行い**、
**ユーザーが止まるのは下記ゲートだけ**。詳細手順・設定・罠は `docs/STATUS.md`（§1/§8/§9）と各メモリが正。

## 原則
- **EDL(SSOT)を非破壊**で各工程が読み書き。出力は EDL から再生成。元動画/音声/EDLを壊さない。
- **判断ゲート以外は止まらない**。各工程の主要成果は SendUserFile で見せて進む（承認待ちにはしない＝下記ゲート以外）。
- **前提不足・CLI失敗は止めて原因提示**（推測で進めない）。
- **コスト規律**: 文字起こしを読むLLM工程は Sonnetサブエージェント＋ファイルI/O（caption/chapter/filler）。DomoAI(lipsync)は高額＝QA合格後のみ。
- **GPU規律**: 重い処理前に `nvidia-smi` でVRAM確認・単一ジョブ（[[no-heavy-gpu-without-consent]]）。

## ユーザー判断ゲート（ここだけ止まる）
- **G1 素材/キー不足**（開始時）: `.env` 必須キー（GEMINI/DOMOAI、投稿時 WWEDIT_YT_*）／**本編BGMジャンルの選択**（`Videos/.../bgms/<genre>`）／不足は AskUserQuestion で確認。
- **G2 編集確認**: cut/framing を `edit serve` で確認・手修正してもらう（「skipでいい」と言われたら飛ばす）。完了後 `framing harvest-corrections` で次回学習データ化。
- **G-文言 課金前の文言審査（必須）**: **お金のかかる生成を回す前に、文言をまとめて提示して承認を取る**。
  対象＝**イントロ台本（読み＋字幕の両方）／動画タイトル（回数込み）／サムネに描く文字**。
  理由: TTS→キャラ画→**lipsync($0.06/秒≒$0.60)**／サムネ(nano banana)は**作り直すたびに課金**される。
  文言が確定してから初めて `publish tts` 以降・`publish thumbnail` を回す。**回数(#NN)も必ずここで確認**（推測しない）。
- **G3 投稿前 最終承認**: 完成動画＋サムネ＋概要欄を見せ、**YouTube下書き化の直前**で承認を取る。
- ※**承認済み文言で作った生成物（イントロ動画・サムネ画像）は、見せて自動で次へ**（直しは言われたら対応）。

## 実行順（§8 RUNBOOK・各工程の自動化記号は §8 準拠）
1. **G1 前提チェック**: `.env`キー・extras同期・SBV2素材・BGMフォルダ存在を確認。**BGMジャンルを選んでもらう**。足りない物だけ G1 で確認。
2. `[CLI]` **ingest** → `data/<date>/edl.json`。
3. `[CLI][GPU]` **transcribe**（VRAM確認）。
4. `[CLI]`+**filler-selector スキル** **cut**: `cut auto-vad --refine`（無音=VAD＋動的エネルギー床。`cut auto`は非動的なので使わない＝[[cut-auto-vs-autovad-dynamic]]）→ `fillers-prepare`→filler-selector→`fillers-apply`（フィラー取捨）→ **`cut ngwords`**（.env `WWEDIT_CUT_NGWORDS` の語に言及した発話をまるごとカット。未設定なら無動作＝安全側）。
5. `[CLI][GPU軽]` **framing** `scenes`→`classify-motion`→`crop-apply`（学習済モデル）。
   **`crop-apply` は既定で「全画面(no_crop)を残さない」**＝モデルが bbox を付けられなかった区間へ
   **上下左右1割トリム**(中央80%・16:9維持)を自動で入れる（`framing default-trim` 単体でも実行可・
   `--no-default-trim` で従来の全画面。G2 の「調整」トラックに枠として出るので手直しできる）。
6. `[CLI]` **画面NGモザイク**: **`privacy ng-mosaic <edl>`** ＝ 画面OCRで `.env` の
   `WWEDIT_MASK_TERMS` ∪ `WWEDIT_CUT_NGWORDS` が写った箇所へ、**大きめのモザイク重ねを自動付与**
   （EDL.overlays・ソースフレーム基準）。**カットしない**（本編の流れを切らない）。
   OCRは `screen_ocr.json` の**共有キャッシュで1回だけ**＝次の `chapter screen-text` が同じ結果を使う
   （[[cache-model-forward-not-resweep]]）。語未設定なら無動作＝安全側。G2 で位置/サイズを手直し可。
7. `[CLI]`+**chapter-detector スキル** **chapter** `screen-text`→`prepare`→（detector）→`apply`→`youtube`。
8. `[CLI]`+**caption-summarizer スキル(Sonnet)** **subtitle** `prepare-captions`→（summarizer）→`apply-captions`。
9. **=== G2 編集確認 ===** `edit serve <edl>`（httptools破損中は `http="h11"`）。手修正完了後 `framing harvest-corrections <edl>`。
10. `[CLI]` **compose video** `--framed --subtitles --audio speakers --bgm "<G1で選んだジャンル>"` **`--chapter-ribbon`** **`--eyecatch`** → 本編mp4（`*_ec.mp4`）。**[H]** で全チャプター冒頭に2秒のgenerative-artアイキャッチを挿入。
   **アイキャッチの音は のべつべ！キャラの「一言」ボイス**（`--eyecatch-voice` 既定ON・**音楽ジングルは廃止**）＝
   SBV2日本語モデルの**キャラを章ごとにランダム**に選び、「つ～ぎ！」「さてと」等の短い一言を**都度合成**して喋らせる。
   イントロと同じく**右上にロゴ＋キャラ名バッジ**を出す。台詞/キャラの定義は `publish/eyecatch_voice.py`
   （読みは**かな書き**＝SBV2は漢字/英字を誤読）。SBV2サーバ未起動なら `--eyecatch-jingle-dir` の音楽へ自動退避。
   **挿入で章時刻がずれるので `*_ec_chapters.txt`（補正済み）を概要欄に使う**。**`--chapter-ribbon`** は左上に収録日＋章名の2段リボンを常時表示（章ごとに**話者色**＝chapterの `speaker` 属性で色分け・字幕と同系統。mossan-hoshi=青/taniguchi=紫等）。収録日は `data/<date>/` の日付から自動（`M/D収録`）。
11. **=== G-文言 課金前の文言審査 ===** イントロ台本（読み＋字幕）・**タイトル（#NN 込み・回数は推測せず確認）**・
    サムネに描く文字を**まとめて提示して承認を取る**。承認後に手順11以降の課金生成へ進む。
12. **intro-builder スキル**: イントロ生成（服装非重複/尺/QAは intro-builder が判断・**台本は承認済みのものを使う**）。生成物を見せ**自動で次へ**。`publish intro-compose` で仕上げ合成（本編先頭に連結）。
13. `[CLI]` **[L]**: サムネは **`publish thumbnail --char <キャラ> --prompt "<文字・配色・構図・表情まで記述>"`＝nano banana 2 一発生成**（キャラ・背景・日本語タイトル文字を一括描画。立ち姿参照で絵柄/キャラ固定。PIL帯合成は廃止＝[[thumbnail-oneshot-nano-banana]]）／タイトル・要約を書き `publish description`。**アイキャッチ挿入時は `publish description --chapter-lines-file <*_ec_chapters.txt>`** で補正章時刻を反映。見せて**自動で次へ**。
    ※**サムネ/イントロのキャラは回ごとに指定されうる**（既定 noa。ユーザー指示があればそのキャラ＝`--char yume` 等）。
13. **=== G3 投稿前 最終承認 ===** 完成動画＋サムネ＋概要欄を提示→承認後 `publish youtube --video <mp4>`（token有れば `--no-dry-run`、無ければ dry-run で JSON 生成し G1 に差し戻し）。

## 1収録→複数投稿（post-unit ループ）
上流（手順1〜9の ingest〜字幕〜編集確認）は**収録1回**。**手順10以降（compose/イントロ/サムネ/概要欄/投稿）を投稿単位ごとに回す**:
- `edl.post_units` の件数 = `n_post_units`（CLIで確認可）。**0/1件＝従来どおり1本**。2件以上＝各 index で出力ループ。
- 各 index N について: `compose video --post-unit-index N`（その単位の動画）→ intro-builder（その単位向け台本でイントロ）→
  `publish thumbnail`（単位の内容で文言）→ `publish description --post-unit-index N`（単位内の章・時刻）→ G3承認 → `publish youtube --video <その動画>`。
- 出力名は `*_p<N>` で分かれる（compose/概要欄/章CLIが自動付与）。
- post_units の妥当性（分割位置・本数）に疑問があれば G2 編集確認でユーザーに確認（editor で post-unit を編集できる）。

## 失敗時の既知対処（推測せず適用）
- webapp 無応答 → httptools破損＝`uvicorn.run(..., http="h11")`（§6）。
- DomoAI 403 → Cloudflare UA／host=`api.domoai.com`（`publish/domoai.py` 処理済）。
- SBV2 /synth 失敗 → github SBV2 venv で `tool/dub_local/server.py` 起動（[[external-assets-and-keys]]）。
- `uv` extra で他extra消える → 複数同時指定。opencv hardlink → `UV_LINK_MODE=copy`。
