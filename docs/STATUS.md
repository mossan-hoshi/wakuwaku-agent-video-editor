# 実装状況（IMPLEMENTATION STATUS）

このファイルは**実装の現状・確定した設計判断・パイプライン実行手順**を残す唯一の状況ドキュメント。
コンテキスト圧縮やセッション跨ぎでも、こことコード+テストだけで状態を復元できることを目的とする。
（最終更新: 2026-07-24）

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
   **1話者につきトラックは最大2本**＝マイク（発話）と**PC音声**（画面共有で流した音楽など）。
   Zoomは PC 音声を「その人の表示名の別枠」として書き出すので、**同名で連番違いの2本**になる。
   `ingest/tracks.py` が**連番の小さい方を発話・それ以外を `is_desktop_audio`** と判定する
   （両参加者がそれぞれ持ちうる）。PC音声は**文字起こししないが合成には混ぜる**（§13）。
2. **transcribe**: WhisperX で word単位STT → `edl.utterances`（`transcribe`）。
3. **cut**: 無音/フィラー/NGワードカット → `edl.segments`（`cut`。無音=`auto-vad --refine`動的閾値、フィラー=filler-selectorスキル、NGワード=`cut ngwords`＝.env `WWEDIT_CUT_NGWORDS` に言及した発話をまるごとカット）。
   ※**画面**に写ったNG語はカットではなく `privacy ng-mosaic` でモザイク（§12.3）。
4. **framing**:
   - `framing scenes <edl>`: codecサイズ動き検出で安定区間 → `edl.framing`（static / pending）。
   - `framing classify-motion <edl>`: pending を オプティカルフローで loading(画面切替) / pending(動画) に分類。
   - `framing assign <edl>`: **既定=保守的に全画面(no_crop)**。`--aggressive` で OmniParser判定+固定箱（過剰crop注意）。
   - **`framing crop-apply <edl>`**: 学習済み専用モデル(`crop_model.pt`)で static 区間の代表フレームを推論し
     `framing.bbox` へ一括書き戻す（既定 device=cpu・数十秒）。`compose --framed`/編集ツールの crop枠に反映。
     **既定で「全画面を残さない」**＝bbox が付かなかった区間へ上下左右1割の既定トリムを入れる（§12.2）。
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
   - `--bgm-avoid-desktop`（**既定OFF・回ごとの判断**）: PCシステム音が鳴っている間だけ
     BGM を落とす。**その回の音そのものを聴かせる時だけ**付ける＝ #103 は音楽生成の
     聴き比べで、下に BGM があると比較にならなかった（2026-08-06 指示）。
     **常時ONにはしない**（普段はPC音声の上にも BGM を敷く）。
     ⚠️ **いきなり切らない**。`bgm_mute_expr` が**フェード付きの音量エンベロープ**
     （`volume=volume='min(clip(…))':eval=frame`）を作る。区間の 0.6秒手前から線形に
     0 まで下げ、終わってから 0.6秒かけて戻す（「いきなり消えると不快」）。
     区間は `_bgm_mute_spans` が `desktop_active.json` から出し、前後 0.3秒の余白を付ける。
     実測（sine 7秒・区間2-4秒）: 区間内 RMS 0.000 / フェード中 0.024 / 区間外 0.088。
     式のカンマは `\,` でエスケープする（フィルタ引数の区切りと衝突するため）。
     ⚠️ ワープ後の EDL では desktop トラックの `voice_path`（ワープ済みPC音声）を測る
     ＝素材のまま測ると時間軸がずれる。
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
  - 概要欄=`publish description <edl> --agenda "<テーマ>" --hashtags "#…" [--links-file] --chapter-lines-file <*_ec_chapters.txt>`。**実投稿フォーマット準拠**（2026-06-29 確定）＝`Agenda「」`→関連リンク→`#タグ`→`00:00 - start` 以下 `MM:SS - ラベル`。**要約文/AI免責/チャンネルURL/タイトル再掲は付けない**（旧独自フォーマットは廃止）。既存概要欄はYouTube Data API(readonly)で取得し分析（[[youtube-description-format]] [[youtube-api-scopes]]）。タイトルは `【 <…> # NN わく枠べんきょ会】`。
  - **サムネ=`publish thumbnail <edl> --char noa --prompt "<文字・配色・構図・表情まで記述>"`**（`publish/thumbnail.py: generate_thumbnail`）。
    **方針確定（2026-06-29 ユーザー指示）= nano banana 2(gemini-3-pro-image) 一発生成**。キャラ(乃亜の立ち姿`<id>_a*.webp`を参照でスタイル/同一性固定)・背景・**日本語タイトル文字まで一括でモデルが描く**（gemini-3-pro-image は日本語タイポも崩れにくい）。文字サイズ階層・強調色もプロンプトで指定。
    **旧「背景だけ生成＋PIL上下帯合成」は廃止**（`compose_banners`/`compose_title_logo` は legacy 残置）。[[thumbnail-oneshot-nano-banana]]。
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
7. `[手]` **edit serve** で確認・手修正（任意。カット/framing/章/字幕＋**重ね(画像/テキスト/モザイク)** を配置）
   → `[CLI]` **`framing harvest-corrections`** → 定期 `[GPU]` **`crop-train --extra-root data/framing_corrections`**（継続学習）。
8. `[CLI]` **compose video** `--framed --subtitles --audio speakers --chapter-ribbon --bgm "D:/Users/sackn/Videos/wakuwaku/assets/sounds/bgms/<genre>"`
   `--eyecatch --eyecatch-jingle-dir "D:/Users/sackn/Videos/wakuwaku/.../jingle"`（`--overlays` は既定ON＝EDL.overlays を最上位に焼込）
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

---

## 10. 実走で判明した環境現実（2026-07-16・初の新規収録 end-to-end 実走）

`auto-edit` を新規収録1本(`data/2026-07-16`)で頭から通した。**G3(投稿承認)手前まで自動到達**（本編`final.mp4`＝イントロ＋本編・サムネ・概要欄・dry-run完成）。詳細な再開手順は **`data/2026-07-16/RUN_STATUS.md`** に集約（このファイルは恒久的な環境事実のみ抜粋）。

- ⚠️ **背景Bashタスクがこのマシン/セッションで一斉killされる**: `run_in_background` は数分でstatus=killed（負荷ゼロのgrep監視ループも同時に死ぬ＝OOMではなく刈り取り）。**重い工程はフォアグラウンド(timeout≤600s)で回す**。**実壁時計**: 本編base compose=**506s**（ffmpeg encodeは3.66xだが起動/BGM整音のオーバーヘッド込みで実質2.8x・**600s制限にかなり近い＝要注意**）、アイキャッチ挿入=**304s**。分割して各々600s以内に収めた。transcribeも背景では4回killされ、フォアグラウンド faster-whisper(約8分)で完走。
- ⚠️ **RAM 17GB・空き3〜4GB＋ユーザーのpiper学習(GPU)並走で WhisperX large-v3 がOS強制終了**。→ **`transcribe run --backend faster-whisper --compute-type int8_float16`** に切替えて完走（アライメントモデル不要でRAM小）。副作用: faster-whisperは日本語1文字トークン化で `cut fillers-prepare` が**候補0件**（フィラーカット無し＝過剰カット無しの安全側）。RAM/VRAM確保できる時はWhisperXが本命のまま。ユーザーのGPUジョブは絶対killしない（重処理前に `nvidia-smi`＋空きRAM確認）。
- ~~⚠️ **SBV2 dub_local 合成サーバ(`tool/dub_local/server.py` :8123) がディスク上に存在しない**~~ **【2026-07-23 訂正: §11参照。`C:/Users/sackn/repos2/novtube4` に存在する】**（novtube作業ツリーが空＝`publish tts`/`aivis.py` は前提サーバ不在で不可）。→ 代替=**SBV2 Python API 直叩き**（`data/2026-07-16/sbv2_direct_synth.py` 参照。github SBV2 venv python・model_assets/noa・style=`normal`・JP-Extra・BERTローカル）。**名前の漢字はG2Pで読めず脱落するのでTTS用テキストはひらがな**（「乃亜」→「のあ」）。字幕表示は漢字でOK。
- ~~⚠️ **novtube → `novtube-voicebox` にリネーム**~~ **【2026-07-23 訂正: §11参照。素材は `repos2/novtube4/web/assets`(6キャラ)。voicebox側は2キャラのみで不足】**。キャラ素材/mascot.mdは `C:/Users/sackn/github/novtube-voicebox/web/...`。`character.py`/`thumbnail.py` は環境変数 **`WWEDIT_NOVTUBE_ASSETS=C:/Users/sackn/github/novtube-voicebox/web/assets`** で参照先差し替えが要る（既定は旧空パス`github/novtube/web/assets`）。noaフルアート参照=`noa_a-BZYVLRjH.webp`。
- ⚠️ **YouTube認証(readonly)が `RefreshError: credentials do not contain the necessary fields` で失敗**。`--no-dry-run` 実投稿前に検証（env値の空/scope/token）。必要なら `scripts/reauth_youtube.py` 再認証。dry-run(認証不要)は通過。
- ℹ️ **アイキャッチ挿入は `compose --eyecatch` を丸ごと再実行せずに、既存本編mp4へ `compose.eyecatch_insert.insert_eyecatches(...)` を直接適用可**（base再エンコードを省ける）。イントロと本編の連結CLIは無く手動（concat demuxer・**相対パスはconcatファイルの所在dir基準**・msys絶対パス`/d/...`はffmpeg不可・音声フォーマットを本編48k/stereoに揃える）。

---

## 11. 2回目の新規収録 end-to-end 実走（2026-07-23 収録・#101）と、その過程で入れた機能

`data/2026-07-23`（38.8分・25fps・話者 mossan-hoshi/Taniguchi）を頭から通した。**G3(投稿承認)手前まで到達**。

### 11.1 §10 の環境記述の訂正（重要・過去の記述は誤り）
- **novtube は消えていない**。`C:/Users/sackn/github/novtube` は**空**だが、実体は **`C:/Users/sackn/repos2/novtube{,2,3,4}`** の4本。
  いずれにも `tool/dub_local/server.py` と `web/assets`(6キャラ) がある。**最新=`novtube4`(2026-07-01)** を使う。
  `D:/Users/sackn/repos/novtube-voicebox` にも `web/assets` はあるが**2キャラのみ**なので使わない。
- **SBV2 合成サーバは使える**（§10 の「存在しない」は誤り）。起動時に **`SBV2_ROOT` の明示が必須**:
  ```
  cd C:/Users/sackn/repos2/novtube4/tool/dub_local
  DUB_LOCAL_SYNTH_PORT=8123 SBV2_ROOT=C:/Users/sackn/github/sbv2/Style-Bert-VITS2 <SBV2venv>/python.exe server.py
  ```
  既定の `SBV2_ROOT` は「novtube の2階層上の `sbv2/`」＝`repos2/sbv2`(不在)に解決され、`/synth` が **HTTP 501** を返す。
  `/health` の `sbv2_root` が `github/sbv2/Style-Bert-VITS2` ならOK。SBV2 venv/モデルは `github/sbv2/...` 側。
- キャラ立ち姿の参照先は `.env` の **`WWEDIT_NOVTUBE_ASSETS`**（現在 `C:\Users\sackn\repos2\novtube4\web\assets`）。
  **`publish/character.py` は `env_value()` 経由に修正済**（生 `os.environ` だと `.env` が効かず既定の空パスを見ていた）。

### 11.2 transcribe: WhisperX は使える（§10 の faster-whisper 退避は今回不要だった）
- WhisperX large-v3 **float16** で 3トラック完走（本命どおり文字レベル整列・19,172語・語長中央値0.080s）。
- ⚠️ **`--compute-type int8_float16` は使うな**。今回 8GB VRAM を空ける前に指定したところ、mossanトラックで
  105分経っても終わらず（GPU 100%張り付き）。**float16 で VRAM を空けてから回すのが正**。
- ⚠️ **whisperx は進捗を stdout に出さない**。stderr が無音でも**ハングではない**。生死判定は
  `uvx py-spy dump --pid <python子PID>` でスタックを見る（`generate_segment_batched` なら ASR 実行中）。
  90秒 `py-spy record` でフレームが散らばっていれば前進、単一フレーム100%張り付きならデッドロック。
- `align` extra（whisperx）が要る: `uv sync --extra align ...`。

### 11.3 framing: loading 検出0件は「仕様どおり」で異常ではない
- `classify-motion` の loading 検出は**過去回も一貫してほぼ0件**（05-14:0 / 06-04:0 / 07-16:1 / 07-23:0）。
- 構造上の理由: `scenes` が画面切替**で区間を分割する**ため、切替は「区間の**境界**」に現れる。
  一方 `classify-motion` は「区間の**内部**」をオプティカルフローで見るので、境界の切替を原理的に拾えない。
  加えて Farneback は全画面変化で流れ量を過小評価し、実測 spread は最大0.473（閾値0.6に構造的に届かない）。
- 実測: 区間境界93個のうち画素差>30が36件・>80が11件＝**切替自体は起きている**。改善するなら「境界の画素差」で判定する別実装が要る。
- `_extract_frame` は `subprocess.run(text=True)` がcp932で ffmpeg バナーを復号できず **UnicodeDecodeError を大量に吐くが無害**
  （読み取りスレッド内で完結し、PNG は正しく出る。3/3で検証済）。

### 11.4 今回追加した機能（コード＋テスト）
- **重ね（ユーザー配置オーバーレイ）**: `EDL.overlays`（新規・ソース時刻＋正規化座標で非破壊）、`compose/overlay.py`、
  `compose video --overlays`(既定ON)。種別は **画像 / テキスト / モザイク**。
  - テキスト: **字幕と同一の二重縁取り**（ASS 2レイヤー: L0=同色外枠 / L1=白1次枠＋色文字）。色はパレット4色 or `#RRGGBB`、
    サイズ・フォント・**揃え(左/中/右)**・**行間**（`line_spacing`。**1.0=枠込みで隣接行が接する寸前**＝被らない基準）。
    複数行は**行ごとに別イベント**にして `\pos` の y を自前計算（ASS既定の行送りだと二重枠が上下で被るため）。
  - 画像: 拡大率・不透明度。**D&D で再生ヘッド位置に配置**、複数選択時は**1枚5秒ずつ連続配置**（ファイル名順）。
  - モザイク: bbox形式。**方式**=pixelate(低解像度・既定)/gaussian、**形状**=rect(既定)/ellipse、**強さ**可変。
    `split`→`crop`→効果→元位置へ `overlay`(時刻 enable)。楕円は PIL 生成の楕円マスクを `alphamerge`。
    ⚠️ pixelate の拡大し直しは **`scale={rw}:{rh}` と実寸を明示**（`scale=iw:ih` だと縮小後サイズのままで領域が縮む＝一度踏んだ）。
  - **合成順序**: 映像 → framing → 字幕 → リボン → **モザイク → 画像 → テキスト**（モザイクは映像側にかかり、ユーザーの画像/テキストはその上に残る）。
