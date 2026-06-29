# 実装状況（IMPLEMENTATION STATUS）

このファイルは**実装の現状・確定した設計判断・パイプライン実行手順**を残す唯一の状況ドキュメント。
コンテキスト圧縮やセッション跨ぎでも、こことコード+テストだけで状態を復元できることを目的とする。
（最終更新: 2026-06-28）

**新規収録→投稿の通し手順は §8 RUNBOOK、再現性の正直な現状と穴は §9 監査を見る。**

承認済みSDD（不変の全体仕様）: `~/.claude/plans/plan-sequential-harp.md`。本書はその**実装側の写像**。

---

## 0. 中核モデル（不変の前提）

- **EDL JSON = 単一の編集状態SSOT**（`src/wwedit/edl/schema.py: Edl`）。全工程がこれを読み書きし、
  合成器（renderer）は EDL から**毎回再生成するだけ＝非破壊**。元動画・元音声・EDLは壊さない。
- カット＝削除ではなく `Segment.invalid` フラグ＋範囲（戻せる）。`edl.kept_ranges()` が採用区間。
- 付加物（ローディング/字幕/ロゴ/BGM）は**最上位レイヤーへ overlay 合成**（下の footage/音声は残す）。
- 時刻は基本**ソースタイムライン秒**で持ち、出力（カット後）時刻へは renderer で変換する。

EDL 主要フィールド: `segments / utterances(words付) / chapters / framing / subtitles /
subtitle_speaker_colors / bgm / post_units`。

---

## 1. パイプライン（実装済みCLI・実行順）

`uv run wwedit <group> <command>`。EDLパスは各収録の `data/<date>/edl.json`。

1. **ingest**: 取り込み・正規化（`ingest` group）。
2. **transcribe**: WhisperX で word単位STT → `edl.utterances`（`transcribe`）。
3. **cut**: 無音/フィラー/NGワードカット → `edl.segments`（`cut`。無音=`auto-vad --refine`動的閾値、フィラー=filler-selectorスキル、NGワード=`cut ngwords`＝.env `WWEDIT_CUT_NGWORDS` に言及した発話をまるごとカット）。
4. **framing**:
   - `framing scenes <edl>`: codecサイズ動き検出で安定区間 → `edl.framing`（static / pending）。
   - `framing classify-motion <edl>`: pending を オプティカルフローで loading(画面切替) / pending(動画) に分類。
   - `framing assign <edl>`: **既定=保守的に全画面(no_crop)**。`--aggressive` で OmniParser判定+固定箱（過剰crop注意）。
   - **`framing crop-apply <edl>`**: 学習済み専用モデル(`crop_model.pt`)で static 区間の代表フレームを推論し
     `framing.bbox` へ一括書き戻す（既定 device=cpu・数十秒）。`compose --framed`/編集ツールの crop枠に反映。
   - **`framing crop-train`**（任意・再学習）: 全 crop アノテで本番モデルを再学習し `crop_model.pt` を更新（重いGPU・§2の規律厳守）。
   - `framing loading-clips`（任意）/ `framing omni-cache`（評価用）。
5. **chapter**:
   - `chapter screen-text <edl>`: static区間の代表フレームをメイン領域でOCR → `screen_text.txt`（固有名補正の文脈）。
   - `chapter prepare <edl>` → chapter-detector スキル → `chapter apply <edl>` → `chapter youtube`。
6. **subtitle**（[I]・下記§3に詳細）:
   - `subtitle prepare-captions <edl>`: カット後12秒窓のTSV `caption_input.tsv` を生成（末尾に screen_text.txt のOCR文脈を付与）。
   - → **caption-summarizer スキルを Sonnetサブエージェントで実行**（§4）→ `caption_decisions.json`。
   - `subtitle apply-captions <edl>`: 決定を `edl.subtitles` へ（話者/タイミング/置換/注意書き込み）。
   - `subtitle color <edl> <話者> <red|purple|blue|green|auto>`: 話者色の上書き。
7. **compose**:
   - `compose video <edl> --framed --subtitles --bgm <dir|file>`: カット連結＋framing crop＋
     loading overlay＋字幕焼き込み＋BGMダッキングを1パス。`--audio speakers|embedded`。
     **BGMにフォルダを渡すと同ジャンル全曲を連続再生**（1曲ループにしない）。並びは `order_bgms`＝
     **収録dir seed の決定的シャッフル**（カテゴリ固定でランダム順・乱数非依存・再レンダリング再現）。
     `render_bgm_playlist` が **各曲を loudnorm(-18 LUFS=unify基準)で揃えてから連結**→1本のwav。
     **本編BGMの最終音量は `--bgm-target-lufs`（既定 -34 LUFS＝カフェBGM並み）**で決め、
     声(-16 LUFS)の約18 LU下に敷く（gain=target-unify を一括適用）。`--bgm-target-lufs 0`以上で
     無効化し相対 `--bgm-gain-db` を使う。出力尺まで `-stream_loop`。単一ファイル＋target無効はその曲をループ。
   - `compose fcpxml`: **全編集情報の書き出し（記録/相互運用、緊急時のみDaVinci）**。カット＋話者音声＋
     字幕(title・話者色・出力時刻再マップ)＋**framing(crop=adjust-transform で部分矩形を全画面充填・scale+position)**まで対応。
     音量/BGMは後続。※手修正の主舞台ではない。
