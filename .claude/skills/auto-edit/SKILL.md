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
  ⚠️ **G2 の手修正後にやり直す時は「編集位置」を絶対に動かさない**（カット境界・framing区間・章の
  `start_at`）。作り直していいのは**中身だけ**＝字幕テキスト・章タイトル・話者ラベル。
  上流をやり直す必要が出たら、EDLを**先にバックアップ**し、`transcribe run` のように
  `utterances` しか書かないコマンドを選ぶ（`ingest init`/`cut auto-vad`/`framing scenes` は
  区切りを作り直すので**再実行しない**）。章タイトルだけ直す時は `chapter apply` を使わず、
  `start_at` を固定したままタイトル/話者を書き換えること。
- **G-V キャラ割当承認**（[V] キャラ声差し替えを選んだ時のみ）: `publish voice-cast` が提示する
  **話者→のべつべキャラのランダム割当**（字幕色＝キャラテーマ色・左右配置つき）を承認してもらう。
  「リロール」＝再実行、「○○にして」＝`--chars` 指名。あわせて **ちびキャラ不足アセットの課金生成**
  （`chibi ensure` が枚数を提示・nano banana 2 lite・1枚勝負）もここで承認を取る。
- **G-文言 課金前の文言審査（必須）**: **お金のかかる生成を回す前に、文言をまとめて提示して承認を取る**。
  対象＝**イントロ台本（読み＋字幕の両方）／動画タイトル（回数込み）／サムネに描く文字／
  [I] インフォグラフィックの入力（タイトル・章一覧・概要欄。`--prompt-only` で出す）**。
  ⚠️ **[I] を使う回はこのゲートを手順10(compose)の前へ前倒しする**（図解は compose に焼き込むため）。
  理由: TTS→キャラ画→**lipsync($0.06/秒≒$0.60)**／サムネ(nano banana)は**作り直すたびに課金**される。
  文言が確定してから初めて `publish tts` 以降・`publish thumbnail` を回す。**回数(#NN)も必ずここで確認**（推測しない）。
- **G3 投稿前 最終承認**: 完成動画＋サムネ＋概要欄を見せ、**YouTube下書き化の直前**で承認を取る。
- ※**承認済み文言で作った生成物（イントロ動画・サムネ画像）は、見せて自動で次へ**（直しは言われたら対応）。
- ⚠️ **課金生成は1回ずつ。作り直しは勝手に回さない**＝キャラ画/サムネ(nano banana 2・**1枚10円超**)も
  「安いから試行」ではない。品質が気になっても**まず見せて、撮り直すかはユーザーに決めてもらう**。

## 実行順（§8 RUNBOOK・各工程の自動化記号は §8 準拠）
1. **G1 前提チェック**: `.env`キー・extras同期・SBV2素材・BGMフォルダ存在を確認。**BGMジャンルを選んでもらう**。足りない物だけ G1 で確認。
   あわせて **[V] キャラ声差し替えの方式**を聞く（AskUserQuestion）:
   **なし** / **方式A: Seed-VC**（声質変換＝タイミング完全維持・笑い声も変換される）/
   **方式B: TTS読み上げ**（文を整形して Qwen3-TTS 合成＝クリアな読みだが、笑い声等の
   非言語音は消え、尺超過時は画面フリーズが入り得る）。差し替え時はちびキャラ2体が
   画面下に常時表示される（ゆっくり風・話者側だけ口パク）。
2. `[CLI]` **ingest** → `data/<date>/edl.json`。
   ⚠️ **1話者につきトラックは最大2本**＝**マイク（発話）とPC音声（画面共有で流した音楽等）**。
   Zoomは「その人の表示名の別枠」としてPC音声を書き出すので、**同じ表示名で連番違いの2本**になる
   （例 `audioTaniguchi2…`=発話 / `audioTaniguchi3…`=PC音声）。**参加者それぞれのPCから入り得る**
   （Taniguchi・sakamoto 両方が持つこともある）。`ingest` が連番の小さい方を発話、それ以外を
   `is_desktop_audio` と判定する。**PC音声は合成には混ぜるが文字起こししない**。
   → **ingest 後に `edl.source.audio_tracks` の `speaker` と `is_desktop_audio` を必ず目視確認する**
   （ここを取り違えると、音楽をSTTした幻聴の語が字幕に入り、かつ**本当の発話が丸ごと落ちる**。
   2026-08-03 で実際に踏んだ＝Taniguchi の発話4139語が消え、字幕が片方の話者だけになった）。
3. `[CLI][GPU]` **transcribe**（VRAM確認）。
   → 実行後、**話者ごとの語数が発話量の実感と合うか**を確認する。主発表者の語数が極端に少ない・
   ある話者が0語なら**トラック取り違え**を疑う（先へ進むと字幕/章まで作り直しになる）。
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
8. **字幕**（G1で選んだ音声方式で**分岐する。両方走らせない**）:
   - **方式なし / 方式A(Seed-VC)**: `[CLI]`+**caption-summarizer スキル(Sonnet)**
     **subtitle** `prepare-captions`→（summarizer）→`apply-captions`（要約字幕）。
   - **方式B(TTS読み上げ)**: `publish voice-cast --method tts` → `publish voice-tts-prepare`
     →**voice-scripter スキル(Sonnet)**→ `publish voice-tts-subtitles`（読み上げ文の字幕を先に貼る）。
     ⚠️ **方式Bで caption-summarizer を走らせない**。方式Bの字幕は読み上げ文そのものなので、
     要約字幕は finalize で捨てられる＝**同じ文字起こしを2回LLMに読ませる無駄**になる
     （実測: 要約字幕70枚/1370字 に対し読み上げ字幕230枚/5041字。入力は 79KB と 29KB）。
     字幕の直しは EDL ではなく **`voice_tts_decisions.json` 側**でやる（finalize が貼り直すため）。
9. **=== G2 編集確認 ===** `edit serve <edl>`（httptools破損中は `http="h11"`）。手修正完了後 `framing harvest-corrections <edl>`。
9.5. **[V] キャラ声差し替え＋ちびキャラ**（G1で「なし」以外を選んだ時のみ。**G2の後・composeの前**。
   カット境界・framing・章 `start_at` は一切動かさない＝加算情報のみで G2 ルール適合）:
   1. `publish voice-cast <edl> --method <seedvc|tts>` → **=== G-V ゲート ===**（割当キャラ・字幕色・
      左右配置を提示して承認。リロール=再実行／指名=`--chars noa,suzu`／全て戻す=`publish voice-revert`）。
      ※方式Bは手順8で voice-cast 済みなので、ここでは G-V の承認だけ取る。
   2. **[E] 感情割当**: `chibi emotions audio`（**元の収録マイク音声**を emotion2vec+ に掛ける・
      合成音は棒読みなので使わない）→ **chibi-emotion-assigner スキル(Haiku可)**
      `chibi emotions prepare` →（スキル）→ `chibi emotions apply`。
      判定単位は**有声区間**（utterance ではない）で、結果は `EDL.emotion_cues`＝**時刻付き**。
      スキルは TSV の `audio` 列（音声モデルの判定）を最優先の根拠にし、
      音に出ない `thinking` だけテキストから拾う。
      ⚠️ `funasr` は torch を巻き込むので**専用 venv** に入れ、
      `.env` の `WWEDIT_EMOTION2VEC_PYTHON` で指す（既存: `D:\novtube_tts\emotion2vec\.venv`）。
      モデルは **Hugging Face から**落ちる（コードが自動で寄せる。ModelScope は 0.4MB/s しか
      出ず1.2GBに27分かかる）。元音声が m4a でも自動で 16kHz mono wav にデコードする。
      **方式A(Seed-VC)でも同じ手順**（判定入力は元の収録マイク音声なので方式に依存しない）。
      ⚠️ 音声モデルは日本語の**驚きをほとんど拾えない**（実測 306区間中 surprised は7件）。
      `smile` は音声が根拠、`surprised`/`thinking` はテキストが根拠になる。
   3. `chibi ensure <edl>` — 不足アセット（キャラ×使用感情の口ペア画像＝**課金**・rembg背景抜き）を
      列挙し **G-V で承認済みなら `--yes`**。初回はベース取り込みも自動。
      rembg 未導入なら `UV_LINK_MODE=copy uv pip install "rembg>=2.0.59" "onnxruntime>=1.17"`。
      口パクは閉/開の2枚を離散切替する（中間フレームは作らない）。
      **ベースの口が笑い口のキャラは** `chibi gen <char> normal --redraw-closed --force` で
      口閉じを描き直す（priya が該当。笑い口↔丸口の往復になって不自然）。
   4. 音声変換（`nvidia-smi` でVRAM確認してから）:
      - **方式A**: `publish voice-convert <edl> --max-chunks 10` を**「残り0」表示まで前景で繰り返す**
        （分割実行・再開可＝[[background-tasks-get-reaped]]）。処理は 0.8〜0.9×実時間、
        モデルロードが毎回約100秒なので**バッチは 600秒に収まる範囲で大きめ**が得。
      - **方式B**: 台本は手順8で作ってあるので、ここは `publish voice-tts`（合成）→
        `publish voice-tts-finalize` だけ。
        ⚠️ **`--max-jobs` で分割するな**（既定0＝全部）。**1回の実行につきモデル読み込みが
        約105秒**かかるので、分けるほど遅くなる。実測（#103・120本）: 合成そのものは37.8分
        （1本 中央18.9秒・GPU律速）なのに、60本ずつ分けたせいで読み込みが**21分ぶん**乗った。
        再開はクリップ横の `u####.txt` サイドカーで効くので、分割しなくても打ち切って安全。
        **SSDへ置き替えても速くならない**（重み3.86GBの読み出しは HDD 31秒 → SSD 8秒。
        1回23秒しか縮まない）。1本19秒は RTX 2070 SUPER で 1.7B を自己回帰デコードする
        妥当な線で、ここを縮めたければカードを替えるしかない。
        **手順8の prepare は `screen_text.txt`（手順7の `chapter screen-text`）を文脈に付ける**。
        スキルは読み上げ文（`voice_tts_decisions.json`）に加えて
        **用語表記の対応表 `voice_tts_terms.json`** を作る＝**読み上げは誤読しないカタカナ**
        （`Lyria 3.5`→「リリア3.5」）／**字幕はOCR由来の正式表記**（→`Lyria 3.5`）に戻す。
        これが無いと字幕に「リリア3.5」と出て表記ゆれになる。
        読み上げ文や用語表記だけ直した時は **`voice-tts-finalize --subtitles-only`**
        （全長トラック約100MB×人数の作り直しを省く）。
        読み上げ単位は**ターン**（`tts_units`）で、utterance ではない。utterance は相槌をまたぐ
        数十秒〜100秒超の塊で相手のターンを内包するため、そのまま読ませると**2人が同時に喋る**。
        そのターンを **`tts_clips` がさらに「文」へ割る＝1文＝1クリップ**。
        **合成の単位＝後処理（話者チェック・字幕1枚・口パク・表情）の単位**にするため。
        ターン丸ごとで合成していた頃は、3文入りの行で1文だけ別人の声になっても
        クリップ平均に薄まって検出できず、直すときも丸ごと引き直しだった（2026-08-06 指摘）。
        実測 137ターン → **231クリップ**（1本 平均24字）。
        recheck の `idx` は `17`＝ターン全体／`17.1`＝そのターンの2文目（0起点）。
        **1文だけのターンはファイル名が `u0017.wav` のままなので、既存クリップを再利用できる**。
        `voice-tts-subtitles` と `voice-tts` は **60字を超える1文を警告する**（止めない）。
        出たら voice-scripter で「。」を打つ＝**長い1文はそこだけ粒度がターン単位に戻る**。
        合成後は `schedule_clips` が**全部を直列**に並べる。**間は 0.15秒固定**
        （最小値ではない。元の会話の長い沈黙は残さない）。**唯一の例外が
        PCシステム音が鳴っている区間**で、そこは詰めない（デモの音が途切れる）。
        ⚠️ **台詞は絶対に重ねない。**「相槌だけ相手の声に重ねる」は**却下済みの設計**で、
        コードから削除済み（`schedule_clips` に重ねる引数は無い／`--bc-gain` も無い）。
        重なりは配置で解く問題ではなく、**台本の時点でターン（タイミング）を加味して
        書くことで起きなくする**。voice-scripter が合いの手を隣のターンへ吸収する。
        ユーザーから複数回同じ指摘を受けた箇所なので、**重ねる方向へ戻さないこと**。
        回帰テストは `tests/test_voice_tts_schedule.py`。
        口パク・感情・字幕はすべて**読み上げクリップの位置**に合わせる（元発話位置ではない）。
        **合成は話者同一性を自動チェックする**（参照音声と別人になることがある）。
        `sim < 0.85`／**最悪窓 `sim_min < 0.80`**／F0のオクターブ差 > 0.35 のどれかなら
        **シードを変えて → 別参照セットで**引き直す。それでも駄目な行は
        `voice_tts_recheck.tsv` に出るので **voice-scripter を見直しモード**で回して
        言い回しを落ち着かせ、`voice-tts` を再実行する
        （棒読みの参照音に強い感情表現を当てるとスコアが落ちる）。
        ⚠️ **クリップ全体の平均だけで見ない**。Qwen3-TTS は**1本の中で途中から別人になる**
        （実測: 前半だけ別人なのに全体 0.9533 で合格していた）。窓ごとに測った `sim_min` と
        崩れた秒 `at` が recheck に出る。`at` が 0.0 付近＝**出だしだけ崩れている**ので、
        台本の文頭を直す（「まあまあ、」「んー、」で始めない）。
        **合格した行のスコアも `speaker_sim.json` に全部残る**（後から耳で指摘されたときに
        その行の数字を確かめられないと、指標を直しようがない）。
        既存の合成を後から監査したいときは wav と `refs/<char>/*.wav` を
        `publish._speaker_sim.compare` に掛ける。
      **合成した声トラックは自動で正規化される**（A/B 共通・`assemble_track(normalize=True)`）。
      目標は **-16 LUFS / TP -1.5dB ＝ 収録音の整音と同じ値**。Seed-VC も Qwen3-TTS も
      出力レベルが揃わず（実測 #103: 話者間で 2.4〜2.6dB 差・Seed-VC は **TP +0.38dB で
      クリップ**）、そのままだと BGM が相対的に浮く。ログに掛けた後の LUFS/TP が出るので、
      **-16 付近に揃っていることを確認する**。
      ⚠️ PCシステム音のトラックには掛けない（共有された音楽の強弱と相対音量をそのまま残す）。
      ⚠️ **合成をやり直す必要はない**。正規化は出力 wav への後処理なので、既存の合成に対して
      `voice-convert`／`voice-tts-finalize` を回し直すだけで掛かる。
   5. `chibi preview <edl> --seconds 30` を SendUserFile で提示（口パク同期・声質の目視/試聴。
      承認待ちにはしない）。
9.7. **[I] 本編冒頭の要約インフォグラフィック**（**compose の前**。図解は課金生成なので
   **G-文言 をこの時点まで前倒しして**タイトル/Agenda ごと承認を取る）:
   1. タイトル案と Agenda を決め（`yt_title.txt` / `agenda.txt`）、`publish description <edl> …` を
      先に回して `youtube_description.txt` を作る。※アイキャッチによる**章時刻のずれは図解に無関係**
      （図解が使うのは章の**並びとラベル**）。手順13で `--chapter-lines-file` を付けて概要欄を作り直す。
   2. `publish infographic <edl> --title-file yt_title.txt --prompt-only` で
      `infographic_prompt.txt` を出し、**入力（タイトル/章/概要欄）を G-文言 で査収**してもらう。
   3. 承認後 `publish infographic <edl> --title-file yt_title.txt`（nano banana 2・**1枚勝負**）。
      EDL に `infographic.path` / `duration_s`(既定10秒) が書かれる。
      表示サイズは compose 側が**上部リボン・ちびキャラ・字幕に被らない安全枠**へ自動で収める。
10. `[CLI]` **compose video** `--framed --subtitles --audio speakers --bgm "<G1で選んだジャンル>"` **`--chapter-ribbon`** **`--eyecatch`** **`--speedup`** → 本編mp4（`*_ec_sp.mp4`）。
   **`--bgm-avoid-desktop` は回ごとの判断**（既定OFF）。その回の**PC音声そのものを聴かせる**
   とき（音楽生成の聴き比べ・デモ音の比較など）だけ付けると、鳴っている間だけ BGM が落ちる
   （**前後0.6秒のフェード付き**＝いきなり消さない）。普段は付けない（PC音声の上にも BGM を敷く）。
   **BGM の音量も回ごとの判断**。既定は `--bgm-target-lufs -34`（カフェBGM並み）で、
   これは**変えない**。ただし**PC音声が長く鳴る回は BGM が相対的に浮く**——
   `build_speaker_mix_filter` は「声＋PC音声を混ぜた全体」を `loudnorm I=-16` する一方、
   BGM はその後に**絶対値**で足されるので、PC音声に引かれて声が下がると BGM だけ残る。
   そういう回は**その回だけ** `--bgm-target-lufs` を下げる（#103 は -40＝既定より -6dB）。
   ⚠️ **`--framed` はワープ後(方式B)でも効く**（2026-08-06 修正済み）。
   keep区間だけでなく**フレーミング境界でも区間を割る**ようになったため。
   画面内NGワードのモザイクは crop に従属する＝crop で切り落とされる区間には出ず、
   全画面（bbox なし）の区間にだけ乗るのが正しい挙動。**全画面のままNG語が映っていたら
   crop が効いていない**ので、`--framed` の付け忘れか framing 未割当を疑う。
   **[I] 実施時は `--infographic` が自動でON**（EDL.infographic.enabled）＝本編冒頭10秒に要約図解。
   **[V] 実施時は `--chibi` が自動でON**（EDL.chibi.enabled）＝字幕はキャラテーマ色・リボンも同系色・
   声はキャラ声（`voice_path` 自動反映）。方式Bのフリーズは映像/字幕/章時刻/概要欄へ自動反映される。
   アイキャッチの一言ボイスも cast の2キャラに揃えると世界観が締まる（任意）。**[H]** で全チャプター冒頭に2秒のgenerative-artアイキャッチを挿入。
   **アイキャッチの音は のべつべ！キャラの「一言」ボイス**（`--eyecatch-voice` 既定ON・**音楽ジングルは廃止**）＝
   SBV2日本語モデルの**キャラを章ごとにランダム**に選び、「つ～ぎ！」「さてと」等の短い一言を**都度合成**して喋らせる。
   イントロと同じく**右上にロゴ＋キャラ名バッジ**を出す。台詞/キャラの定義は `publish/eyecatch_voice.py`
   （読みは**かな書き**＝SBV2は漢字/英字を誤読）。SBV2サーバ未起動なら `--eyecatch-jingle-dir` の音楽へ自動退避。
   **[S2] 高速化は `compose video` の前に `compose warp` で掛ける**（方式B）。
   ⚠️ 後段パスの `--speedup` は**音声ごと早回しする**ので使わない（BGM・PC音声まで速くなり、
   判定を1つ外すと読み上げが巻き込まれる）。速度が掛かるのは**収録フッテージだけ**で、
   読み上げ・字幕・口パク・ちび・リボン・図解は**通常速度のまま**連動させる。

   ```
   compose warp <edl>            # → footage_warped.mp4 ＋ edl.warped.json
   compose video <edl>.warped.json --framed --subtitles --chibi ...   # --speedup は付けない
   ```

   発話の**頭は等速**、余った映像を次の `--gap`（既定0.15秒）へ押し込む。8倍で収まるなら
   間がちょうど 0.15秒になる倍率（8倍以下）を使い、収まらないときだけ**発話の末尾**を
   `--speech-rate`（既定8倍）の対象に広げる。読み上げの方が長ければ `--lookahead`
   （既定5秒）まで先の映像へ食い込み、超えたら**フリーズ**。
   **PC音声が鳴っている区間は等速固定**（読み上げ側も、そこの間は 0.15秒に詰めない）。
   台詞は重ねないので、読み上げの並びがそのまま出力の並びになる。
   #103 実測: 910.8→**786.0秒**、発話中の倍率は中央1.00倍/最大8.0倍、フリーズ21本/29.6秒。
   `--dry-run` で計画だけ確認できる。
   **章時刻は `compose video` 側で出る `*_chapters.txt` を使う**（ワープ後の時刻で出る）。
   **挿入で章時刻がずれるので `*_ec_chapters.txt`（補正済み）を概要欄に使う**。**`--chapter-ribbon`** は左上に収録日＋章名の2段リボンを常時表示（章ごとに**話者色**＝chapterの `speaker` 属性で色分け・字幕と同系統。mossan-hoshi=青/taniguchi=紫等）。収録日は `data/<date>/` の日付から自動（`M/D収録`）。
11. **=== G-文言 課金前の文言審査 ===** イントロ台本（読み＋字幕）・**タイトル（#NN 込み・回数は推測せず確認）**・
    サムネに描く文字を**まとめて提示して承認を取る**。承認後に手順11以降の課金生成へ進む。
12. **intro-builder スキル**: イントロ生成（服装非重複/尺/QAは intro-builder が判断・**台本は承認済みのものを使う**）。生成物を見せ**自動で次へ**。`publish intro-compose` で仕上げ合成（本編先頭に連結）。
13. `[CLI]` **[L]**: サムネは **`publish thumbnail --char <キャラ> --prompt "<文字・配色・構図・表情まで記述>"`＝nano banana 2 一発生成**（キャラ・背景・日本語タイトル文字を一括描画。立ち姿参照で絵柄/キャラ固定。PIL帯合成は廃止＝[[thumbnail-oneshot-nano-banana]]）／タイトル・要約を書き `publish description`。**後段パスを使ったら `publish description --chapter-lines-file <最後に出た *_chapters.txt>`** で補正章時刻を反映（アイキャッチのみ=`*_ec_chapters.txt` / 高速化まで=`*_ec_sp_chapters.txt`）。見せて**自動で次へ**。
    ※**サムネ/イントロのキャラは回ごとに指定されうる**（既定 noa。ユーザー指示があればそのキャラ＝`--char yume` 等）。
    ※`publish description` は最後に**YouTubeのチャプター条件**（先頭00:00／3個以上／昇順／**各章10秒以上**）を検査し、
      破っていれば異常終了する（条件を1つでも破ると章が**1つも出ない**＝#101 は先頭章9秒で全滅した）。
      止まったら**短い章を隣と統合**して `chapter apply` からやり直す。`publish youtube` も投稿直前に同じ検査をする。
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