- **エディタ(webapp)**: 重ねトラック（Cutの直上・**重なりは自動で複数レーンに分割**）、ステージ上でのドラッグ配置/モザイクの角リサイズ、
  **Ctrl+C/V でコピペ**（別ID・再生ヘッド位置へ／Undo対応）、**Delで削除**（Undoで完全復元＝削除APIが復元用ペイロードを返す）、
  **Alt+ホイールで縦スクロール**＋ルーラー固定、ズームスライダを**2段対数**（中央=既定倍率7px/s）、
  **重ねのスナップは他の重ねの端を最優先**（次点で従来の編集点）。
  `/api/overlay/upload` は **`/api/overlay/{idx}` より前に登録**すること（後だと "upload" が `{idx}` にマッチして壊れる＝踏んだ）。
  `UploadFile` は **module 直下 import**（`from __future__ import annotations` 下では関数内importだと FastAPI が注釈を解決できない＝踏んだ）。
  `python-multipart` を webapp extra に追加済。
- **framing の端ドラッグで隣接区間が追従**するよう修正（従来はシーンだけ動いて「調整」と食い違い、不連続データになった）。
  反転入力は segment と同様クランプ（400を返さない仕様に変更）。
- **イントロ字幕の折り返し修正**（`intro_compose._split_long`）: 英数字トークン(`ComfyUI`)を割らない・
  「です」の「で」で折らない(残り3文字未満の位置を選ばない)・助詞に「の」を追加。
- **`tests/test_editor_js_syntax.py`**: editor.html の埋め込みJSを `node --check`。
  JSが1文字壊れると**タイムラインが丸ごと表示されなくなる**のに Python テストでは検出できないため（実際に踏んだ）。

### 11.5 イントロ音声の読み（TTS）
- **読み用テキストはかな書き**。SBV2は英字を1文字ずつ読む（`ComfyUI`→シーオーエムエフワイユーアイ）。
  `ComfyUI`→**コミュファイユーアイ** / `MCP`→**エムシーピー** / `小ネタ`→**コネタ**(しょうねた と読まれる) / `のあです`→**ノアです**。
  読点で間を作れる。**字幕用テキストは正表記**（`--script` に渡す方）。詳細は `intro-builder` スキル手順3。
- **リップシンク後に読みを直す場合**は動画を作り直さず（$0.06/秒）、新旧音声を whisperx で単語タイムスタンプ化 →
  共通語をアンカーに区間表を作り → `trim`+`setpts` で区間ごとに伸縮 → 新音声を `-map`。
  無音区間（口が閉じている）で大きく吸収する。**逆再生の差し込みは発話中だと「口が喋り戻る」ので不自然**（±20%までは setpts が滑らか）。

### 11.6 YouTube の tags は「その回の内容から起こす」（固定既定タグは廃止）
- **`DEFAULT_TAGS`（勉強会/AI/コンピュータビジョン/論文紹介/わくわくべんきょ会）は固定値で、
  内容と照合されないまま付いていた**。#100 にもそのまま付いており、その回に CV も論文紹介も無い。
  → **タグは毎回その回の内容から決める**。固定既定に戻さないこと。
- `build_video_resource` の既定を **`tags_from_description(desc)`** にした
  （概要欄の**#ハッシュタグだけの行**から `#` を外して起こす・重複除去・API合計上限480字でカット）。
  ハッシュタグは毎回内容に合わせて決めるので、**タグも自動的に内容と一致**する。
- CLI: `publish youtube --tags "a,b,c"` で明示上書き（**ハッシュタグに載せない固有名を足したい時に使う**）、
  `--no-tags` でタグ無し。dry-run の表示に `tags=` を追加（何が付くか投稿前に見える）。
- テスト: `test_tags_from_description` / `..._respects_total_limit` / `..._tags_override`（`tests/test_publish.py`）。

### 11.7 サムネの API 設定を実装（`thumbnails.set`）
- `publish/youtube.py` に **`set_thumbnail(video_id, image)`** を追加。`publish youtube` は投稿後に
  `<date>/thumbnail.png` があれば**自動で設定**する（`--thumbnail-file` / `--no-thumbnail`）。
  投稿済み動画の差し替えは **`publish set-thumbnail <videoId> --image <png>`**。
- **2MB 上限**があり `publish thumbnail` の 2K PNG は超えるので、`_shrink_thumbnail` が
  1280幅 JPEG へ品質を段階的に落として収める（**元ファイルは触らない**・`<stem>_yt.jpg` を隣に作る）。
- サムネ設定に失敗しても**投稿自体は成功として扱う**（例外を握って手動設定を案内）。
- スコープは `youtube.upload` のままでよい（`thumbnails.set` は upload スコープで通る）。

### 11.8 イントロと本編の連結（`final.mp4`）
- イントロは **30fps/44.1kHz mono**、本編は **25fps/48kHz stereo** で規格が違う。concat demuxer で
  `-c copy` する前に**イントロ側を本編と同じ規格へ再エンコード**する
  （`fps=25,scale=1920:1080,setsar=1` / libx264 High@4.0 yuv420p / aac 48k stereo 192k）。9.3s なので安価。