8. **privacy**: `privacy mask-frame`（PII画面マスク。秘匿語は `.env` のみ）。
9. **edit（手修正の主舞台＝自前タイムラインNLE）**: `edit serve <edl>` でローカル起動（既定 :8800・**--preview mp4 不要**=レンダ結果は動的合成）。
   **DaVinciは使わない**（緊急のみ）。SSOTのEDLを**非破壊編集**し全修正を `correction_log.jsonl` に蓄積（M5学習データ）。
   実装 `webapp/editor.py`（API）＋`webapp/static/editor.html`（UI・単一ファイル）＋`webapp/cli.py`。詳細は下記§7。
   別に `framing annotate`＝crop箱アノテータ（`webapp/server.py`、モデル学習用データ作成）。
   ⚠️ **API(Pythonエンドポイント)を変更したらサーバ再起動が必要**（HTMLは都度読込で即反映）。httptools破損中は `http="h11"` 起動（§6）。

---

## 2. フレーミング[E]の現状 ✅【2026-06-27 専用モデル床超え達成】

- ✅ **専用クロップモデルを実学習し、定数床(IoU≈0.62)を超えた**。当初依頼＝専用クロップAIモデルの学習で、
  ユーザーのアノテがその教師データ。旧版の「学習不能」結論は誤りだった。
- **Deep Research 回答を批判的採用**: `docs/research/crop-framing-model-research.md` の依頼にユーザーが実行→回答取得。
  推奨は **frozen backbone + 二頭ヘッド（no_crop/crop分類 ＋ center+log-scale回帰）**で dense patch 特徴を空間保持。
  ⚠️ 回答の数値（zoom 1.10–2.21等・カーソル83%）は**我々の実測と食い違い／出典不明＝鵜呑み禁止**だが、定性方針は妥当。
- **実学習の結論（収録単位 grouped CV・リーク無し）**:
  - frozen DINOv2 dense特徴＋線形(Ridge)＝床とほぼ同じ 0.62（global pool が size 情報を潰す＝1スケール固定では頭打ち）。
  - **DINOv2 ViT-S/14 を後段2ブロック部分fine-tune＋scale/pan 同変aug** → **mean IoU 0.653 / median 0.700**（定数床 0.617 超え・5fold中4勝）。
    aug が「唯一効く信号=size(zoom)」を増幅できるのが鍵（frozen probe では増幅不可）。
- **新モジュール**（`framing-train` extra=timm を要する）:
  - `cropfeat.py`=frozen backbone の dense patch 特徴抽出＋ディスクキャッシュ。`cropmodel.py`=箱パラメタ化(正規化16:9=正方→cx,cy,log s)＋attention-pool ヘッド。
  - `croptrain.py`=frozen特徴の grouped CV（床probe）。`croptrain_ft.py`=**DINOv2部分fine-tune＋aug＋本番学習(`train_final`)＋EDL書き戻し(`apply_model_to_edl`)**。
  - CLI: `framing crop-cv`（frozen CV）/ `framing crop-cv-ft`（fine-tune CV）。入力252×448・AMP fp16・VRAM上限`mem_fraction`既定0.6。
- **本番モデル成果物（永続化済）**: `data/framing_pred/crop_model.pt`（state_dict+標準化+unfreeze）。
  **CLI化済**: `framing crop-apply <edl>` が各 static 区間の代表フレームを推論し **`EDL.framing.bbox`(px x,y,w,h) に書き戻す**
  （既定 device=cpu・数十秒）→`compose --framed`/編集ツールで反映。再学習は `framing crop-train`（`save_crop_model`で保存）。
  実測: 2026-06-04 EDL で 68 static区間に書き戻し成功・全bbox in-frame＆AR1.778（crop幅 0.63〜0.86・zoom1.16〜1.59）。
  **本番 `data/2026-06-04/edl.json` に crop 反映済**（適用前バックアップ＝揮発Tempの `edl_2026-06-04_backup_precrop.json`。
  戻すなら `framing assign <edl>` で全 no_crop に再初期化可）。`compose video --framed --max-ranges N` で焼込確認＝出力に正しく crop 反映を実映像検証済。
  品質所見: 概ねブラウザchrome/余白を落として本文へ寄せる妥当な挙動。一部(zoom≈1.6)は左見出しを切る過剰cropあり＝編集ツールで微修正前提（IoU0.653相応）。
- ⚠️ **GPU規律（重要・マシンを落とした反省）**: 重い学習を GPU99%・複数ジョブ並行で回しマシンをクラッシュさせた。
  **重めのGPU処理前に `nvidia-smi` で現行VRAMを確認し溢れない見込みなら実施・単一ジョブ・上限設定**。軽い推論はGPUで普通に（CPU逃げ不要）。
- **学習データ**: `data/framing_anno_full/`（有効アノテ**351件**、crop342/no_crop9・`frames/`）。`data/framing_ds/dataset.json` は読むだけ・改変禁止。
- 評価指標: crop mean IoU と no_crop↔crop 二値精度を同等、過剰クロップはガードレール。no_crop判定は OmniParser span でも可(IoU0.705)。
  ※ `FramingRegion.bbox` はスキーマ上**ピクセルint4組**(x,y,w,h)。モデル出力(正規化float)は apply 時に px へ変換して書き戻す。
- **継続学習ループ（手修正→再学習・実装済）**: 編集ツールの crop 手修正は `correction_log.jsonl`＋最終EDLに残る。
  `framing harvest-corrections <edl> [--trust-final]` が最終 static 区間 bbox を**人手GT**として
  `data/framing_corrections`（dataset.json＋frames・**追加蓄積のみ**／保護対象 framing_anno_full・framing_ds は不変）へ収穫。
  既定は log で人手が触れた区間のみ（保守的・confirmation bias回避）、`--trust-final` で最終EDL全cropを採用。
  corrections は専用 timeline グループ `corr:<rec>` ＝ grouped CV で別foldに隔離されリーク無し。
  再学習は `crop-train --extra-root data/framing_corrections`（`load_crop_items_multi` が anno_full と合算・image絶対パス化でroot非依存）、
  検証は `crop-cv-ft --extra-root ...`。実績: 2026-06-04 の手修正から **57 crop GT を収穫**（モジュール `framing/corrections.py`・テスト `tests/test_corrections.py`）。
  ループ: 編集ツールでcrop修正 → harvest-corrections → crop-train --extra-root → crop-apply で次回フレーミングが改善。
  **実行済(2026-06-27)**: 57手修正を足して継続学習→新 `crop_model.pt`（旧=anno_full のみ版は `crop_model_prev.pt`）。本番 2026-06-04 へ再apply。
  実映像比較で**以前の過剰crop(t1061・左見出し切れ)が矯正**され全体平均zoom 1.368→1.329（やや控えめ）＝人手修正の傾向を学習。一部(右パネル寄り)は好み次第で更に手修正→再収穫で改善するループに乗る。
- **学習スループット（GPU飢餓を解消）**: DataLoaderが num_workers=0＋クロージャDatasetで**フルレスPNGを単スレッドdecode**しGPUがutil数%で餓えていた。
  Dataset をモジュール最上位 `_CropFrameDS`（picklable）化し **num_workers並列decode＋persistent_workers＋prefetch＋batch128＋mem_fraction0.8**へ。
  **GPU util 3%→60%・25ep 242s・peakVRAM2.89GB**（バッチが小さいのでなくデータ供給律速だった）。`crop-train/crop-cv-ft` に `--num-workers` 追加。
- **残課題**: 二頭の no_crop 分類ヘッドはラベル枯渇(9件)で未統合＝当面 no_crop は OmniParser gate に委譲。
  上積み余地（解像度↑/疑似ラベル/DINOv3）。**解凍ブロック増は検証済で不発**＝`unfreeze=3` は mean0.6502/median0.6950 で
  既定`unfreeze=2`(0.653/0.700)を超えず（339件と小さく容量増は軽い過学習）。次の上積みは入力解像度↑が本命（`INPUT_HW`定数の変更＋VRAM増を伴う）。
  ※実験時 peakVRAM 1.6〜1.8GB＝mem_fraction0.6・単一ジョブで安全に完走（GPU規律の実証）。
- 動き種別: spread(オプティカルフローの空間広がり)≥0.6 で loading(画面切替)、未満は pending(コンテンツ内動画+警告)。
- ローディング画面: 白背景＋のべつべ!ロゴ＋「○○中…」ドットループ。`assets/logo/nobetube_logo.png`(透過2000²)。
  **ループ動画なのでラベルごとに1周(2秒)を1本だけ生成しキャッシュ**（`loading_loop_clip`）、各区間は `-stream_loop` で伸ばす。

実装: `framing/predict.py`（固定箱`[0.16,0.1932,0.8391,0.8723]`・OmniParser gate）、`motion_type.py`、
`loading_screen.py`、`compose/ffmpeg_compose.py`（`loading_overlay_intervals`/`build_framed_overlay_script`）。

---

## 3. 字幕[I]の確定仕様（重点・多数の指摘を反映）

- **二重枠**（メイリオ）: 内側から「**色の文字 → 白枠(固定) → 同色の外枠**」。白文字にしない。
  ASS 2レイヤー（L0=色fill+太い色outline / L1=色fill+白outline）。実装 `subtitle/ass.py`。
- **色は話者ごと**: sakamoto/mossan-hoshi=**寒色(blue/green)**、taniguchi=**暖色(red/purple)**。
  同一動画内は同一人物=同色（`assign_speaker_colors` が収録dirハッシュでペア内決定）。`EDL.subtitle_speaker_colors` で上書き可。
  イントロ字幕(style=intro)はピンク固定。
- **内容＝要約字幕**（逐語でない）。caption-summarizer スキルが**カット後12秒窓を1窓1字幕**で要約（§4）。
- **タイミング＝カット後の実時刻**: 単語タイムスタンプで「カット後に残る実発話」を~12秒窓に切り、窓の
  [開始,終了]に字幕を出す。`build_caption_windows`。原文字起こしの粗いアンカーは廃止（先行/早消えの原因だった）。
- **切れ目なし**: 前の字幕を次の字幕が出る瞬間まで伸ばす（消えて即出る現象を回避）。
- **本編最初の注意書き**: `DISCLAIMER_TEXT`（AI生成の免責）を先頭字幕に自動挿入（`apply_captions`）。
- **表記置換**: 個人名の漢字→カタカナ等。マップは**`.env: WWEDIT_SUBTITLE_NAME_MAP`**（コード非直書き）。
  実装 `privacy/masking.py: load_name_replacements/apply_name_replacements`。
- **色解決（話者なしを白にしない）**: disclaimer 等の話者なし字幕は、ass.py に合わせ **既定 blue**（intro=ピンク/話者あり=話者色）。
  編集ツールのライブ重畳も同じ解決（`webapp/editor.py: _sub_css`）＝真っ白で読めない問題を解消。
- **transcript-range CLI**（部品）: `subtitle transcript-range <edl> --start S --end E --pad 20` で指定期間±padの文字起こしを
  `[mm:ss] speaker:` 行で返す。LLM(字幕付け)が**全文を抱えず**狭い窓だけ取得する手段（後述§4の有界化設計）。