- **章時刻はイントロ尺ぶんシフトが必要**。2026-07-16(#100) はシフトせず投稿しており**章が7秒早い**。
  今回は +9.32 秒シフト（切り捨て＝マーカーが章頭のわずか手前＝アイキャッチのタイトルカードが見える）。
- 検証は**実フレームを抜いて確認**する（`ffmpeg -ss <t> -frames:v 1`）。継ぎ目・各章マーカー直後の
  リボン/アイキャッチが期待どおりかを目で見る。

### 11.10 重ねのレイヤー順と座標系（2026-07-24 修正・**仕様の確定**）
投稿後にユーザー指摘で2件直した。どちらも「見えているものと出力が食い違う」種類のバグ。

**(1) モザイクは最上位ではない**。当初「モザイク＝最上位レイヤー」で作ったため、
チャプターリボン（収録日＋章名）や字幕まで一緒にぼけた。レイヤー順を下から
**映像/ローディング → ユーザー画像 → モザイク → 字幕 → チャプターリボン → テキスト重ね**
に変更。**モザイクが掛かるのは映像とユーザー画像だけ**で、文字情報/UI はその上に置く。
- `compose_kept` の合成段の並び替え＋エディタの `drawOverlaysToCanvas` も同順に。
- 回帰テスト `test_mosaic_is_below_text_ui_layers`（filtergraph 本文の出現順を検査。
  `subprocess.run` を差し替えて `-filter_complex_script` の中身を読む）。

**(2) 重ねの座標は「ソースフレーム基準」**（`Overlay.x/y/w/h`・確定仕様）。
編集ツールはソース映像の上に置くので、**素材の同じ場所に貼り付く**のが正しい
（モザイクは被写体＝隠したい画面領域を追従する）。合成は framing bbox で crop→拡大するため、
`compose.overlay.place_overlays` が **crop 区間ごとに出力ピクセルへ写像**する。
- crop の区切りは framed concat と**同じ規則**（keep区間ごとに中点の bbox・
  `output_crop_segments`。隣接同一 bbox は畳んでフィルタ数を抑える）。
  1つの重ねが crop 違いの区間をまたぐと、**区間ごとに別の配置へ分割**される。
- **`scale`/`size`/枠の太さ/`strength` にも crop 拡大率 `mag` を掛ける**
  （寄っても見かけの大きさ・粗さがソース基準で保たれる）。
- 画面外へ出た配置は落とす。はみ出したモザイク矩形は**内側へ寄せる**（欠けさせない＝
  隠し漏れを作らない側に倒す）。
- エディタ**左ペイン（配置UI）は元からソース基準で正しかった**。直したのは合成と
  右ペイン（`renderResult`／`ovMap`）。**この2つは同じ式にすること**（ズレるとプレビューが嘘になる）。
- 純関数 `place_overlays` / `output_crop_segments` にテスト6件
  （恒等・crop写像・画面外除外・区間分割・bbox畳み込み・mag のサイズ反映）。

⚠️ **投稿済みの #101 は修正前の合成**（リボン/字幕がぼけた版・座標も旧解釈）。
作り直す場合は `compose video --framed --subtitles --chapter-ribbon --overlays` から。

---

## 12. 3回目の実走（2026-07-06 収録・#102）で確定した仕様変更（2026-07-26・ユーザー指示）

いずれも**恒久仕様**。過去回の挙動とは変わるので、以前の記述より本節が優先。

### 12.1 アイキャッチの音＝音楽ジングル廃止 → のべつべ！キャラの「一言」ボイス
- `publish/eyecatch_voice.py`（新規）が **SBV2日本語モデルを持つキャラから章ごとにランダム選択**し、
  「つ～ぎ！」「さてと」「お楽しみに！」等の**短い一言を都度合成**する（`VOICE_LINES`・`NOBETUBE_VOICES`）。
  seed 決定的＝再レンダリングで同じ結果。**読みはかな書き**（SBV2は漢字/英字を誤読＝§11.5）。
- `generate_eyecatch(voice=..., voice_name=...)` は**イントロと同じ右上バッジ（ロゴ＋キャラ本名）**を出し、
  声が2秒に収まらなければ**尺を声に合わせて伸ばす**（語尾を切らない）。従来の右下ロゴは jingle 時のみ。
- `compose video --eyecatch` の `--eyecatch-voice` が**既定ON**。SBV2サーバ未起動なら
  `--eyecatch-jingle-dir` の音楽へ**自動退避**（アイキャッチ自体は必ず出す）。
- 実測: 合成1本 1.0〜2.5秒・章ごとにキャラと台詞が変わることを実機確認（priya/souta/suzu）。
  テスト `tests/test_eyecatch_voice.py`（キャラに表示名がある/読みがかな/seed決定的/章ごとに変化）。

### 12.2 フレーミングは「全画面(no_crop)を出さない」＝既定トリム上下左右1割
- `framing/default_trim.py`（新規）＝ bbox が付かなかった区間へ**中央80%×80%**（16:9維持）を書き込む。
  `framing crop-apply` が**既定で自動実行**（`--no-default-trim` で従来の全画面）。単体 CLI は `framing default-trim`。
- EDL へ**明示的に書き戻す**ので編集ツールの「調整」トラックに枠が出て G2 で手直しでき、そのまま学習データにもなる。
- 実測(2026-07-06): モデル推論 66区間＋既定トリム 69区間＝**135区間すべてに crop 枠**（全画面ゼロ）。
  テスト `tests/test_default_trim.py`。

### 12.3 画面のNGワードは**カットせずモザイクで隠す**
- `privacy ng-mosaic <edl>` ＝ 画面OCRで `.env` の `WWEDIT_MASK_TERMS` ∪ `WWEDIT_CUT_NGWORDS` に当たった箇所へ
  **大きめのモザイク overlay を自動付与**（`privacy/ocr_mosaic.py`）。本編の流れを切らないためカットはしない。
  座標は**ソースフレーム基準**（§11.10の確定仕様）で `EDL.overlays` に入り、G2 で位置/サイズを手直しできる。
- 検出boxは `margin=0.8`（四方に寸法の8割）＋最小6%で**ざっくり大きめ**に広げ、ヒットした代表フレームの
  **framing区間まるごと**を覆う（区間内は同じ画面が写り続けるため。サンプル±padでは隠し漏れる）。
- OCRはフル画面（crop で見えなくなる想定に頼らない＝G2で crop を広げても漏れない安全側）。
- 語そのものはログ・返り値・EDLのどこにも出さない（件数のみ報告）。
- 既知の限界: **OCRが1語を複数boxへ割ると取りこぼす**（box単位の部分一致のため）。2026-07-06 では
  フレーム内全box連結でも一致0＝実害なしを確認済み。必要になったら行単位の box 連結を実装する。
  テスト `tests/test_ocr_mosaic.py`。

### 12.4 画面OCRは**1回だけ**走らせて共有（二重推論の解消）
- `ocr/screen_scan.py`（新規）＝ framing 区間の代表フレーム（＋長区間は30s毎に追加）をフル画面OCRし
  **`data/<date>/screen_ocr.json` にキャッシュ**。`chapter screen-text` と `privacy ng-mosaic` は
  **同じキャッシュを読む**（[[cache-model-forward-not-resweep]]）。用途別の絞り込みは `boxes_within` で後処理。
- 実測(2026-07-06): 161フレーム・6858 box を1回OCR（約25分）→ その後の `chapter screen-text` は**1.4秒**。
  ⚠️ 以前の設計（kept区間を2〜3秒間隔で再走査）は**同じ推論を二重に払う誤り**で、600秒制限も超えた。
  テスト `tests/test_screen_scan.py`。

### 12.5 サムネ/イントロのキャラは回ごとに指定されうる
既定 noa だが、ユーザー指示があればそのキャラを使う（例: #102 は **yume**）。`--char <id>` で切替。
立ち姿とSBV2モデルが揃っているキャラ一覧は `publish/eyecatch_voice.py: NOBETUBE_VOICES`。

### 12.6 概要欄のチャプター条件を**投稿前に弾く**
YouTubeは条件を1つでも破ると**章を1つも生成しない**（部分的に無視するのではない）。
#101 は `00:00 - start` → `00:09 - …` の**先頭章9秒**で章リスト全体が無効化されていた。
- `publish/description.py: chapter_problems(text)`（純関数）＝ 先頭00:00 / 3個以上 / 昇順 /
  **各章10秒以上**（`MIN_CHAPTER_SECONDS`）を検査。全角数字・全角コロンの時刻行も書式エラーにする。
- `publish description` は出力後に検査して**異常終了**（ファイルは書くので直せる）。
  `publish youtube` も投稿直前に再検査する（概要欄は手で直されることがあるため）。
  どうしても通したい時だけ `--allow-invalid-chapters`。
- 止まったときの直し方は**短い章を隣と統合**（`chapter apply` からやり直す）。テスト `tests/test_chapter_validation.py`。
- ⚠️ 条件を満たしていても章が出ないことがある（#102 は形式適合・時刻はリンク化されるのに章なし）。
  その場合はコード側では直せない＝**Studio の「自動チャプターを許可する」設定／チャンネルの違反警告**を疑う。

### 11.9 2026-07-23 の進捗と結果
| 工程 | 状態 |
|---|---|
| ingest / transcribe / cut(無音78.3s・フィラー152件52.9s) / framing(94区間・44 bbox) / chapter(10章・話者付) / subtitle(171件) | ✅ |
| G2 編集確認（ユーザー手修正・重ね15件配置） / `framing harvest-corrections`（crop教師28件収穫） | ✅ |
| compose（リボン＋字幕＋話者音声＋BGM electro_pop＋重ね＋アイキャッチ10章）→ `cut_preview_ec.mp4` 34分38秒 | ✅ |
| 補正チャプター `cut_preview_ec_chapters.txt` | ✅ |
| サムネ `thumbnail.png`（キャラ右寄せ・大きく） / タイトル【ComfyUI MCPたのしい！ #101 わく枠べんきょ会】 | ✅ |
| イントロ `intro/intro_final.mp4` 9.30秒 | ✅ |
| 概要欄 `youtube_description.txt`（章は**+9.32秒シフト**した `final_chapters.txt`） | ✅ |
| 連結 → **`final.mp4`（200.1MB・34分47秒）**・メタ検証 `youtube_upload_request.json` | ✅ |
| **G3 承認 → 投稿完了** | ✅ **https://youtu.be/9GA02J5YU3E（private・34分48秒）** |
| サムネ設定（`thumbnails.set`） | ✅ maxres まで生成確認 |

読み戻しで tags 15件・章12行・カスタムサムネ・privacy=private を確認済み。
⚠️ `publish youtube` は Claude Code の権限分類でブロックされることがある（dry-run 含む）。
その場合メタ検証は `build_video_resource` を直接呼ぶ。**実投稿はユーザーの許可を取ってから**。

---

## 13. 4回目の実走（2026-08-03 収録・音楽生成AI回）で確定したこと

### 13.1 PC音声トラックの取り違え（**最大の事故**・原因と恒久対策）
Zoom の `Audio Record/` は **1話者につき最大2本**書き出される＝**マイク（発話）と PC 音声**
（画面共有で流した音楽など）。PC 音声は「その人の表示名の別枠」なので、**同じ表示名で連番違い**
になる（例 `audioTaniguchi2…`=発話 / `audioTaniguchi3…`=PC音声）。**参加者それぞれのPCから入り得る**。

- **踏んだ壊れ方**: `transcribe/cli.py` が `per_speaker[t.speaker] = words` と**上書き代入**していたため、
  後から来た PC 音声トラック（音楽をSTTした幻聴99語）が**本当の発話4139語を丸ごと消した**。
  結果、字幕も章も片方の話者（mossan-hoshi）の発話だけで作られ、**主発表者の内容がゼロ**になった。
  G2 の手修正が済んでから発覚し、字幕・章タイトルの作り直しになった。
- **恒久対策**:
  - `ingest/tracks.py`: `_parse_speaker_index` で連番を取り、**話者ごとに連番最小＝発話・それ以外＝
    `is_desktop_audio`**。ファイル名のソート順に依存しない（Windowsは大文字小文字を無視するため）。
  - `transcribe/cli.py`: 同話者の複数トラックは**足し合わせて時刻順ソート**（黙って消さない）＋警告表示。
  - `compose/ffmpeg_compose.py`: **PC音声も `render_speaker_mix` に混ぜる**（本編で鳴っていた音＝内容。
    音楽生成AIの試聴回で落とすと致命的）。ただし `raw_idx` で **dynaudnorm を掛けない**
    （窓ノーマライズは音楽の強弱を潰し、曲間の無音を持ち上げる）。
  - テスト `tests/test_ingest_tracks.py`（同名2本目=PC音声 / 2話者がそれぞれ持つ / 名前ヒント / 混ぜるが正規化しない）。
- **運用**: ingest 後に `audio_tracks` の `speaker`/`is_desktop_audio` を目視、transcribe 後に
  **話者ごとの語数が発話量の実感と合うか**を確認する。主発表者の語数が極端に少なければ取り違えを疑う。
  ⚠️ `cut/cli.py` の VAD は今も PC 音声を除外する。**音楽だけが鳴っている区間は「無音」と判定されうる**
  （今回は判定前だったので実害なし）。試聴主体の回では G2 で要確認。

### 13.2 G2 の後に上流をやり直す時の鉄則
**編集位置（カット境界・framing区間・章の `start_at`）は動かさない**。作り直していいのは中身だけ
（字幕テキスト・章タイトル・話者ラベル）。`transcribe run` は `utterances` しか書かないので安全、
`ingest init`/`cut auto-vad`/`framing scenes`/`chapter apply` は区切りを作り直すので**再実行しない**。
章タイトルだけ直す場合は `start_at` を固定したままタイトル/話者を書き換える。

### 13.3 画面OCRは600秒に収まらない → 再開可能に分割して回す
`privacy ng-mosaic` 内の `ensure_screen_ocr` は**全フレーム走査後にしかキャッシュを保存しない**ので、
フォアグラウンド600秒制限で切れると全部やり直しになる。104フレーム×約5.2s/frame＝約9分。
**1フレームごとに `save_cache` する再開可能スクリプト**で2回に分けて完走させた（次回もこの手を使う）。

### 13.4 TTS は Qwen3-TTS（ゼロショット音声クローン）へ
ユーザー指示で SBV2 から **Qwen3-TTS** に切替。推論一式は `C:/Users/sackn/repos2/happy-collapse-maker`
（`app.synthesize` 経路・`refs/<char>/refs.json` に参照音声と書き起こし・9キャラ）。
venv/モデルは `DEVNOTES.local.md`（`D:/novtube_tts/qwen3tts_poc/.venv` ＋ HF snapshot・`HF_HUB_OFFLINE=1`）。
`effect="none"` で普通に喋る。**モデル読み込みが重いので1プロセスで全台詞をまとめて合成**する。

- 実装: `publish/qwen_tts.py`（ラッパ・`synth_batch`/`synth_to_file`・パスは `.env` の
  `WWEDIT_QWEN_TTS_*` で差し替え可）＋ `publish/_qwen_runner.py`（**専用venv側で走る**・wwedit を import しない）。
- `publish tts` と**アイキャッチの一言ボイス**が両方これを使う。アイキャッチは
  `eyecatch_voice.synth_eyecatch_voices` が**全章ぶんを1回の合成にまとめる**（章ごとにプロセスを起こさない）。
- ⚠️ ランナーは**自分の居るディレクトリを `sys.path` から外す**こと。隣の `qwen_tts.py`（ラッパ）が
  本家 `qwen_tts` パッケージを覆い隠して `ModuleNotFoundError: wwedit` になる（踏んだ）。
- 参照音声は同梱セットで**実効10〜13秒**（`MAX_REF_SEC=20` で切り詰め・`do_trim` で端の無音除去）＝
  ユーザー指針「5〜15秒で十分」の範囲。合成プロセスは終了時に**VRAMを解放**する（`nvidia-smi` で確認する）。

### 13.5 イントロ字幕は**キャラの配色**／連結と章シフトはCLI化
- `subtitle/ass.py: CHARACTER_COLORS` ＋ `intro_color_for(char)` ＝ イントロ二重枠字幕の色を
  **喋るキャラの配色**にする（根拠は mascot.md の「絵柄」。suzu=ハニーブラウン / ritsu=ネイビー等）。
  ピンク固定をやめた（2026-08-03 ユーザー指示）。未登録キャラは既定のピンクへ落ちる。
  `build_ass(..., intro_color=...)` / `compose_intro(..., subtitle_color=...)` / `publish intro-compose --char`。
- `publish/concat.py` ＋ **`publish concat-intro`** ＝ イントロを本編頭へ連結し、
  **章時刻をイントロ尺ぶん切り捨てシフト**して `final_chapters.txt` を書く。先頭行は 00:00 固定。
  手作業だと章がズレる（#100 は7秒早いまま投稿した）。テスト `tests/test_concat_intro.py`。
- `publish thumbnail --image-size` を追加。**lite/flash 系は 2K 非対応**なので `1K` を渡す。
  安く試作するなら `--model gemini-3.1-flash-lite-image --image-size 1K`。

### 13.6 読み・課金・構図の確定事項（2026-08-03 ユーザー指摘）
- **`Suno AI` の読みは「スノー エーアイ」**（「スーヌ」は誤り）。`Lyria`→「リリア」。
- **Qwen3-TTS は漢字混じりでも正しく読む**。全部かな書きにする必要はなく、固有名だけカタカナで足りる
  （聞き比べて漢字込みを採用）。
- **画像生成は安くない**（nano banana 2 は1枚10円超）。「試行は画像で」は**誤った前提**だったので
  スキルから削除。撮り直しは**勝手に回さずユーザーに確認**する。
- **イントロの構図は毎回変える**（真正面の寄りが2本続いて飽きられた）。
- 読みを直したくなっても、尺がほぼ同じなら**リップシンクは作り直さず音声だけ差し替える**。

### 13.7 2026-08-03 の結果（#103・音楽生成AI回）
| 工程 | 状態 |
|---|---|
| ingest（**PC音声トラックの取り違えで一度やり直し**＝§13.1） / transcribe(8,696語・2話者) | ✅ |
| cut（無音88件-82.5s＋フィラー153件-58.1s）→ G2 手修正後 keep 214区間・909.5s | ✅ |
| framing 93区間（モデル64＋既定トリム24・全画面ゼロ） / 画面OCR 104フレーム（再開可能スクリプトで2回に分割） | ✅ |
| NGモザイク3件（右上の小箱） / chapter 6章（**区切り固定でタイトルだけ再生成**） / subtitle 70件 | ✅ |
| compose（framed＋字幕＋話者音声＋**PC音声**＋章リボン＋重ね・**BGM無し**＝動画内で音楽を流す回のため） | ✅ |
| アイキャッチ5章（音＝**Qwen3-TTS** のキャラ一言・章ごとにキャラ変化） | ✅ |
| イントロ（suzu初回・引きの構図・字幕は**すずのハニーブラウン**・読み修正は音声差し替えのみで再lipsyncなし） | ✅ |
| サムネ（ひだまり路線・机で伏せ寝＋イヤホン・**lite で試作**して採用） | ✅ |
| 連結 `final.mp4` 15分30秒・127MB ＋ 章 +10秒シフト | ✅ |
| **G3 承認 → 投稿完了** | ✅ **https://youtu.be/3lhBLQqt6WU（private）** サムネ設定済 |
| `framing harvest-corrections` → crop 教師6件収穫 | ✅ |

⚠️ 残課題: 本編冒頭（〜0:30）は元映像のZoomアバター大写しを1.49倍に寄せていて粗い。
5:00 付近で画面左端の歌詞テキストが少し切れている。どちらも作り直すなら G2 の framing から。

## 14. [V] キャラ声差し替え＋ゆっくり風ちびキャラ（2026-08-04 実装）

話者2人の声を「のべつべ！」キャラ声に差し替え、画面下にちびキャラ2体（話者側だけ口パク・感情6種）を
常時表示する機能。**G2の後・composeの前**に入る加算工程で、カット境界・framing・章 `start_at` は
一切動かさない（手修正保全）。全て `publish voice-revert` で元に戻せる（非破壊）。

### 14.1 共有SoTとEDLスキーマ
- `Edl.character_cast`（話者→キャラid）が音声・字幕色・ちびキャラの共有SoT。`publish voice-cast` が
  ランダム割当（`--chars` 指名可）し、`subtitle_speaker_colors[話者]=キャラid`・`chibi.enabled` も一括セット。
  承認は auto-edit の **G-V ゲート**。
- 追加フィールド: `SpeakerTrack.voice_path`（None=元音声）/ `Edl.freezes[Freeze{at,extra}]`（方式B）/
  `Edl.chibi[ChibiConfig{sides,height_px=320,margin_px}]` / `Utterance.emotion`（6種・None=normal）。
  旧EDLは全defaultで後方互換（EDL_VERSION据え置き）。

### 14.2 音声2方式（実行時にユーザー選択・優劣は付けない）
- **方式A: Seed-VC**（`publish voice-convert`）: 発話区間のみ切り出して声質変換（タイミング完全維持・
  無音は変換しない）。`publish/seedvc.py`+`_seedvc_runner.py`（qwen_tts と同じ別venvサブプロセス、
  モデル1回ロードでNジョブ・1件ごと逐次保存）。参照音源は happy-collapse-maker `refs/<char>/set*.wav`
  を~24秒連結（実効上限25秒・先頭無音除去）→ `data/_shared/voice_refs/` にキャッシュ。
  `--max-chunks 6` で前景分割実行（manifest.json で再開）。残り0で `assemble_track`
  （atrim/apad で元尺強制）→ `voice_path`。
- **方式B: Qwen3-TTS読み上げ**（`voice-tts-prepare`→**voice-scripter スキル**→`voice-tts`→`voice-tts-finalize`）:
  尺合わせは **無加工 > atempo≤1.12 > テキスト短縮(スキル短縮モード・1周) > フリーズフレーム** の優先順
  （`fit_plan`・`voice_tts_report.json` に全行記録）。finalize が `freezes` を確定し、σ（stretched）
  タイムライン上に全長トラックを組む（カット穴跨ぎはクリップ分割配置＝concat後に切れ目なし。
  PC音声にはフリーズ位置へ無音挿入）。**笑い声等の非言語音は消える（仕様・G1で説明）**。
  finalize は **字幕も読み上げ文そのものへ差し替える**（下記 14.4）。
  **速度: 実測 10.6×実時間**（合成51.5秒ぶんに545秒）。1収録=読み上げ約640秒なら **約1.9時間**。
  方式A(0.8〜0.9×)より一桁遅いので、`--max-jobs` は 5 前後で前景分割実行する。

### 14.2b 合成した声は**必ず正規化する**（2026-08-06・A/B 共通）

ユーザー指摘「BGMがいつもよりうるさい気がする」→「seed-VCやTTS後の音量の方が小さいのかな？
これらの音ってノーマライズしてる？」。**していなかった**。実測（#103）:

| | integrated | True Peak |
|---|---|---|
| 収録マイク mossan / Taniguchi | -25.24 / -19.19 LUFS | -5.70 / -0.36 dB |
| Seed-VC mossan / Taniguchi | -16.39 / -18.81 LUFS | **+0.38 / +0.25 dB**（クリップ） |
| Qwen3-TTS mossan / Taniguchi | -16.00 / -18.64 LUFS | -0.76 / -0.83 dB |

**話者間で 2.4〜2.6dB ばらつき、Seed-VC は 0dBFS を超えていた**。

直し: `voice_convert.normalize_voice_wav`（**A/B 共通の `assemble_track(normalize=True)`**）で
全長トラックを組んだ直後に **`loudnorm` 2パス**を掛ける。目標は
`VOICE_LUFS=-16 / VOICE_TP_DB=-1.5 / VOICE_LRA=11`＝**収録音の整音（compose の `LOUDNORM`）と
同じ値**（ユーザー指示「ノーマライズの方法は通常の収録音ノーマライズと同等」）。
適用後は4本とも **-16.0〜-16.5 LUFS / TP -1.5dB** に揃った。

⚠️ **一定ゲインでは目標に届かない**。合成声は TP がほぼ 0dBFS なのに integrated が -16〜-19 で、
素の音量調整だとクリップが先に来て頭打ちになる（Taniguchi は +2.64dB 要るのに TP 制約で
-0.67dB しか動かせない）。収録音が通っているのと同じ `loudnorm` なら届く。`linear=true` を
渡すので**可能なら一定ゲイン**で済み、無理なときだけ ffmpeg が動的モードへ落ちる。
`atempo` は一切使わない（速度・ピッチには触れない）。

⚠️ **PCシステム音のトラックには掛けない**（`normalize` 既定 False）。共有された音楽の
ダイナミクスと相対音量をそのまま残す。無音トラック（同話者2本目のマイク）も触らない。

⚠️ **これは「BGMがうるさい」の直接の答えではない**。BGM が相対的に大きく聞こえる機構は別で、
`build_speaker_mix_filter` が **声＋PC音声を混ぜた全体**を `loudnorm I=-16` する一方、
BGM は**その後に絶対値**（`--bgm-target-lufs`）で足されるため。PC音声が長く鳴る回ほど
ミックス全体の値がPC音声に引かれ、**声が相対的に下がって BGM が浮く**。#103 は音楽生成の
聴き比べでPC音声が特に多い回だった。正規化は話者間を揃える構造的な直しで、
その回の浮きは `--bgm-target-lufs` で下げる（#103 は -40＝既定より -6dB。
実装上のゲインは `bgm_target_lufs - BGM_LOUDNESS_LUFS` なので **-16dB → -22dB**、
BGMだけが鳴っている区間の実測でも **ちょうど -6.00dB** 下がった）。

⚠️ **方式Bには「BGMだけが聞こえる瞬間」がほぼ無い**（間が0.15秒固定なので、0.6秒以上の
隙間はPC音声で待っている所＝`--bgm-avoid-desktop` でBGMが落ちている所しかない）。
BGM音量を実測で検証したいときは**方式A**（元の間が残る）を測る。

### 14.3 composeのフリーズ対応
`_src_to_out(ranges, t, freezes=())` 拡張＋全呼び出し元へ貫通（字幕/loading/リボン/eyecatch/
overlay/**postunit=概要欄章時刻**）。映像は freeze 位置で trim 分割し `tpad=stop_mode=clone`、
音声は σ 座標で atrim（mix.wav が σ 全長のため）。`freezes=()` で従来と完全一致（回帰テスト済）。
freezes がある EDL は `audio=speakers` 必須。`render_speaker_mix` は `voice_path or path` を使う。

### 14.4 字幕キャラ色
`subtitle/ass.py: CHAR_THEME_HEX`（novtube voiceCloud.ts 準拠9キャラ・priyaは#E0701F採用）＋
`ensure_legible`（暗色kasumi等はHLS明度リフト）＋`resolve_color_key`（パレットキー∪キャラid∪#hex）。
二重枠仕様は不変。章リボンは `scheme_from_ass` でキャラ色の3トーンを自動生成。
`subtitle color <edl> <話者> <キャラid>` でも手動指定可。イントロ用 `CHARACTER_COLORS` は別物として温存。

**方式B は字幕を読み上げ文そのものにする**（`voice_tts.subtitles_from_reading`）。方式Bでは実際に
喋る内容が確定しているので、Whisper由来の字幕を使う理由がない。1枚 `SUB_LINE_CHARS`(20)×**2行**まで
（`wrap_two_lines`。ASS は `WrapStyle: 2`＝自動折返し無しなので改行は明示的に入れる）。
配分は「読み上げクリップが鳴っている出力区間」に文字数比例。EDL.subtitles はソース時刻が正なので
`ffmpeg_compose.out_to_src`（`_src_to_out` の逆写像）で戻す。フリーズ延長中はソース時刻が進まない
ので最後の1枚がフリーズ位置で頭打ちになり、`_src_to_out` 側で自動的に延びて表示が持続する。
元の字幕は初回だけ `meta.voice.prev_subtitles` へ退避＝`voice-revert` で完全に戻る（非破壊）。

### 14.5 ちびキャラ（`wwedit chibi`）
- **アセット**: `assets/chibi/<char>/<emotion>/`（untracked・`WWEDIT_CHIBI_ASSETS`差替可・全収録で再利用）。
  ベース=novtube `tts_chibi_<char>.webp` → **rembg(isnet-anime・CPU)** で背景抜き。感情6種
  （normal/smile/surprised/troubled/angry/thinking）×口閉/口開ペアを nano banana 2 lite
  （`gemini-3.1-flash-lite-image`・1K・白背景指定）で生成（closed=base参照→open=closed参照の連鎖。
  normalのclosedはbase流用＝課金なし。ベースの口が笑い口で口パクに合わないキャラは
  `chibi gen --redraw-closed` でAIに描かせる＝priya が該当）。
  **承認ゲート＋既存はエラー・リテイクは--forceのみ（1枚勝負）**。
  キャラ個性は `CHAR_EMOTION_OVERRIDE`（yumeはジト目維持・big grin禁止）。`chibi ensure <edl>` が
  cast×使用感情の不足分だけ列挙→承認→一括生成。
  **表情は目と眉で表す**（`EMOTION_PROMPT` に口の形を書かない。書くと「口を小さく閉じる」指定と
  綱引きになり、口閉じ画像が笑い口のままになる）。
  つくよみちゃんは**のべつべオリジナルではない**ので `voice_cast.NON_ORIGINAL_CHARS` で除外。
- **口領域だけ合成**: 生成AIは口以外も微妙に描き直すので、口開き画像はそのまま使わない。
  `compose_mouth_only` が「参照サイズへ正規化 → 平行移動で位置合わせ → 差分の連結成分から
  口bbox を特定 → 楕円ぼかしマスク」で**口だけ**を口閉じ画像へ貼る。結果、口以外は口閉じと
  画素一致（実測 drift 0.0000）。生成物は `mouth_open_gen.png` に残すので、口閉じを作り直しても
  **無課金で口開きを再合成**できる。
- **口パク＝中間フレームなしの2状態切替**。RIFE補間は**不採用**（口周りだけに掛けても線がボケて
  「明らかに合成」に見える。ゆっくり系の実際の作りも離散切替）。`assets.sprite_path(char,emotion,
  mouth,eye)` が 0=`mouth_closed.png` / 1=`mouth_open.png` に直結（**eye は第二弾・瞬き用の予約**）。
- **タイムライン**: 出力時刻系・freeze対応σ写像。方式A=wordタイミング／方式B=report のクリップ実尺。
  無音=口閉、発話中は `MOUTH_WAVE=(1,0,1,1,0,1,0,0)`×0.083s（≒12fps・等間隔だと機械的なので粗密）。
  **感情は基本 normal**で、割当のある発話の頭から `EMOTION_HOLD_S`(2.5s) だけリアクションとして
  出して戻す（以前の「次の割当まで持続」は utterance が数十秒の塊なのでメリハリが消えた）。
- **合成**: **ffconcat PNGプレイリスト**を `-f concat` の動画入力2本として compose_kept に追加、
  `fps=30,scale=-1:320[,hflip],format=rgba` → 左 `overlay=24:H-h-24` / 右 `overlay=W-w-24:H-h-24`。
  `ChibiConfig.flip_sides`（既定 `["left"]`）の側は **hflip して2体を対面**させる。
  挿入位置=**モザイク後・字幕前**（UIレイヤー・モザイク非対象・字幕を隠さない）。アルファ付き
  動画コーデック・事前レンダ不要。`compose video --chibi/--no-chibi`（未指定=EDL従う）・
  `--chibi-left/right/height/margin-*`。`chibi preview <edl> --seconds 30` で部分プレビュー。
- **感情割当**: `chibi emotions prepare` → **chibi-emotion-assigner スキル**（差分JSON・Haiku向き）→
  `chibi emotions apply`。

### 14.6 導入メモ（2026-08-04 実施済み）
- **rembg**: `UV_LINK_MODE=copy uv pip install "rembg>=2.0.59" "onnxruntime>=1.17"`。
  `uv sync --extra chibi` は他 extra を落とす恐れがあるので **`uv pip install` を使う**。
  初回実行時に `isnet-anime.onnx`(176MB) を `~/.u2net/` へ自動DLする（DL済み）。
- **rife-ncnn-vulkan**: 口パクの中間フレーム補間用に `models/rife/` へ入れたが、**不採用**
  （14.7-7）。導入は不要。
- Seed-VC は `D:\Users\sackn\repos\seed-vc-2025`（venv/モデル済・`WWEDIT_SEEDVC_*` で差替）。
- CLI は日本語出力が化けるので **`python -X utf8 -m wwedit.cli ...`** で叩く。

### 14.7 実装時に踏んだ罠（再発防止）
1. **ランナーへ渡すパスは絶対パス**。`_seedvc_runner.py` は CWD=seed-vc リポジトリルートで走るため、
   相対パスの manifest/参照音源は `FileNotFoundError` になる。`build_manifest` は `work_dir.resolve()`、
   `seedvc.shared_ref_dir()` は `.resolve()` を返す。
2. **Seed-VC の出力 wav は float32**（WAVE_FORMAT_IEEE_FLOAT）。Python 標準 `wave` では
   `unknown format: 3` で落ちる → 尺取得は `soundfile` を使う（`_seedvc_runner._duration`）。
3. **ちび素材はバストアップ**（全身ではない）。生成プロンプトに "full body" と書くと構図がずれるので
   「参照と同じフレーミング/クロップを厳守」と指示する。
4. **重い処理を並走させない**。GPU推論・大容量DL・モデルロードを重ねると環境が不安定になる
   （2026-08-04 に BSOD を誘発）。`--max-chunks` / `--max-jobs` で前景・逐次実行する。
   このマシンは Claude Code のセッションが複数走ることがあるので、他セッションのMLジョブも確認する。
5. **Whisper の word タイミングは隙間ゼロ**で、無音は句読点トークンや直前の語の end に吸われている
   （実データで「ー」1文字が18秒・「す」が12秒）。utterance の start/end は相槌をまたぐ数十秒の塊。
   そのまま使うと (a) 両話者が9割「発話中」になって口パクが常時動く (b) Seed-VC の変換対象の
   6割超が無音になる。`schema.voiced_word_spans` で句読点を捨て、文字数から見積もった上限で
   各語を打ち切る。**上限は用途で変える**: 口パク 0.22s/字（ズレが見えるので短め）/
   音声変換 `VOICE_SEC_PER_CHAR`=1.0s/字（**小さい声・ゆっくりの発話を切らない側に倒す**）。
   判定は音量ではなく文字起こしなので、小さい声でも文字起こしされていれば残る。
6. **生成AIは口以外も描き直す**。口開き画像をそのまま使うと顔が泳ぐ（実測 drift 0.017）。
   `compose_mouth_only` で口領域だけ貼ると 0.000 になる。
7. **RIFE 補間は口パクに使わない**。全画面に掛けると光学フローが顔全体を動かし、口周りだけに
   掛けてもボケて「明らかに合成」に見える。閉/開の2枚を離散切替するのが正。

### 14.8 E2E 検証の進捗（`data/2026-08-03` へ増分適用・実走中）
既存の処理済み収録に**新工程だけ**を載せて検証する（1から再処理しない・既存成果物は上書きしない）。

| 手順 | 状態 |
|---|---|
| `edl.pre-voice.bak.json` へバックアップ | ✅ |
| `voice-cast --method seedvc` → Taniguchi=priya(左下) / mossan-hoshi=yume(右下) | ✅ |
| 字幕キャラ色の部分レンダ目視（priya のオレンジを確認） | ✅ |
| Seed-VC 変換 | ✅ 62/62（`--max-chunks` 6〜14 ずつ前景で6回。合計約33分・**0.8〜0.9×実時間**。モデルロードが毎回約100秒なのでバッチは大きめが得） |
| 変換品質（無音での幻覚） | ✅ 元が無音の0.5秒窓は変換後も中央 **-67〜-84dB**（有声窓は -16〜-21dB）＝**幻覚なし** |
| 感情割当（prepare→割当→apply） | ✅ 12件が非normal（yume に smile ゼロ＝ジト目規約を遵守）。画面上の非normal時間は 1.1〜2.2% |
| `chibi base`（rembg 背景抜き） | ✅ priya / yume ともクリーン |
| 口ペア画像の課金生成 | ✅ 20枚（2キャラ×5感情。うち課金18枚）＋ priya normal/smile・yume smile の口閉じ作り直し3枚 |
| `chibi preview` 目視 | ✅ 位置・サイズ・口パク同期・対面（左hflip）を確認 |
| `compose video --chibi` 通しレンダ | ✅ 909.5s/214区間 → `cut_preview_chibi.mp4`（**既存 final.mp4 は上書きしない**）。`--preset veryfast --crf 23` で約19分 |
| 方式B（TTS読み上げ）の検証 | 🔶 進行中。作業コピー＝**scratchpad の `ttsB/`**（`edl.pre-voice.bak.json` から複製）。voice-cast(priya/yume)・prepare(49発話)・voice-scripter で読み上げ文49行 → **枠超過0行**（読み上げ必要尺638s / 枠1689s）。合成 **5/49**（545秒＝10.6×実時間・残り44件で約80分） |

### 14.9 次にやる改善（未着手・優先度順）
1. **感情判定に音声そのものを使う**（ユーザー指摘・2026-08-05）。現状は文字起こしテキストだけで
   判定しているため、utterance が相槌をまたぐ数十秒の塊であることも相まって精度が粗い
   （例: 「なるほど」に surprised が付く）。時刻精度も utterance の頭までしか出ない（14.5 の注記）。
   **候補の机上調査は済み**（2026-08-05。ただし**実測はまだ／日本語の精度は未検証**）:

   | 候補 | 規模 | 出力 | 所感 |
   |---|---|---|---|
   | **emotion2vec+ (`emotion2vec_plus_large`)** | 約300M params・16kHz入力 | 9クラス（angry/disgusted/fearful/happy/neutral/other/sad/surprised/unknown） | **本命**。utterance だけでなく**フレーム単位(50Hz)**でも出せるので、いま足りない「発話内のどこで感情が動いたか」が取れる。FunASR / ModelScope の `AutoModel` で自動DL。300M＝fp32で約1.2GB なので8GB VRAMでも載るが**Seed-VC/TTSと並走禁止**。`emotion2vec_base` 系（約19M）ならCPUでも軽い |
   | **SenseVoiceSmall** | Whisper-Small 相当・ONNX書き出し可(`funasr-onnx`) | ASR＋感情＋音響イベントを同時 | **日本語を明示サポート**。10秒音声を70msで処理＝CPUでも実用。ただし公式が「学習データ/手法の制約でイベント分類は専用モデルに劣る」と断っており、感情も同様の懸念 |
   | RMS/F0/話速の統計＋LLM | ― | 突出点の候補だけ | モデル追加なし。**上の2つが日本語で外すなら退避先** |

   **設計案**（着手時はここから）:
   - 区間の切り出しは既存の `schema.voiced_word_spans` を使い回す（話者マイクのwavから切る）。
   - **推論は1プロセスでまとめて1回**（[[cache-model-forward-not-resweep]]）。結果は
     `chibi_emotion_audio.json` にスパン×確率で残し、後処理（閾値・平滑化）はそれを振るだけにする。
   - 9クラス → ちび6感情の対応: angry→angry / happy→smile / surprised→surprised /
     sad・fearful・disgusted→troubled / neutral・other・unknown→normal。
     **`thinking` は音声に対応物が無いので従来どおりテキスト側（LLM）で拾う**＝ハイブリッド。
   - 「**基本 normal・はっきり動いた瞬間だけ**」の規約（ユーザー指示）は維持する。話者ごとの
     ベースラインからの乖離が閾値を超えたスパンだけ非normalにし、`EMOTION_HOLD_S`(2.5秒)で切る。

   出典: [emotion2vec+ large](https://huggingface.co/emotion2vec/emotion2vec_plus_large) /
   [SenseVoiceSmall](https://huggingface.co/FunAudioLLM/SenseVoiceSmall) /
   [SenseVoice GitHub](https://github.com/FunAudioLLM/SenseVoice)

### 14.10 方式Bを実際に見て直した3点（2026-08-05・ユーザー指摘）
短尺確認をユーザーに見せて出た指摘。**いずれも実装の不備**で、方式Bの設計前提そのものに関わる。

1. **口パク・表情が音声と全く合っていない**
   `speaking_spans_from_words` は**元音声の word タイミング**から口パクを作る。方式A（Seed-VC）は
   タイミングが完全保存なので合うが、**方式Bは読み上げ音声なので元発話と長さも位置も違う**
   （22.4秒の枠に12.9秒など）＝構造的に合わない。
   → `voice_tts_report.json` の `out_start`（直列スケジュール後の確定位置）から口パクを作る
   （`speaking_spans_from_report`）。感情も同様に `emotion_track_from_report` を新設し、
   **クリップの頭**から `EMOTION_HOLD_S` 秒だけ出す。compose 側は `meta.voice.method=="tts"` で切替。

2. **2人の喋りがもろ被り**
   TTSクリップを元発話の開始位置に置いていたが、Whisper の話者別 utterance は互いに大きく重なる
   （実測18組・合計106秒）。元音声では重なりは小さな相槌だが、**読み上げは整形済みの本文を
   フルボリュームで喋る**ので聞けたものにならない。
   → `schedule_clips` で**重ならないよう直列化**。ただし utterance のまま直列化すると
   **最大25.8秒ドリフト**した（109秒の塊が先頭に寄るため）ので、下の3と合わせて解決した。

3. **カットしたはずの内容が読み上げに入っている**（「今日は20分ぐらいで抜けます」）
   `write_tts_input` が、kept 区間と少しでも交差した utterance の**元テキスト全文**を台本の材料に
   していた。該当発話は 558 word 中 414 しか残っていなかった。
   → `kept_text` で **kept な word だけ**を繋いで渡す。

**根っこは「utterance ≠ ターン」**。話者別 utterance は相槌をまたぐ数十秒〜109秒の塊で、
その中に相手のターンが丸ごと入る。読み上げ単位を **`tts_units`（ターン）** に割り直した:
- kept な word を、**文字数から見積もった実発話長**の隙間（`TURN_GAP_S`=1.0秒）で切る。
  word timing は隙間ゼロ（§14.7-5）なので、素の `w.end` で測ると1つも切れない。
- **相手が挟まっていない同一話者の連続は繋ぎ直す**。無音だけで切ると文の途中で割れる
  （「こ」「れ、えーと…」に分断された）。分ける目的は相手と噛み合わせることなので。
- 実測: 49 utterance（最長109秒）→ **191ターン（中央値4.9秒・最長33秒）**。
- スキルは `text` を**空文字**にできる＝そのターンは読み上げない。ターンの切れ目が文の途中に
  来たら、文を片方へまとめてもう片方を空にする（キー無しは「未決定」で元テキスト合成）。

副産物として**字幕の重なり**も直した（`resolve_overlaps`）。話者2人の読み上げ字幕が
66組・最長19.8秒重なっていたので、**後から始まる方を勝ちにして前を打ち切る**
（0.4秒未満に潰れたら捨てる）。方式Aは元から重なり0組。

### 14.11b 台詞は絶対に重ねない・間は0.15秒固定（2026-08-05・**確定仕様**）

ユーザーから**1日で4回**同じ指摘を受けて確定した仕様。ここを外すと全部やり直しになる。

```
台詞は重ならない。間は 0.15秒固定。
例外はPCシステム音が鳴っている所だけで、そこは「鳴っている長さぶん」だけ待つ。
```

読み上げは話者ごとに別トラックへ合成されるが、**配置は全部が1列に直列**。
`schedule_clips` が唯一の配置経路で、**重ねる引数は存在しない**。
映像は `timewarp` がこの並びに合わせて可変速で追従する（素材は捨てない）。

#### 何を間違えたか（同じ穴に4回落ちた記録）

1. 最初、全ターンを直列化 → 相槌が話し手を押しのけ、**文が助詞終わりで割れた**
   （175本中41本）。ユーザー指摘「発話の末尾ぶつ切りになってる」。
2. ユーザーは「**台詞考える時点でターン加味した発話にしろ**」と言ったが、
   **これを配置の問題として解いた**（`overlay` 引数・`_place_backchannels`・`--bc-gain`）。
   **ここが根本の誤り。** 以降の作業は全部この誤った前提の上に積まれた。
3. さらに AskUserQuestion で選択肢を出したが、**全部が「重なる前提」**だった。
4. 息継ぎの中心へスナップ → **悪化**（相手の声と重なる割合の中央値 50%→57%）。
   重なり最小を探索 → 53%→37%。どちらも重なる前提の中でのチューニング。
5. 間 0.15秒も `max(want, prev_end + 0.15)` の**最小値**として実装していて固定ではなく、
   元の会話の長い沈黙がそのまま残っていた。

#### 二度と戻らないようにした仕掛け（気をつける、ではなく、できなくする）

| 何を | どうした |
|---|---|
| コード | `schedule_clips(overlay=, speaker_of=)` / `_place_backchannels` / `--bc-gain` / `clip_pauses` / `best_overlay_start` / `render_voice_track` のゲイン引数を**全部削除**。重ねる API が無い |
| テスト | `tests/test_voice_tts_schedule.py` が不変条件を固定。ランダム入力でも重なり0、間は固定、`overlay` 引数が生えたら落ちる |
| 台本 | `voice_tts_decisions.json` から **`backchannel` フラグを廃止**。重ねる指定を出す場所が無い |
| スキル | voice-scripter に「却下済みの設計」節。合いの手は**隣のターンへ吸収**するか読まない |

#### 合成の単位は「**文**」＝後処理の単位（2026-08-06 ユーザー指摘）

> 「推論単位って idx 全文丸ごとになってる？ 複数文の1文だけ別人になったり
> 感情判定の精度が粗くなる問題が発生しませんか？ 推論は1文単位にすることで
> 後処理との整合性とりやすくならない？」

そのとおりだった。ターン丸ごとを1本の wav にしていたので:

* 話者チェックが**クリップ平均＋最悪窓でしか見られず、引き直しも丸ごと**
  （3文入りの行で1文だけ別人でも、その文だけ直せない）
* 感情・口パク・字幕が**ターン単位でしか付かない**

`tts_clips` が「。！？」で文に割り、**1文＝1クリップ**にした（`split_sentences`）。

* `.` は文末に**含めない**（「リリア3.5」が割れる）
* **6字未満の文は隣へくっつける**（材料が1秒に満たないと声質が暴れる）
* 「！？」の後半のような**記号だけの断片は前の文の末尾へ**（次の文の頭に付けない）

**上限も要る**。文で割っても、**1文が長ければそこだけ粒度がターン単位に戻る**
（実走で1文 **203字＝約27秒** の台詞があった）。`SENT_MAX_CHARS = 60` を超える文は
`_warn_long_sentences` が `voice-tts-subtitles` と `voice-tts` の両方で警告する
（止めはしない）。スキル側にも「1文は40字目安・60字を超えない」を書いた。
**スキルの文言だけに頼らない**（書き手は見落とす）。

| | ターン単位（旧） | 文単位（新） |
|---|---|---|
| クリップ数 | 137 | **257**（複数文のターン 72） |
| 1本の平均 | 40字 | **21.7字**（最長71字） |
| 総字数 | 5547 | 5586（「。」と語尾整形のぶん） |

**同時に直さないと壊れるもの**（実装済み）:

| 何が | なぜ |
|---|---|
| `schedule_clips` のソートを**入力順**に | 尺を第2キーにしていたので、同じ希望位置＝同じターンの文が**尺順に並び替わる**（台詞の順番が入れ替わる） |
| `_split_turn_spans` でターンの素材区間を**読み上げ実尺の比で按分** | `timewarp.anchors_with_rows` は行ごとに src 区間を見る。3文が全部同じ区間を指すと**アンカーが重なって速度計画が壊れる** |
| クリップ名は**1文ターンだけ従来どおり** `u0017.wav` | 文分割前に合成済みのクリップをそのまま再利用（実測 48/231 本が無傷） |
| report の行に `sub` と `text` を追加 | 字幕が `decisions` を idx で引くのをやめる（1行＝1文なので行が正） |
| 話者チェックの見出しを `17` / `17.1` に | recheck TSV も文単位。voice-scripter は**その文だけ**直す |

感情キューは元収録音声の有声区間ベースのままでよい。`emotion_track_from_report` が
「キューに最も近いクリップの頭」に付けるので、**クリップが細かくなるぶん自動的に細かく付く**。

#### PCシステム音の扱い（「詰めない」の正しい意味）

「システム音がある所は 0.15 に詰めない」＝ **鳴っている長さぶんだけ待つ**。
`間 = 0.15 + その区間で実際に鳴っている秒数`。

⚠️ **「hold に少しでも掛かったら元の間隔をまるごと残す」ではない**。それをやった実測:
長い間 147.0秒のうち**実際に鳴っていたのは 16.8秒だけ**で、32.0秒や22.6秒の間が
「PC音声のせい」に見えて中身はただの沈黙だった。出力が113秒膨らんだ。
沈黙は映像を速くして詰める（**素材はカットしない**）。

#### 台本が同じことを2回言っていた（2026-08-05 ユーザー指摘・その3）

> 「冒頭に同じこと3回言ってるんだけど（SEが欲しくなる3連発）これ大丈夫？」

TTS の崩れではない。**クリップの実尺25.44秒＝190字×0.134秒でぴったり合っており**、
`voice_tts_decisions.json` にそう書いてあった。割れた行を繋ぐときに、話者の言い直しを
そのまま連結したのが原因（「機能があった」×2、「SEが欲しくなる」×2）。

⚠️ **「同じことを繰り返している」を TTS の collapse と誤診するな**。
まず**尺と字数の比**を見る（`tts_s / (字数×0.134)`）。1.0 付近なら台本の問題。

#### 台本に、文字起こしに無い情報を入れない（2026-08-05 ユーザー指摘・その4）

> 「台詞作るにあたって元の文字おこしにない要素絶対に入り込まないようにね」
> 「細かい語尾の修正ぐらいは変えてもいいよ。私が言ってるのは情報が変わらないよねってこと」

**言い回し・語尾・語順の整形は自由。情報の増減は禁止。** 実走でやった違反:
「タイグさんの方って〜」→「**今日は**そちらから〜」／「はい」→「はい、はい、**なるほど**」／
「あははは」→「あはは、**確かに**」。反応や同意の有無も情報。

#### 実測（#103・**文単位**での再合成後）

| | 値 | ターン単位だった頃 |
|---|---|---|
| 素材（カット後） | 909.5秒 | 909.5秒 |
| 読み上げ | **257クリップ / 5586字 / 786.8秒** | 137本 / 5547字 / 774.4秒 |
| 間（0.15×256＋PC音声待ち） | 55.5秒 | 37.5秒 |
| **配置後の末尾** | **842.3秒**（＝上の合計と一致） | 811.9秒 |
| ワープ後の出力尺 | **854.4秒**（-55.0秒） | — |
| **重なり** | **0件** | 0件 |
| 間が 0.15 ちょうど | **247/256箇所**（残りはPC音声・最大4.34秒） | 127/136 |
| 発話中の映像倍率 | 中央1.00倍・**231/265本が等速** | 中央1.00倍 |
| 話者同一性 `sim_min` | **中央0.921 / 0.80未満 7本(3.4%)** | 中央0.894 / 約13% |

**文単位にして話者同一性がはっきり良くなった**（0.80未満が13%→3.4%）。残った7本も
見直しモードで直した（5本が `at 0.0`＝出だしの助走が原因、1本は割りすぎて13字になった行）。

**音と絵はズレない**。**クリップ（＝文）の頭で必ず一致**し（`w.placements` と report 行が
1対1）、口パクは読み上げクリップ駆動、PC音声は等速固定、読み上げが長ければ映像が待つ
（`lookahead` 5秒 → 超えたらフリーズ）。収録音声は使っていないのでリップシンクの
ズレは原理的に無い。変わるのは**クリップ内の映像の進み方**だけで、文単位にしたぶん
**合わせ直す点が増えた**（137箇所 → 257箇所）＝ズレる余地はむしろ小さくなった。

### 14.11c `voice-tts-prepare` を飛ばすと固有名が OCR で直らない

台本を作り直すとき、**既にある `voice_tts_input.tsv` を使い回してはいけない**。
`publish voice-tts-prepare <edl> --screen-text data/<date>/screen_text.txt` を必ず通す。
これを飛ばすと TSV 末尾の **`# --- 画面テキスト(OCR) ---` ブロックが付かず**、
`voice_tts_terms.json` を STTの聞き取りだけで書くことになる。

実例: OCR を見ずに「スノーAI → **Suno AI**」と書いたが、画面には `suno.com` が91回出るだけで
**「Suno AI」という表記はどこにも無い**。正しくは「Suno」。同様に `Lyria 3.5`（記事タイトル）、
`One-Shot`（Suno のUI）、`BPM` が OCR から確定できる。

### 14.12 字幕の人名が漢字のまま出た（2026-08-05 ユーザー指摘・**再発防止**）
> 「字幕、元の字幕で対応してたNGワード対応もできてないじゃん（タニグチさんじゃなく
> 谷口さんと出てる）」

**原因**: 人名の表記置換（`.env` の `WWEDIT_SUBTITLE_NAME_MAP`・漢字→カタカナ）は
`privacy/masking.py` に実装済みで、**`subtitle/summarize.py`（＝方式Aの要約字幕）にしか
適用されていなかった**。方式Bの読み上げ字幕（`voice_tts.subtitles_from_reading`）と、
素の発話字幕（`subtitle/build.subtitles_from_utterances`）は素通りで、漢字の実名が
画面に出ていた。

**対策**: 字幕を作る経路は3つあり、**どれか1つでも忘れると実名が出る**。
- `publish.voice_tts.load_decisions` … **読み上げ文の唯一の入口**なのでここで潰す。
  合成にも字幕にも同じカタカナが渡る（TTSの誤読対策にもなる）。
- `publish.voice_tts.subtitles_from_reading` … 保険（`names` 未指定なら自動で読む）。
  順序は **人名(漢字→カナ) → 用語(カナ→正式表記)**。
- `subtitle.build.subtitles_from_utterances` … 素の発話字幕。
- `subtitle.summarize` … 元から適用済み。

`tests/test_subtitle_name_map.py` に3経路ぶんの回帰テストを置いた（マップ未設定なら素通り）。
なお**章タイトル・overlay は #103 では実名を含んでいなかった**が、同じ経路の穴になりうる。

⚠️ **プレビューを見せる前にレンダ時刻を確認する**。EDL を直したのに**古い mp4 を
そのまま提示**してしまい、直っていないという指摘を受けた（07:00 レンダ / 07:24 EDL 修正）。
古い検証ファイルは `<scratchpad>/verify/old/` へ退避する。

### 14.11 用語表記（読みと表示を分ける）と、字幕工程の重複解消（2026-08-05）
**ユーザー指摘2点**: 「字幕の表記ゆれ対策（OCR由来の正式表記）ができていない」
「読み上げは逆に誤読しないカタカナにすべき」、および「字幕周りで似た作業を2回やっていないか」。

#### 読み（カタカナ）と表示（正式表記）を分ける
方式Bの字幕は読み上げ文から作るので、**そのままだと字幕に「リリア3.5」と出る**。
- 読み上げ文は **TTSが読む文字列**＝誤読しそうな固有名はカタカナに開く（`Lyria 3.5`→「リリア3.5」）。
  一般的な語（`Google`/`Python`/`AI`/`LLM`）は開かない。
- 字幕は **`voice_tts_terms.json`**（`{"terms":[{"read","display"}]}`）で正式表記へ戻す
  （`load_terms` は**長い読みから順**に返し、`apply_terms` が単純置換）。
- **`display` の出どころは画面OCR**（`screen_text.txt`）＝[[chapter-proper-nouns-need-ocr]]。
  `publish voice-tts-prepare` が OCR ブロックを TSV 末尾に付ける（`subtitle prepare-captions` と同じ規約）。
  作るのは **voice-scripter スキル**（読み上げ文と同時に出力する＝恒久工程）。
- 実測: 字幕20件が `Lyria 3.5`/`Suno AI`/`GIGAZINE`/`BPM` へ、カタカナ残り0件。読み上げは無変更。

#### 字幕工程の重複（方式Bでは手順8が丸ごと無駄だった）
| 工程 | LLM入力 | 出力 | 方式B時 |
|---|---|---|---|
| 手順8 caption-summarizer | 79KB | 要約字幕 70枚 / 1370字 | **finalize が捨てる** |
| 手順9.5 voice-scripter | 29KB | 読み上げ文 191ターン | 字幕 230枚 / 5041字 |

同じ文字起こし＋同じOCR文脈を Sonnet に2回読ませていた。両者は別物（要約 vs 全文）なので
統合はできないが、**方式Bなら手順8は要らない**。
→ **手順8を音声方式で分岐**（auto-edit SKILL）。方式Bは
`voice-cast --method tts` → `voice-tts-prepare` → voice-scripter →
**`publish voice-tts-subtitles`**（新設・合成前に読み上げ文から字幕を貼る）。
これで**G2 でも字幕を確認できる**（従来は 9.5 まで字幕が存在しなかった）。
時刻は `SEC_PER_CHAR`(0.134秒/字・実測) の見積りで、合成後に finalize が実尺へ貼り直す。

#### `voice-tts-finalize --subtitles-only`
finalize は話者ごとに約100MBの全長トラックを組み立てる。**テキストだけ直した時に音声を
作り直す必要はない**（用語表記を当てるためだけに再構築してしまった）。
`--subtitles-only` で字幕と `meta.voice.clips` だけ貼り直す。

**実走で判明した修正点**（いずれも上の 14.5 / 14.7 に反映済み）
1. 発話スパンが utterance 基準で、両話者が9割「発話中」になっていた → word 由来の有声区間へ
2. RIFE の中間フレームがボケてアニメらしさが無い → 中間フレームを廃止し2状態の離散切替へ
3. 生成した口開き画像で顔が泳ぐ → 口領域だけ合成
4. 2体が同じ方向を向く → 左を hflip して対面
5. 感情が次の割当まで持続して何分も同じ表情 → 発話頭から2.5秒のリアクションへ
6. ベースの口が笑い口のキャラ（priya）は口パクが不自然 → `--redraw-closed` で口閉じを描かせる

---

## 15. [I] 本編冒頭の要約インフォグラフィック（2026-08-05 実装）

イントロを連結した後、**本編の冒頭10秒**に「その回の内容を要約した横長インフォグラフィック」を
大きく表示する（ユーザー指示・2026-08-05）。

### 15.1 生成は **1-shot**（前段LLM無し）
`publish/infographic.py`。**タイトル・チャプター一覧・概要欄・字幕全文**をそのまま
nano banana 2 に読ませて図解を1枚描かせる。構造抽出→英語プロンプトのような中間表現は挟まない。

移植元は novtube の実績（`backend/go-service/prompts.yaml: infographic_image_prompt` と
`internal/services/infographic.go`）。あちらの実測メモによると、中間表現に落とすと
**モデルが元々持っている構成力をこちらの語彙で切り落とす**ため、版面が痩せて骨も外しやすくなる。

- **画像に日本語を焼けるモデル限定**（nano banana 2 系）。日本語非対応モデルに本文を直接渡すと
  非文字だらけのポスターになる（novtube が gemini-2.5-flash-image で実測）。
- 入力テキストの並びは **タイトル→章→概要欄→字幕全文** で固定。上限（`SOURCE_MAX_RUNES`=6000）で
  切られるのは末尾＝字幕の後ろ側なので、骨子を決める要素は必ず残る。
- キャンバス向きの指示文（`aspect_layout`）を必ず入れる。**無いとモデルが常に横並び構図を作る**
  （novtube では縦長キャンバスで上下がレターボックスになった）。
- 生成アスペクトは既定 **21:9**（表示側の安全枠 1824×650 ≒ 2.8:1 に最も近い横長比）。

```
publish infographic <edl> --title-file yt_title.txt [--prompt-only]
  --prompt-only  APIを叩かずプロンプトだけ infographic_prompt.txt に出す（**課金前の査収用**）
  実行すると EDL.infographic（path/duration_s）を書いて保存する
```
**課金なので1枚勝負**（[[paid-image-gen-one-shot-only]]）。撮り直しはユーザー判断。

### 15.2 表示は「何にも被らない安全枠」へ contain 収め
`compose/infographic_overlay.py`。ちびキャラと同じ**画面固定レイヤー**で、フレーミング crop の
影響を受けない（`EDL.overlays` のソースフレーム基準とは別系統）。

`InfographicConfig` の予約値（1080p基準・他解像度は高さ比でスケール）:

| 予約 | 既定 | 根拠 |
|---|---|---|
| `top_reserve_px` | 78 | チャプターリボン 54px ＋ 余白 |
| `bottom_reserve_px` | 352 | ちびキャラ（高さ320＋余白24）＋余白。字幕（MarginV70＋2行）より大きい方 |
| `side_margin_px` | 48 | 左右 |

→ 安全枠 **(48, 78, 1824, 650)**。21:9 の画像はここに内接して **1516×650 @ (202, 78)**。
拡大はしない／寸法は偶数へ丸める（yuv420 対策）。

- レイヤー順は **モザイクより上・ちびキャラより下**。サイズ計算がずれても
  ちび/字幕/リボンが上から描かれて隠れない。
- 表示区間は `enable='between(t,start,start+duration)'`、前後に `fade=…:alpha=1`。
  `-loop 1` の静止画入力も pts は 0 始まりで実時間と同じ進み方をするので、
  **フェード時刻は出力タイムラインの絶対秒でそのまま書ける**。
- 時刻は**本編の出力タイムライン**。イントロは別ファイルとして前に連結されるので無関係。
- 投稿単位が **2本目以降のときは出さない**（図解は収録1本ぶんの要約なので冒頭だけ）。
- **`--eyecatch` との関係**: アイキャッチ挿入（[H]）は合成後の mp4 を章境界で割って
  `[EC0][ch0][EC1][ch1]…` に組み直す後段パスなので、図解は EC0 のぶん（2秒）後ろへずれるだけ
  ＝**イントロ → アイキャッチ → 図解** の並びになる（問題なし）。
  ただし**表示中に章境界があると図解が真っ二つになる**ので、`publish infographic` は
  その場合に警告を出す（`_chapters_inside`）。出たら `--infographic-seconds` を短くする。

```
compose video <edl> --infographic [--infographic-seconds 10]
```

### 15.3 検証（2026-08-05・#103 音楽生成AI回で実生成）
- プレースホルダ（21:9）で先頭12秒を出し、リボン／ちびキャラ／字幕のいずれとも重ならないことを
  フレームで確認（`<scratchpad>/verify/ig_frame_*.png`）。
- **実生成 1枚**（`gemini-3-pro-image` / 2K / 21:9・入力2361字）→ `data/2026-08-03/infographic.png`
  （3168×1344）。実表示は **1532×650 @ (194,78)**。
- **1-shot の効き**: 4区画がチャプター（比較紹介／試聴対決／Lyria独自:動画生成／Suno独自:SE編集）に
  そのまま対応した。**数値の捏造なし**（「1曲10クレジット」「動画10秒125クレジット」
  「イヤホン片耳の不具合」「デイリー無料枠」はいずれも字幕に実在）＝プロンプトの禁止条項が効いている。
- **残る弱点**: 焼いた日本語が**1箇所だけ崩れた**（「歌詞の情報を末しない!か?」）。
  nano banana 2 でも長文パネルの吹き出しは崩れうる。**この回はユーザーが「問題なし」と判断して
  そのまま採用**（2026-08-05）。気になる回で撮り直すなら、次に効きそうな手当ては
  「画像に焼く文字は必ず正しい日本語にすること」の一文追加。
  なお**画像を差し替えるだけなら再レンダ不要**（EDL は `infographic.path` を見ているだけ）。

---

## 16. [S] 発話の「間」を一定に揃える（無音の高速化・2026-08-05 実装）

> ユーザー指示①（原文）: 「発話の間の間、システム音声が流れている期間であればそのまま流すけど、
> システム音声のない区間であれば8倍速にする」
> ユーザー指示②（同日・①の実装を見た上での指摘）: 「倍速で縮めても縮が発生するときに発話の間が
> 間延びしている感がある。……発話の終了付近からフレームを8倍速にして（字幕の消えるタイミングは
> 変えない、発話が終わるまで表示する）、発話と発話の間が縮小があろうがなかろうが**一定**になるように
> して発話のリズム感がキープされるようにして」

### 16.1 なぜ「倍率固定」ではダメだったか
無音カット後も発話の隙間（間）は残る。方式Bは読み上げ計739秒に対し出力尺910.7秒＝
**約170秒が無音**で、そのまま出すと間延びする。

最初の実装は「無音区間を一律8倍速」にした。これだと**縮めた後の間の長さが元の長さに比例**する
（4秒の間→0.5秒、10秒の間→1.25秒）。結果、**場所によって間がバラつき、会話のリズムが壊れる**。
ユーザー指摘の「縮めても間延び感がある」はこれ。

### 16.2 設計 —「速くする量」ではなく「残す間」を目標にする
**発話が終わった直後から高速化を始め、次の発話の ``target`` 秒前で通常速度に戻す。**
元の間が何秒でも、耳に残る間は必ず ``target`` 秒になる。

    (G - x) + x / f = target   →   x = (G - target) · f / (f - 1)

``x`` が空き ``G`` を超える（＝ ``G > target · f``）＝8倍では目標に届かないので、
そのときは**空き全部を高速化して倍率を ``G / target`` へ上げる**（``max_factor`` で頭打ち）。
**倍率を8固定にすると長い間だけ残ってしまい、目的（一定）が達成できない。**

``target`` は「発話が連続しているときの間」を**実測から自動決定**する
（``auto_target_gap``＝``AUTO_GAP_CUTOFF_S``(1.0秒)以下のギャップの中央値・0.10〜0.60秒でクランプ）。
方式Bは ``schedule_clips`` の ``MIN_CLIP_GAP`` がそのまま出るので綺麗に決まる。
`--speedup-gap` で明示指定も可。

**#103（方式B・通しレンダ 912.2秒）の実測**（フレーム丸め**後**の実効値）:

| | 前 | 後(目標0.15秒=自動) | 後(目標0.30秒) |
|---|---|---|---|
| 目標の間 | — | **0.15秒**（自動決定・短いギャップの中央値） | 0.30秒（指定） |
| 間の中央値 | 0.15秒 | 0.15秒 | 0.15秒 |
| 0.3秒超の間 | 53本 | **1本**（末尾の余韻だけ） | 53本（＝どれも0.30秒ちょうど） |
| 高速化区間 | — | 52区間 | 48区間 |
| 倍率 | — | 8〜69倍（中央値13.4） | 8〜34倍（中央値8） |
| 出力尺 | 912.2秒 | **795.2秒**（−117.0秒） | 803.3秒（−108.9秒） |

残る唯一の 1.52秒は**最後の発話より後ろ＝動画の最終端**で、会話の間ではない
（EDL の `out_total` より実レンダが 1.5秒長く、その差ぶんが計画の外に出るため）。

⚠️ **「計画上は一定」でも出力が一定とは限らない**。下の §16.5-1 の丸めバグでは、計画の
段階では最大0.27秒だったのに**実際の出力は最大2.20秒**（0.3秒超が48本）になっていた。
検証は必ず ``effective_plan``（＝フレーム丸め後）で行うこと。

### 16.3 速くしてはいけない区間（すべて**出力タイムライン秒**で判定）

| 種別 | 取得元 | 余白 |
|---|---|---|
| 話者音声 | 方式B=**`meta.voice.clips`**（直列スケジュール後の実クリップ）／方式A・変換無し=`voiced_word_spans` | 方式Bは0／方式Aは0.25s |
| 字幕 | (1)`hold_max`(1.0s)以内に終わる字幕だけブロック終端を延ばす (2)どの字幕も表示開始から`min_read`(2.5s) | — |
| PCシステム音声 | desktop トラックの窓RMS（`desktop_active_spans`） | 0.30s |

**図解は「速くしてはいけない区間」に入れない**。静止カードなので下の無音を詰めても
読みやすさは変わらず、縮むのは表示秒数だけ。丸ごと保護していたら**冒頭10秒の間だけ
元のまま残り**、ユーザーに「"何かありますか？"の後だけ間が長い」と指摘された
（`4.87-5.66` の 0.79秒がそこだけ手つかずだった）。代わりに `soft_regions_out` で
図解の窓を返し、`limit_shrink_in` で**表示秒数の 20%（`SOFT_SHRINK_RATIO`）までしか
縮めない**上限を掛ける。上限に当たった区間は**捨てずに短く切り詰める**
（捨てるとその間だけ元の長さで残り、かえってリズムが崩れる）。

方式Bの発話には余白を付けない。**発話の終了直後から**速くするのが仕様なので、代わりに
フレーム境界を**内側へ**丸めて（`ceil`/`floor`）発話に食い込ませない。方式A（`voiced_word_spans`）は
文字数×0.22秒で打ち切った**見積り**なので 0.25秒の余白を付ける（余白は全ギャップに等しく
乗るので「一定」は崩れない）。PC音声・図解も「イベント」として扱い、**その手前にも同じ
``target`` 秒の間を残す**。

⚠️ **字幕をまるごと塞いではいけない**（実装中に踏んだ）。方式Aの字幕は caption-summarizer の
**要約カード**で、1枚が12〜46秒も出っぱなし＝#103 では70枚で**出力尺の98%を占有**する。
まるごと塞ぐと高速化が 909.5秒中 3.3秒 しか効かなかった。また「発話に重なる字幕の終端まで
ブロックを延ばす」を無条件にやると、**1発話ごとに最大 hold_max 秒ずつ伸びて全部が繋がる**
（864.8秒＝95%が発話ブロック扱いになった）。**すぐ後で終わる字幕だけ**延長対象にすること。

**方式Aの実測**（`data/2026-08-03/edl.json`）: 目標0.19秒・40区間・**909.5→883.0秒**（−26.5秒）。
方式Bより縮まないのは正しい — 方式Aは**元の話速のまま**なので発話が尺を埋めており、
死んだ間が元々少ない（方式Bは読み上げが速いぶん間が空く）。

**PC音声のしきい値は固定にしない**（[[silence-detection-dynamic-threshold]]）。8kHz mono へ
落として 0.05 秒窓のRMS(dBFS)を取り、**20%点＝暗騒音の床から +12dB**。
ダイナミックレンジが 6dB 未満のトラックは「ずっと無音」とみなす。#103 の Zoom PC音声は
床 **-120dB（デジタル無音）/ p99 -22.8dB** の綺麗な二山で、閾値をどこに置いても
鳴っている割合は 7〜9% とほぼ変わらなかった（＝判定は安定）。
計測は全長を読むので `data/<date>/desktop_active.json` にキャッシュする。

### 16.4 実装（`compose/speedup.py` ＋ `compose video --speedup`）
**`compose/eyecatch_insert.py` と同じ「合成後の後段パス」**。本編の巨大 filtergraph や
`_src_to_out` 系のタイムライン計算には手を入れない（字幕・章・リボン・ちび・図解・BGM・
アイキャッチが全部そこに依存していて、速度変化を入れると全部の時刻が狂うため）。

計画は ``Plan = [(開始秒, 終了秒, 倍率)]``（**倍率は区間ごとに違う**）。
- `speedup_plan(edl, ranges, …)` → `(plan, info)`。info に目標の間・倍率分布・短縮秒。
- `speech_blocks_out` / `blocked_spans_out` / `auto_target_gap` / `uniform_gap_plan`
- `shift_plan_by_inserts(plan, inserts)` → アイキャッチ挿入後の時刻へ（**挿入点で分割**）
- `frame_segments` / `effective_plan` → フレーム割（下記）
- `apply_speedups(in_mp4, out_mp4, plan)` → 分割して concat
- `shifted_time` / `shift_chapter_lines` → **章時刻の補正**

**後段パスの順序は「アイキャッチ → 高速化」に固定**（`compose/cli.py`）。章時刻は
`shifted_chapter_lines`（アイキャッチ）→ `shift_chapter_lines`（高速化）の順に**1回ずつ**通す。
出力は `<name>_sp.mp4` と `<name>_sp_chapters.txt`。**高速化前の mp4 も残る**。

### 16.5 ffmpeg 側で踏んだ罠（全部実測して直した）
filtergraph の組み方は `build_filter_script`（純関数・テストあり）に集約してある。

**A/V がずれる（3つとも踏んだ）**。素直に `trim`＋`setpts/f`＋`atempo` で繋ぐと
**区間数ぶん映像と音がずれる**:

1. **境界は内側へフレームに乗せる**（`frame_segments`・`ceil`/`floor`）。倍率は整数へ丸める。
   ⚠️ かつてここで**長さを「倍率の倍数」へ切り詰めていたが、これが間のばらつきの原因だった**
   （2026-08-05 ユーザー指摘「間のばらつき直ってないよ」）。端数（最大で倍率−1フレーム＝
   69倍なら2.7秒）が通常速度で残る。**切り詰めず**、`select` が実際に出す枚数
   ``ceil(長さ/倍率)``（`seg_out_frames`）から音声の目標長を出せば A/V は合う。
   実効倍率は ``長さ/出力枚数`` なので、**章時刻の補正もそれで行う**（`effective_plan`）。
2. **映像は `fps` フィルタでなく `select` で間引く** … `setpts=/f` のあと `fps=25` を通すと
   区間の終わりに複製フレームが1枚乗り、区間数ぶん尺が伸びる。
   `select='not(n-trunc(n/f)*f)'`（式にカンマを使わない書き方）で f 枚に1枚だけ残し、
   `setpts=N/fps/TB` で振り直す。
3. **音声は atempo の出力長を信用しない** … WSOLA なのでぴったり 1/f にならない。
   `apad,atrim=0:<目標長>` で**目標長へ強制**し、さらに **0.1ms だけ映像より短く**する
   （concat は区間ごとに長い方へ揃えるので、音が1サンプルでも長いと映像が複製される）。

**ffmpeg が固まる（2つ踏んだ）**。どちらも「単体では動くのに繋ぐと止まる」ので原因が分かりにくい:

4. **引数なしの `apad` は無限に無音を作り続ける** … `atrim` は上流へ EOF を返さないので、
   `apad,atrim=0:X` は永久ループになる。必ず **`apad=whole_dur=X`** で有限にする。
5. **数フレームだけのセグメントを作ると trim/concat グラフがデッドロックする** …
   末尾に **9フレームの通常セグメント**が残った時に実際に固まった（13セグメントのうち
   12個までなら通るのに、その9フレームを足した瞬間に止まる）。`frame_segments` で
   通常側 `MIN_NORMAL_FRAMES`(12) を保証し、足りなければ**高速区間の方を削って譲る**。
   高速側が `MIN_FAST_OUT_FRAMES`(2) 枚出せないときは**捨てずに倍率を落とす**
   （捨てるとその間だけ通常速度で残り、間の均一化が崩れる）。

**エラーの読み方**: ffmpeg の stderr 末尾N行だけを見ると、libx264/aac が終了時に吐く統計
（`consecutive B-frames:` 等）に押し流されて**肝心のエラー行が見えない**（910秒の合成が
落ちた時に踏んだ）。`common.media.ffmpeg_error()` がエラーらしい行を拾うようにしてある
（compose/eyecatch/speedup/concat/intro_compose/eyecatch 全部これを使う）。

結果、短尺検証で**映像と音声の duration が完全一致**した（97.000/97.000・95.800/95.800）。
章時刻の補正は必ず `effective_plan`（＝フレーム丸め**後**の計画）で行う。

### 16.6 口パク・字幕との関係（ユーザー確認 2026-08-05）
> 「8倍の開始を前倒ししたけど、口パクは影響受けない（ちゃんと字幕同様に読み上げに連動した
> まま）ってことでOKだよね？」→ **その通り**。

* 高速化は**合成済み mp4 への後段パス**で、口パク・字幕・表情は前段の compose で既に
  焼き込まれている。
* 方式Bの発話ブロックの出所は `meta.voice.clips` ＝ `voice_tts_report.json` の
  `out_start`/`tts_s`。これは **`chibi_overlay` が口パクを作るのに使うのと同じデータ**
  （`speaking_spans_from_report`）。したがって**口が動いている区間と高速化区間は定義上
  重ならない**。フレーム境界も内側へ丸める（開始は `ceil`）ので1フレームも食い込まない。
* 字幕も同じブロックに入れている（§16.3）。
* ⚠️ **表情だけは例外的に少し影響する**。感情は発話の頭から `EMOTION_HOLD_S`(2.5秒)
  保持する仕様なので、1秒しか喋らないクリップだと残り1.5秒が高速化区間に入り、
  ノーマルへ戻るのが早くなる。**口の動きが飛ぶことはない**。

### 16.7 注意（残っている前提）
- 音は捨てずに `atempo` で一緒に速める。**無音といっても本編BGMは鳴っている**ため
  （捨てると穴が開く）。BGMは -34LUFS なので高速でも耳につかない。
- 方式Bの無音判定に**元音声の word タイミングを使ってはいけない**（§14.10-1 と同じ罠）。
- 高速化で**全体尺が縮む**＝概要欄の章時刻に波及する。`_sp_chapters.txt` を必ず使う。
- **倍率は最大69倍まで上がる**（10秒の間を0.15秒に潰すため）。一定にするには不可避だが、
  速すぎるのが嫌なら `--speedup-max-factor` を下げる（その分だけ長い間が残る）。

## 17. [S2] 高速化を「合成前」へ作り直し（読み上げ主・映像従／2026-08-05）

### 17.1 なぜ §16 の後段パスが構造的に間違いだったのか

§16 は**合成済み mp4 の無音区間を丸ごと速くする**後段パスだった。これは音声ごと
`atempo` するので、

* 間に鳴っている**BGM・PC音声まで早回し**になる
* 発話ブロックの判定を1つでも外すと**読み上げが即巻き込まれる**

ユーザー指摘（2026-08-05）:

> 本来は読み上げ・キャラの口パク・字幕は通常速度で最後まで読み上げている間に
> **収録映像だけ**が高速化することを想定しています。全部レンダリングしてから
> 高速化すりゃそりゃこうなるよ。**合成前に調整しないと**

さらに「**キャラや字幕は読み上げに連動（早くならない）ようにね**」と明示された。
＝速度が掛かるのは**収録フッテージだけ**。

### 17.2 設計 — 素材を先にワープして、合成は普通に通す

合成コア（`ffmpeg_compose`）に可変速を持ち込むと、字幕・章・リボン・ちび・図解・BGM・
アイキャッチが全部そこのタイムライン計算に依存しているのでまとめて壊れる。そこで

```
収録mp4 ──Warp──> footage_warped.mp4（映像＋PC音声だけ・出力座標）
EDL     ──Warp──> edl.warped.json（segments は全体1本・freezes なし）
→ compose video を普段どおり実行（--speedup は不要）
```

**合成コアには一切手を入れない**ので既存の動作は無傷。

### 17.3 座標系は3つある（混ぜると必ず壊れる）

| 名前 | 何 |
|---|---|
| **raw** | 収録ファイルの秒。EDL の segments / utterances / framing はこれ |
| **src'** | keep区間を連結した秒（＝**これまでの**出力タイムライン）。`_src_to_out` の値域 |
| **out** | ワープ**後**の出力秒。字幕・章・ちび・読み上げは最終的にこれ |

`Warp` は **src' → out** の区分線形写像（`compose/timewarp.py`）。

### 17.4 どこを速くするか（ユーザー指示のとおり）

1. **読み上げが鳴っているあいだは映像も等速**（見せたい所を崩さない）。
2. 読み上げに収まらず**余った素材**を、次の ``target``（既定0.15秒）の「間」へ押し込む。
   余りが上限倍率（既定 **8倍**）で収まるなら、**間がちょうど ``target`` になる倍率**
   （＝8倍以下）を使う。**8倍固定にはしない。**
3. 8倍でも収まらないときだけ、**発話映像の末尾**を8倍の対象に広げる:
   末尾を ``x`` 秒ぶん速くすると ``(R-1)·x`` だけ余分に素材を食えるので
   **``x = (余り − target·R) / (R − 1)``**。速い側が末尾に来るので直後の間と連続し、
   **発話の頭から早送りにはならない**。
4. 発話まるごと8倍でも足りないときだけ、間の倍率を ``gap_max_rate``（既定80倍）まで上げる。

5. 逆に**読み上げの方が元発話より長い**とき（映像が先に尽きる）は、
   ``lookahead``（既定 **5秒**）まで次の発話の映像へ食い込み、超えたら**フリーズフレーム**。

**PC音声が鳴っている区間は倍率1.0固定**（速くすると画面と音がずれ、音程も変わる）。

⚠️ 一度「発話全体に可変倍率（上限3倍）を掛ける」実装にしたが**誤り**。
発話の**頭から**早送りになるうえ、倍率が中途半端（1.4倍など）で不自然になる。

⚠️ 「読み上げが長いときは必ずフリーズ（先行0秒）」も**やり過ぎ**だった。テンポの速い
掛け合いでは元発話が1秒でも読み上げは2〜3秒になるので毎回フリーズが入る。
#103 実測（先行秒 → フリーズ本数/合計/最長）:

| 先行 | 0秒 | 1秒 | 2秒 | 3秒 | **5秒** | 8秒 |
|---|---|---|---|---|---|---|
| 本数 | 144 | 95 | 71 | 51 | **21** | 9 |
| 合計 | 155.6s | 109.3s | 78.8s | 55.3s | **29.6s** | 10.8s |
| 最長 | 14.6s | 13.4s | 12.6s | 11.5s | **9.6s** | 6.6s |

画面共有が主なので5秒の先行はほぼ気づかない。フリーズの方が目立つので **5秒**を既定にした。

なお**読み上げ音声が重なることは構造上ありえない**（出力タイムラインは読み上げを順に
並べて作るので、重なりは定義上発生しない）。先行/フリーズは**映像だけ**の話。

### 17.5 音は伸縮しない

映像だけが可変速で、音は**切って詰める**。読み上げ（TTS）は out 座標に置き直すだけで
**一切加工しない**。`atempo` はどこにも使わない。

### 17.6 実測（#103・方式B）

| | 値 |
|---|---|
| 出力尺 | 910.8秒 → **786.0秒**（−124.8秒） |
| Warp 区間 / レンダ片 | **448 / 637**（発話が「等速の頭＋高速の末尾」に割れるため増える） |
| 発話中の映像倍率 | 中央 **1.00倍** / 最大 8.0倍（209区間中164本は等速のまま） |
| フリーズ | 21本 / 計29.6秒（先行5秒許容時） |
| 間 | **0.15秒で一定** |
| PC音声（等速固定） | 20区間・93.5秒 |
| 映像 | **19649フレーム＝785.96秒**（計画と実測が一致・クランプ修正後） |

### 17.7 フレーム数を合わせるために踏んだ罠（4件）

1. **`fps` フィルタを使うと区間ごとに1枚落ちる**（60片で0.28秒不足・604片なら約2.5秒）。
   → `trim=start_frame:end_frame` ＋ `select` でフレーム番号のまま間引く。
2. **`select` の比を小数で書くと端で1枚落ちる**（62枚→56枚のはずが55枚。
   `62*0.903225806` が `55.99999…` になるため）。→ **整数比のまま**式に書く
   （`trunc((n+1)*56/62)-trunc(n*56/62)`）。
3. **穴で割った小片を個別に丸めると端数が積み上がる**（393区間→604片で+35フレーム）。
   → **累積丸め**（`round(累積*N) - 済`）にし、1フレーム未満の小片は**捨てる**
   （1枚に切り上げると逆に増える）。
4. **素材の外を指す片は丸ごと消える**。`trim=start_frame` が素材の最終フレームより後だと
   出力が0枚になり、`tpad=stop_mode=clone` は**複製元が無いので伸ばせない**。
   実測: 末尾のフリーズ1片（素材1048.12秒＝26203枚に対し26205枚目を要求）で
   **-30フレーム＝-1.2秒**。→ `video_frame_count()` で素材の実フレーム数を取り、
   `build_warp_video_script(..., src_frames=N)` で **最終フレームへクランプ**する。
   `nb_frames` が無いコンテナのために `-count_packets` で数える（デコードしないので数秒）。

**ログの「最大◯倍」が上限（既定8.0）を少し超えていても異常ではない**。`WarpSeg.rate` は
`src_dur / out_dur` で、`out_dur` は**フレームに丸めたあと**の値（`_snap`）。短い片ほど
丸めの影響が大きく、8倍のつもりの0.1秒の片が1フレーム短く丸まると 8.86倍と表示される。
倍率を渡している側（`emit_at`）は 8.0 を超える値を発話に渡していないので、
**誤差は最大1フレーム**。ここを「仕様違反だ」と読み違えて `speech_max_rate` を触らないこと。

加えて **concat の継ぎ目でも1枚落ちる**（区間ごとに枚数を固定しても数値が1つも変わらない
＝落ちているのは区間の中ではない）。→ **連結後に通しで `setpts=N/fps/TB`** を掛けると、
重なった時刻が無くなって CFR 化の際に捨てられなくなる。

`source.video_path` と音声トラックは**絶対パスで書く**（相対だと ffmpeg が
`No such file` で落ちる。filtergraph は一時ディレクトリ経由で渡るので cwd 依存にできない）。

### 17.8 ちびキャラは**ワープ後のレポート**を見る

`chibi_overlay` は `data_dir/voice_tts_report.json` を読む。ワープ後は読み上げの出力位置が
変わっているので、`meta.voice.warped` が真なら `warped_voice_tts_report.json` を優先する。
これを忘れると**字幕は合っているのに口パクだけ元の位置に残る**。

### 17.9 keep区間だけで割ると crop が効かない → **framing 境界でも割る**（2026-08-06 修正済み）

> 「B系統：画面クロップ機能・画面内NGワードマスキングが外れてる」

実機で確認した（`final_B.mp4` はブラウザのタブもタスクバーの時計も出ている＝全画面のまま。
同じ回の `final_A.mp4` はドキュメントに寄っていて正しい）。

**原因**（実測で特定）: `build_filter_script_framed` が **keep区間ごとに、その区間の中点の
bbox で crop** する作りだった。ワープ後の EDL は**素材が既に1本に繋がっている**ので
keep区間が1つしかなく、**91個の framing を使い分けられない**。方式Aは keep区間が214個
あったので**たまたま**成立していた。

**直し**: `framed_pieces(edl, ranges, freezes)` を新設し、keep区間を**フリーズ位置と
フレーミング境界の両方で**割る。`build_filter_script_framed` と `output_crop_segments`
（モザイク/重ねの配置区間）が**同じ関数**を使うので、crop と重ねの区間分割が必ず一致する。
非破壊で、ワープの有無に関わらず正しくなる。
`FRAMING_MIN_PIECE_S = 0.2` 未満の小片は作らない（1フレーム未満の片は concat で消える）。

| | keep区間 | 分割後の小片 | bbox が当たる小片 | crop セグメント |
|---|---|---|---|---|
| `edl.json`（方式A） | 214 | 291 | 277 | 84 |
| `edl.warped.json`（方式B） | **1** | **90** | **84** | **84** |

（修正前は方式B が 小片1 / bbox 0 / セグメント1。）

**画面内NGワードのモザイクは crop に従属する**。#103 の NG語は画面右上（1816,121 の
104×73px）にあり、crop の bbox はそこを含まない＝**crop が効いていれば切り落とされる**ので
モザイクは要らない。全画面（bbox None）の区間だけ乗る（3件中1件・2.8秒）。
修正前の方式Bは「crop が効かず全画面のまま＝NG語が映るのに、モザイクは1個だけの bbox で
写像されて画面外へ落ちる」という最悪の組み合わせだった。片方を直すともう片方も直る。

## 18. [V] Qwen3-TTS の話者同一性チェック（2026-08-05）

ユーザー指摘: 「推論によっては参照音声と**全くの別人**になるときがある」。

### 18.1 判定

`publish/_speaker_sim.py`。**numpy だけ**で完結させる（Qwen3-TTS 側の venv からそのまま
呼ぶため。引き直しを別プロセスに出すとモデル読み込み約280秒を毎回払う）。

* **声道の形** … 対数メルスペクトルの DCT（MFCC相当）の平均のコサイン類似度。c0（音量）は捨てる
* **声の高さ** … 自己相関による F0 中央値のオクターブ差

どちらも**有声フレームだけ**で測る（無音を混ぜると全員似る）。

**合格条件 = `sim ≥ 0.85` かつ `オクターブ差 ≤ 0.35`**。0.5秒未満は判定しない（材料不足で
相槌が延々と引き直しになる）。

閾値の根拠（#103・176本／**ユーザーが実音で確認**）:
別人と確認されたものの最高が **0.775**、通した側の最低が **0.852**。その間に置いた。

### 18.2 引き直しは3段

1. **シードを変える**（同じシードなら同じ別人が出るだけ）
2. **同じキャラの別参照セット**に替える
3. それでも駄目なら **`voice_tts_recheck.tsv` に出して台本の言い回しを見直す**
   （voice-scripter の**見直しモード**）

3段目はユーザー指摘による:
「参照音とニュアンスが違う文（棒読み参照音に対して『まじか！』みたいな感情表現）は
スコアが下がりやすい」。実際、最後まで残ったのは短い相槌と笑い声だった。

### 18.3 実測（#103）

| | 修正前 | 修正後 |
|---|---|---|
| yume 最小類似度 | 0.438 | **0.773** |
| priya 最小類似度 | 0.636 | **0.775** |
| オクターブ差 最大 | 0.862（ほぼ1オクターブ下＝男声化） | **0.345** |

22本の再合成で **17本を引き直し**、残った4本（「うーん」「あははは」等）を台本見直しへ回した。

### 18.4 クリップ平均だけでは「途中から別人」を取りこぼす（2026-08-05 ユーザー指摘）

ユーザー指摘: 「0m54sあたり『まぁまぁ意味わかんないですね』は別人の声です。スコアはどうなってる？」
→ **0.9533 で合格していた**。さらに「**その一文だけが別人で、後半は参照音声と同じ声**」。

平均は1本ぶんの MFCC を1つに潰すので、**崩れた1文が残りの正常な部分に薄められる**。

そこで `window_sims` で **1.5秒窓 / 0.75秒ホップ**に切り、各窓を参照と比べ、
**最悪の窓 `sim_min`** でも判定する。合格条件に `sim_min ≥ 0.80` を追加。

| | 値 |
|---|---|
| 全体 `sim`（162本） | 中央 0.966 / 5%点 0.872 / 最小 0.853 |
| **最悪窓 `sim_min`** | 中央 0.894 / 10%点 0.768 / **5%点 0.729** / 最小 0.462 |
| 指摘された idx=142 | `sim` 0.930（合格）／**`sim_min` 0.731 ＠0.00秒**（不合格） |

`sim_min` が **0.80 未満で約13%** が引っかかる。これが窓判定の代償。

**崩れるのは出だしが多い**。低い15本のうち7本が最悪窓 0.00秒。
無音のせいではない（idx=142 の先頭窓の rms 0.227 はそのクリップで最大）。
**Qwen3-TTS は喋り出しで参照に乗り切らないことがある**、という系統的な癖。
台本側の直し方（文頭の「まあまあ、」「んー、」を落とす等）は voice-scripter の見直しモードに書いた。

引き直しの**最良の回の選び方も変える**。`sim` だけで選ぶと「平均は高いが出だしが別人」の回を
最良として拾ってしまうので、**`min(sim, sim_min)`** で比べる。

**合格した行のスコアも `speaker_sim.json` に残す**。以前は不合格だけ `voice_tts_recheck.tsv`
に書いていたため、後から耳で「別人だ」と言われても**その行のスコアを確かめられなかった**
（指標を直しようがない）。分割実行なので、パスごとにクリップ名で合流させる。

## 19. [E] 感情判定を「元の収録音声」ベースへ作り直し（2026-08-05）

ユーザー指摘: 「感情判定がごみすぎる。明らかに驚いてるところで驚いた顔にならない」
「元の発話音をもとに判定するんですよね。Qwen3-TTS の推論結果だと基本棒読みなんで検出できない」

### 19.1 何が壊れていたか

* 判定単位が **utterance**（相槌をまたぐ数十秒の塊）→ 塊の先頭テキストで1回しか判定できず、
  「なるほど」に surprised が付き、実際に驚いた瞬間は normal のまま
* 判定材料が**テキストだけ**→ 声の高さ・強さ・立ち上がりが見えない

### 19.2 直し方

| 変更 | 中身 |
|---|---|
| `chibi/audio_emotion.py` ＋ `_emotion2vec_runner.py` | **元の収録マイク音声**を emotion2vec+ に掛け、**有声区間ごと**に9クラスのスコア。推論は1回だけ・結果JSONを残し閾値調整は後処理のみ（[[cache-model-forward-not-resweep]]） |
| `EDL.emotion_cues`（新設） | 感情を**時刻付きキュー**に。utterance 単位をやめる |
| `chibi/timeline.py` | キューがあれば使う。方式Bでは**読み上げクリップ位置へ写す** |
| `chibi emotions audio` | 音声判定コマンド |
| `chibi emotions prepare` | TSV が `key / time / speaker / **audio** / text`。1行＝1有声区間 |
| chibi-emotion-assigner SKILL | **audio列を最優先の根拠**にする表。`thinking` は音に出ないのでテキストから拾うハイブリッド |

9クラス→ちび6感情: `angry→angry` / `happy→smile` / `surprised→surprised` /
`sad・fearful・disgusted→troubled` / `neutral・other・unknown→normal`。

### 19.3 環境

`funasr` は `torch` を巻き込むので **wwedit の venv には入れない**
（paddle と torch が衝突した前例がある）。`.env` の `WWEDIT_EMOTION2VEC_PYTHON` に
専用 venv の python を指す。

* 専用 venv: `D:\novtube_tts\emotion2vec\.venv`（torch 2.6.0+cu124 / funasr 1.4.1）
* ⚠️ **モデルは ModelScope から落とすな**。FunASR の既定は阿里雲だが国際回線が細く
  **実測 0.4MB/s**（1.2GB に27分）。同じ重みが Hugging Face にあり **45MB/s＝27秒**。
  `_emotion2vec_runner.resolve_model()` が `iic/emotion2vec_*` を HF へ寄せる
  （ローカルパス指定はそのまま、HF に無い名前や取得失敗時は ModelScope へ戻す）。
* ⚠️ **元音声は m4a**（Zoom収録）。推論側は依存を増やさないため標準 `wave` で読むので
  そのままでは `file does not start with RIFF id` で落ちる。`decode_for_analysis()` が
  話者トラックごとに**一度だけ 16kHz mono wav へデコード**して `<EDL>/emotion_wav/`
  に置く。16kHz mono は emotion2vec の入力そのものなのでリサンプルも不要になる。

### 19.4 実測（#103・306区間）と**音声判定はどこまで効くか**

| audio の top | 件数 |
|---|---|
| neutral | 157 |
| happy | 64 |
| sad | 48 |
| disgusted | 16 |
| `<unk>` | 10 |
| **surprised** | **7** |
| angry / other / fearful | 2 / 1 / 1 |

判定を**そのまま**採ると 117件（40.6%）。ここから「yume に smile を付けない」
「テキストと突き合わせ」「同一話者×同一感情は10秒以上空ける」「1割目安」で **31件（10.8%）**。

採用31件を音声判定と突き合わせた結果:

| 最終感情 | 音声と一致 | 実態 |
|---|---|---|
| **smile** 10 | **10/10** | **完全に音声由来**（happy 0.90〜1.00）。笑いは文字に出ないので音声が無ければ1件も付かない |
| troubled 7 | 2/7 | 主にテキスト（「うまくいかなくて」）。音声は裏付け役 |
| **surprised** 9 | **2/9** | **ほぼテキスト由来。ここで音声判定は戦力にならない** |
| thinking 5 | 0/5 | 定義上、音に対応物が無い |

⚠️ **ユーザーの元の不満（驚き顔が出ない）は閾値やガードの問題ではない**。emotion2vec は
中国語音声中心の学習で、日本語の「あ、マジですか」「すごーい」を happy/neutral に寄せる。
**閾値をいくら下げても surprised は7件から増えない**。増えるのは smile と troubled だけ。

驚きを拾うなら音響量で測るのが本筋（未実装・ユーザー提示済み）:
直前0.5秒に対する **F0の急上昇＋音量の立ち上がり**。日本語の驚きは「高く・速く・短い」に
出るので意味理解より確実。`publish/_speaker_sim.py` に numpy だけの F0 抽出があるので
流用でき、**追加の重い推論は要らない**。