- **修正**: `write_caption_input` は text の全空白（`\r`等の改行含む）を1スペースに畳むよう修正（`\n`のみ除去では行ベース処理が壊れた）。

実装: `subtitle/ass.py`（二重枠・話者色）、`subtitle/summarize.py`（窓/要約適用/注意書き/置換）、
`subtitle/build.py`（逐語版フォールバック）、`subtitle/cli.py`（transcript-range）、`compose/ffmpeg_compose.py`（焼き込み・話者色解決・出力時刻再マップ）。

---

## 4. LLM内容生成の実行形態（コスト規律）

字幕要約・章・フィラー等、**文字起こしを読むLLM工程は主ループ(Opus)に載せない**。
**Sonnetサブエージェント（Agentツール `model:"sonnet"`）に投げ、入出力はファイルのみ**。サブエージェントの
返り値は件数1行だけ（本文は返さない）。1Mコンテキストは設定不可のため通常Sonnetで可。

例（字幕）: `subtitle prepare-captions` で `caption_input.tsv` 生成 → Sonnetサブエージェントが
`.claude/skills/caption-summarizer/SKILL.md` に従い `caption_decisions.json` を Write → `subtitle apply-captions`。
出力スキーマは `{"captions":[{"utt":<窓idx>,"text":...}]}`（apply-captions互換）。BOMは付けない。

- **破綻しない有界化設計（長尺/弱モデル向け・ユーザー考案）**: 章解析時に**全体フロー要約**(`flow_summary.txt`)を作り、
  字幕付けの各呼び出しには「要約＋章(時刻付)＋OCR＋対象期間」だけを渡し、必要なら `subtitle transcript-range` で
  局所の文字起こしを取得させる＝**各LLM呼び出しの入力を有界化**（全窓を一度に詰め込まない）。これで弱いモデルでも完走できる。
- **caption_input.tsv の構造注意**: 窓レコード（`<idx>\t...`）の後に「画面OCRコンテキスト節（`# --- 画面テキスト(OCR) ---`）」が付く。
  行ベースで分割する時は **`^\d+\t` の行だけが窓**（OCR節を窓と数えない）。
- **Haiku vs Sonnet 実測**: 同一115窓で Haiku は件数が不安定（46〜114件・フィラー判断が振れる）、Sonnet は 99件で内容も均一。
  **字幕付けは Sonnet が妥当**（要約自体は元々 Sonnet 規律）。Haiku は単独全文だと弱い＝有界バッチ前提。
- **本番字幕生成済(2026-06-27)**: `data/2026-06-04` で prepare-captions(115窓)→Sonnetサブエージェント(caption-summarizer)→apply-captions。
  **84字幕**（要約83＋免責1・冒頭挨拶窓は飛ばし utt5開始）。話者 mossan67/Taniguchi16・色は自動割当（寒/暖）・免責(話者None)はblue解決。`subtitle_speaker_colors`未上書き。

---

## 5. 残作業（外部キー/認証が要る or 未実装）

- **[G] イントロ**: 文生成→AIVis(TTS)→キャラ画像→DomoAIリップシンク→ロゴ/ピンク字幕/ジングル合成。
  - ✅ **DomoAI クライアント実装＋1秒テスト成功**（`publish/domoai.py`・$0.06）。`POST /v1/video/talking-avatar`（image/audio=base64≤10MB or upload→domoai_uri・seconds1-60・aspect_ratio・model talking-avatar-v1）→`GET /v1/tasks/{id}`ポーリング→`output_videos[].url`(8h失効)。出力720p/16:9・口元が音声に同期。
    **罠**: ① 正ホストは **`api.domoai.com`**（.appは404）② **Cloudflare error1010** で urllib 既定UAが弾かれる→**ブラウザ風User-Agent必須**（生成・DL両方）。
  - ✅ キャラ画像=nano banana（サムネと同一キャラ・参照渡しで一貫）。AIVis=SBV2 venv 在(`repos/sbv2/Style-Bert-VITS2`)・novtube `dub_local/server.py`移植元。ジングル/ピンク二重枠字幕/ロゴ済。
  - ✅ **イントロ生成パイプラインを実機で完走(2026-06-28)**＝① 文章=SDD L176テンプレ「こんにちは。今日は〜してみました。詳しくは本編でどうぞ」調（CVPR回向け・最終8.93s文）
    ② 話者=**noa(AIVIS)**＝novtube SBV2サーバ（`github/novtube/tool/dub_local/server.py` を **`github/sbv2/Style-Bert-VITS2/venv` の python** で起動・:8123・`/synth {engine:AIVIS,voice_model_id:noa,voice_style_id:normal,texts}`・モデルは `github/sbv2/.../model_assets/noa`・neutral style=`normal`）。**返り `duration_ms` で尺チェック→10s超(17.2→11.5)は文を詰めて再生成→8.93s**。
    ③ 開始フレーム=**`github/novtube/web/assets/noa_a-BZYVLRjH.webp` を参照画像に渡し「絵柄・キャラ同一性維持」を制約**、格好/シチュのみ初夏6月（紫陽花/新緑）。nano banana2=**gemini-3-pro-image**。chibi/マスコット画像は参照に使わない。
    ④ DomoAI talking-avatar で 9s 動画（$0.54）→ `noa_intro.mp4`（720p）。
  - ✅ **CLI/skill 適切分離(2026-06-28)**: 決定的処理＝`publish tts`(AIVis `aivis.py`)/`publish character-image`(`character.py`・参照+同一性)/`publish lipsync`(DomoAI)。
    判断（台本/季節服装の非重複/尺ループ/目視QA）＝**`.claude/skills/intro-builder`**。sub-agentは重い台本時のみ。＝「イントロはClaude無しでは無理」を正しい使い分けに。
  - ✅ **仕上げ合成 `publish intro-compose` 実装済(2026-06-28)**: 720p→FullHD＋**右上にロゴ(元色そのまま・パネル無し)＋本名フルネーム(縁取り)**(`overlay=W-w-28:24`)＋ピンク二重枠字幕＋ジングル(-20dB)。
    **キャラ名＝本名フルネーム**（`--char`→`mascot.md`の本名・`character.py: FULL_NAME`・noa=文月 乃亜）。**字幕は2行ずつのキューに分割**(`script_to_subtitles`＝`wrap_script`を助詞境界折返し→2行/キュー→文字数比で尺配分＝同時表示は2行)。
    ⚠️ロゴは**白再着色しない**（カラフルなので潰れる）。LOGOパスは `parents[3]`（repoルート）。実機で完成イントロ生成・目視OK。
- **[K] YouTube投稿**: **コード実装済**（`publish youtube <edl> --video <mp4>`・`publish/youtube.py`）。
  body組み立て(`build_video_resource`・title100字/desc5000字トリム・categoryId28・privacy private既定)は純関数でテスト済。
  **既定 dry-run でキー無しでもリクエストJSONを検証生成**（本番 `youtube_upload_request.json` 確認済）。
  実投稿(`--no-dry-run`)は **.env の WWEDIT_YT_CLIENT_ID/SECRET/REFRESH_TOKEN ＋ google-api-python-client が必要**（遅延import・無ければ手順提示で停止）。
- **[L] サムネ/概要欄**: ✅ **両方実装済**。
  - 概要欄=`publish description <edl>`（タイトル＋要約＋`youtube_chapter_lines`＋AI免責＋チャンネル・LLM別生成を`--*-file`で）。本番生成済。
  - **サムネ=`publish thumbnail <edl> --top "[CVPR2026] 最新AI論文" --bottom "[動画超解像]・[3D復元]を一気に解説！"`**（`publish/thumbnail.py`）。
    **実投稿サムネの傾向に準拠**（@mossan_hoshi のサムネ8枚を実取得・分析）= **テキスト主役の上下太字バナー（`[語]`で赤/黄強調・極太縁取り）＋中央=ゆる×AIイラスト（ロボ/論文/3D/動画モチーフ・ちいかわ系）**。
    高コントラスト・ブランドカラー無し・下品でなく内容アピール（20〜50代男性向け）。**萌えアニメ娘案は撤回**。
    背景は nano banana（`DEFAULT_ART_PROMPT`）、テキスト/ロゴは PIL 後合成（日本語文字崩れ回避）。`GEMINI_API_KEY` は secret manager(GCP `cosmic-talent-450413-f9`)→.env 設定済。`--art` で既存背景なら無課金。既定 gemini-2.5-flash-image（gemini-3-pro-image で高品質）。
- **[E] 専用クロップモデル**: ✅ 学習完了・床超え・**CLI化完了**（`framing crop-train`/`crop-apply`・§2）。製品反映は `compose --framed`。
  残=上積みのみ（解像度↑/解凍ブロック増/疑似ラベル/DINOv3）。
- **[J] fcpxml**: ✅ **一通り完了**。カット＋話者音声＋字幕＋framing(crop=adjust-transform)＋**BGM(音楽レーン・adjust-volume=cue.gain_db)**。
  ※話者の loudnorm は適応的(レンダ時測定)＝静的fcpxml値にできず話者は素レベル（BGM相対音量のみ表現）。BGM cue.start/end は出力時刻解釈。
  〔旧記述〕カット＋話者音声＋字幕(title・話者色・出力時刻再マップ)＋**framing(crop=adjust-transform・実装済**
  ＝部分矩形を中心合わせで scale=W/bw 拡大・position は出力px原点中央＋Y上。1クリップ1フレーミング・キーフレーム無し前提）。
  実測: crop適用済EDLで 179/211クリップに変換付与(scale1.14〜1.67)。**音量/BGMのfcpxml表現は未**。座標系はFCPX準拠＝Resolveで要目視確認。
- **near-complete動画を実生成(2026-06-27)**: `data/2026-06-04/framed_subbed_full.mp4`（**全編23.4分・framed＝継続学習モデルのcrop＋字幕84件＋話者別整音**・1920×1080・130MB）。
  残るは BGM（素材待ち＝`assets/`はlogoのみ）・[G]イントロ/[K]YouTube/[L]サムネ（APIキー待ち）。冒頭挨拶部のcrop寄りは編集ツール→harvest→再学習で改善可。
- 編集ツール: 主要編集は実装済（§7）。次候補=投稿単位の範囲分割/章割当、loadingクリップのプレビュー精緻化。
- 調整余地: 字幕の窓長/表示秒、framingのspan較正（収録別）、BGMのセクション別ジャンル切替
  （現状は本編一括で1フォルダ＝1ジャンル連続。章ごとにフォルダを変える拡張は未実装）。

---

## 6. 環境・品質

- uv 管理。extras: cv/omni/ocr/webapp/**framing-train(timm)**。torch2.6.0+cu124。`UV_LINK_MODE=copy` で opencv hardlink破損回避。
  ⚠️ `uv sync --extra X` 単体は他extraを外す。複数同時(`--extra a --extra b ...`)で同期すること。
- OCR=**RapidOCR-ONNX**（PaddleはWindows DLL衝突で不可）。STT=WhisperX。動き=codecサイズ（PySceneDetectではない）。
- テスト: `uv run pytest -q`（**160緑**）。lint: `uv run ruff check src tests`。editor.html のJSは `node --check` で構文検証。
- **[M4] publish群**: `publish/{description,thumbnail,youtube,domoai}.py`＋`publish/cli.py`。GEMINI(nano banana)＝secret manager→.env。DomoAI罠は§5[G]。
- ✅ **httptools 修復済(2026-06-28)**: `uv pip install --force-reinstall --no-deps httptools` で `HttpRequestParser` 復活＝webapp は通常 uvicorn で起動可（h11 回避はもう不要・h11 でも可）。
  〔教訓〕起動中サーバが `.pyd` をロックしていると `uv sync` で入替失敗→無応答になる。サーバ停止中に再インストールする。
- **運用セットアップ状況(2026-06-28)**: `WWEDIT_YT_CLIENT_ID/SECRET/REFRESH_TOKEN` を secret manager(`GOOGLE_PUBLISHING_*`)→.env に設定済＋`google-api-python-client`/`google-auth-oauthlib` 導入済＝**実YouTube投稿(`publish youtube --no-dry-run`)が可能**。
  WhisperX(STT)は**このvenvに直import**（`transcribe/stt.py`）＝新規収録の文字起こしには要導入（既存 `data/2026-06-04` は処理済）。RapidOCR/torch/timm は導入済。
- 検証用: 本番EDLは触らず **コピーEDL**で編集検証（コピー先は揮発するTempスクラブパッド＝再起動で消える。再生成は `cp data/2026-06-04/edl.json <tmp>`）。
- レンダ結果プレビューは**静的mp4不要**になった（§7・canvas動的合成）。古い `long_v10*.mp4` 依存は撤廃。

---

## 7. タイムライン編集ツール（自前NLE）の詳細【2026-06-27 大幅拡張】

**起動**: `uv run wwedit edit serve <edl> --port 8800`（**`--preview` mp4 は不要**＝レンダ結果は動的合成）。
※ httptools 破損中は CLI でなく `uvicorn.run(create_editor_app(edl), http="h11")` 起動で回避（§6）。
**設計**: 編集はEDL(SSOT)へ非破壊保存し `correction_log.jsonl` へ全修正を追記＝M5学習データ。

- **レンダ結果＝動的合成（重要・静的mp4依存を撤廃）**: 右ペインは **canvas**。tick毎に**ソース動画 vid の現フレームを
  `drawImage` で framing crop して描画**＋字幕をEDLからライブ重畳＋loading区間はプレースホルダ。**エンコード/ffmpeg不使用＝軽量**。
  EDL=SSOT・レンダラは再生成のみ、の原則どおり。無変化フレームは再描画スキップ。
- **API（`webapp/editor.py`・変更時は要再起動）**:
  - `GET /api/timeline`・`GET /api/transcript`・`GET /media/source`(Range対応)。
  - 字幕: `POST /api/subtitle`(追加)/`/api/subtitle/{idx}`(編集)/`/api/subtitle/{idx}/merge`(隣接結合・内容は隣接側)/`/api/speaker-color`。
  - カット: `POST /api/segment/{id}`(invalid切替/境界移動)・`/api/segment/split`(at分割)・**`/api/segment/cut-range`(start,end範囲を非破壊カット・端が区間途中なら分割)**。
  - 調整(framing): `POST /api/framing/{idx}`(kind/範囲/label/warning/**bbox(px x,y,w,h)/clear_crop**)・`/api/framing/split`・`/api/framing/{idx}/merge`。
  - `POST /api/chapter`/`/api/chapter/{idx}`/`/api/postunit/{idx}`。
- **トラック順（上から）**: **投稿 → 章 → Cut → シーン(framing種別) → 調整(crop枠) → 字幕 → BGM**。
  - **調整トラック**=各 framing 区間の crop 状態(`crop W×H`/`全画面`)。選択すると**ソース映像に crop bbox を橙枠で重畳**し、
    **本体ドラッグ=移動／角ハンドル=リサイズ(16:9維持)**で編集→即保存。全画面区間も全画面サイズの編集可能枠を出し、角を縮めると crop 作成。
    crop overlay は**再生ヘッド位置の区間に追従**（選択固定でなく）。
- **ショートカット**:
  - `J/K/L`=逆/停/順（**2倍以上はシークステップで最大32x**）・`←→`=1F・`Space`=再生。
  - `↑↓`=**選択中アイテム種別の区切り**へ移動（未選択=カット基準）＋**選択も追従**（移動先から始まる項目／無ければ手前）。
  - `Ctrl+←→`=**同種の前後アイテムへ選択移動**。`Alt+←→`=**隣接(隙間なく接する)同種と結合**(字幕/調整・多重発火ガード有)。
  - `I/O`=in/out・**`Del`=In/Out範囲を非破壊カット**・`B`=分割（**選択が調整なら framing 分割**/それ以外はカット分割）・`N`=スナップ・`+/-`=ズーム・`Ctrl+Z`/`Shift`/`Ctrl+Y`=Undo/Redo。
- **マウス**: タイムライン**ドラッグ=スクラブ**（前シーク完了で最新位置へ追いシーク＝フレーム/枠/再生ヘッド同期）。
  **ホイール=再生ヘッド移動／Ctrl+ホイール=ズーム**(カーソル直下固定)。**投稿/章タップ・選択中再クリックは冒頭にシークしない**。
- **高速再生の負荷対策**: 1x=通常デコード再生／**≥2xはシークステップ(最大32x・playbackRate非依存)**。**≥2xはレンダ描画・字幕重畳・crop枠DOM更新を凍結／テロップは~250ms間隔**。
- **継ぎ目の整合**: `framingAt`/字幕検索は**半開区間[start,end)**＝区間の継ぎ目は「そこから始まる項目」を表示（前項目ズレを解消）。`seeked`でレンダ即再描画。
- **最小ズーム動的**: スライダ左端=全体が表示幅の約半分に収まる縮尺（尺長から算出・固定px/sでない）。
- Undo/Redoは前後値スタックでAPI再POST（分割/結合/cut-range など構造変更時は履歴クリア）。JS構文は `node --check` 検証。

---

## 8. 新規収録→投稿の通し手順（RUNBOOK）

**この通し手順は `auto-edit` 司令塔スキルが自動駆動する**（ユーザーは判断ゲート3点G1/G2/G3のみ）。以下は各工程の中身。
新しい収録1本を投稿可能動画にする**実行順**。各行頭の記号＝自動化レベル。
`[CLI]`=コマンド一発 / `[LLM]`=Sonnetサブエージェントを手動dispatch / `[外部]`=外部API課金 /
`[GPU]`=GPU推論(事前VRAM確認) / `[手]`=人手/未CLI化(都度スクリプト)。EDL=`data/<date>/edl.json`。

0. **前提**: `.env`（`GEMINI_API_KEY`/`DOMOAI_API_KEY`/`WWEDIT_MASK_TERMS`/`WWEDIT_SUBTITLE_NAME_MAP`、投稿時`WWEDIT_YT_*`）。
   `uv sync`（extras は複数同時指定）。GEMINIキーは secret manager から（[[external-assets-and-keys]]）。`UV_LINK_MODE=copy`。
1. `[CLI]` **ingest** 取り込み・正規化。
2. `[CLI][GPU]` **transcribe**（WhisperX word単位）→ `utterances`。
3. `[CLI][LLM]` **cut**（無音=`auto-vad --refine`動的閾値・フィラー=filler-selectorスキル・NGワード=`cut ngwords`＝.env `WWEDIT_CUT_NGWORDS` 言及発話まるごと）→ `segments`。
4. `[CLI][GPU軽]` **framing**: `scenes` → `classify-motion` → **`crop-apply`**（学習済`crop_model.pt`でbbox書戻し）。保守運用は `assign`。
5. `[CLI][LLM]` **chapter**: `screen-text`(OCR) → `prepare` → chapter-detectorスキル → `apply` → `youtube`(章txt)。
6. `[CLI][LLM]` **subtitle**: `prepare-captions` → caption-summarizerスキル(Sonnet) → `apply-captions`。
7. `[手]` **edit serve** で確認・手修正（任意）→ `[CLI]` **`framing harvest-corrections`** → 定期 `[GPU]` **`crop-train --extra-root data/framing_corrections`**（継続学習）。
8. `[CLI]` **compose video** `--framed --subtitles --audio speakers --bgm "D:/Users/sackn/Videos/wakuwaku/assets/sounds/bgms/<genre>"`
   `--eyecatch --eyecatch-jingle-dir "D:/Users/sackn/Videos/wakuwaku/.../jingle"`
   → 本編 framed＋字幕＋整音＋BGM(-34LUFS)＋**[H]全章冒頭2秒アイキャッチ**(generative art＋ランダムジングル) の mp4(`*_ec.mp4`)。
   挿入で章時刻がずれるので **`*_ec_chapters.txt`(補正済み)** を概要欄へ回す。（BGMジャンルは収録に合わせ選ぶ）
9. `[skill][外部][GPU]` **[G] イントロ** ＝ **`intro-builder` スキル**を実行（Claudeが台本/服装非重複/尺/QAを判断し下記CLIを順に叩く）:
   `[CLI]` SBV2起動(github venv) → `publish tts`(台本→wav・実尺≤10s確認・超過なら台本詰めて再実行) →
   `publish character-image`(`<id>_a*.webp`参照＋同一性・季節/服装は[[intro-generation-log]]で非重複・目視QA) →
   `publish lipsync`(DomoAI・seconds=音声尺・**$0.06/秒**・QA後のみ) → `publish intro-compose`(720p→FullHD＋ロゴ/名＋ピンク字幕全文＋ジングル)。
   **1収録に投稿が2件以上(`post_units`≥2)なら手順9以降を `--post-unit-index N` で単位ごとにループ**（上流は1回）。
10. `[CLI][LLM]` **[L] サムネ/概要欄**: `publish thumbnail --top/--bottom`（背景=nano banana）／`publish description`（title・summaryはLLM別生成→`--*-file`）。**アイキャッチ挿入時は `--chapter-lines-file <*_ec_chapters.txt>`** で補正章時刻を反映。
11. `[CLI][外部]` **[K] 投稿**: `publish youtube --video <mp4>`（既定dry-run）→ キー有りで `--no-dry-run`。`publish/fcpxml` は記録/緊急手修正用。

---

## 9. 再現性監査（2026-06-28・正直な現状）

**問い**: 日常的に新規収録から再現性high-qualityで生成できる状態か？ → **要素は一通り検証済だが、ワンコマンドのターンキーではない。** 毎回 人手・LLM手dispatch・設定記憶 が介在し、抜け漏れ余地が残る。テスト160緑・lint緑。

**再現性リスク（重大度順）**:
- 🟡 **[G]イントロ＝CLI/skill 適切分離済（2026-06-28）**: 決定的処理を `publish tts`/`publish character-image`/`publish lipsync`（`publish/{aivis,character,domoai}.py`）にCLI化。判断（台本執筆/季節服装の非重複/尺ループ/目視QA）は **`intro-builder` スキル**が担い CLI を順に叩く。sub-agentは重い台本時のみ。→ 残=**`intro-compose`（仕上げ合成）未実装**＋スキルのend-to-end通し検証（個別CLIは検証済）。揮発scratchの一回スクリプトは役目終了。
- 🟢 **イントロ仕上げ合成＝実装済(2026-06-28)**: `publish intro-compose`（`publish/intro_compose.py`）＝720p→FullHD＋左上ロゴ/キャラ名(PILバッジ)＋**ピンク二重枠字幕で台本全文**(`ass.py` intro)＋ジングル(-20dB amix)。実機で `noa_intro.mp4`→FullHD完成イントロ生成・目視OK。字幕折返しは `wrap_script`（句点優先）＝微調整余地。
- 🟢 **1収録→複数投稿(post-unit)＝本番対応済(2026-06-28)**: `edl/postunit.py`（`post_unit_ranges`=kept∩単位スパン／`post_unit_chapter_lines`=単位内出力時刻／`n_post_units`）。
  `compose video --post-unit-index N`（`compose_kept` に `ranges` override・字幕/framing/BGMも単位内整合）／`chapter youtube --post-unit-index`／`publish description --post-unit-index`（出力は `*_p<N>`）。`auto-edit` は手順9以降を post_units ループ（上流1回）。テスト `tests/test_postunit.py`。
- 🟢 **[H] チャプター冒頭アイキャッチ＝実装済(2026-06-28)**: 全章冒頭に2秒の generative-art クリップ（`publish/eyecatch.py`＝ffmpeg `gradients`動勾配＋`geq`流動プラズマ(softlight)＋微ノイズ＋ヴィネット・**seedで毎回見た目が変化**・curated配色10種・タイトルカード＋右下ロゴ）＋**章ごとseedでランダム選曲したジングル**を挿入。`compose/eyecatch_insert.py` が本編mp4を章境界で分割しconcatフィルタで再連結（本編filtergraphは非改変）、挿入で生じる章時刻ずれは `shifted_chapter_lines`（純関数）で補正し `*_ec_chapters.txt` に出力。`compose video --eyecatch --eyecatch-jingle-dir`／`publish eyecatch`／`publish description --chapter-lines-file`。実機スモークで尺18s(本編12+EC3×2)・章補正一致を検証。テスト `tests/test_eyecatch_insert.py`・`test_publish.py`。
- 🟡 **マスター・オーケストレータ＝`auto-edit` スキル実装済（2026-06-28）**: 収録→投稿を §8 RUNBOOK 通りに自動駆動し、CLI呼び出し・LLM工程のスキル/サブエージェントdispatch・目視QA・継続学習をClaude側で完結、止まるのは**判断ゲート3点（G1素材/キー不足・G2編集確認・G3投稿前承認）**のみ（サムネ/イントロは生成して見せ自動進行）。→ 残=**新規収録1本での end-to-end 通し検証**（各工程CLI/スキルは個別検証済）。
- 🟡 **LLM工程のdispatch**: filler/chapter/caption は専用スキル（filler-selector/chapter-detector/caption-summarizer）＋`auto-edit` が順に dispatch する設計。概要欄title/summary・サムネ文言・イントロ台本は `auto-edit`/`intro-builder` 内でClaudeが書く。＝手起動依存は解消（通し検証は残）。
- 🟡 **手作業の文言**: サムネtop/bottom・イントロ文章は人手（自動生成の口は要LLM接続）。
- 🟡 **設定/罠の記憶依存**（[[external-assets-and-keys]][[no-heavy-gpu-without-consent]]に集約済だが多い）: DomoAI=正ホスト`api.domoai.com`＋Cloudflare対策UA／SBV2サーバ起動はgithub venv／イントロ音声≤10s尺ループ／BGM・jingleはVideos配下／`UV_LINK_MODE=copy`／httptools=h11。
- 🟡 **BGMセクション別ジャンル切替 未実装**（現状 本編一括1ジャンル）。
- 🟢 **httptools破損**（webappのみ・`http="h11"`回避済・要再インストール修復）。
- 🟢 **検証はコピーEDL**（揮発Temp・再生成可）。

**結論（2026-06-28 更新）**: **ターンキー化のアーキテクチャは概ね揃った**＝`auto-edit` 司令塔スキルが §8 RUNBOOK を自動駆動し、判断ゲート3点以外はClaude側で完結（CLI＋4スキル＋サブエージェント）。
残るは **新規収録1本での end-to-end 通し検証**のみ（個別工程＋イントロ仕上げ＋post-unit分割は実装・検証済だが、`auto-edit` で頭から1本通したことはまだ無い）。次の新規収録が来たら `auto-edit` 実走で確証すれば、「最初に `auto-edit` に投げる→判断ゲート以外は自動」が実運用で立つ。
（残ポリッシュ: イントロ字幕の折返し最適化・BGMセクション別ジャンル切替・no_crop二頭ヘッド等は品質上積みで運用ブロッカーではない。）
