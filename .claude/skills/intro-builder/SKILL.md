---
name: intro-builder
description: わくわくべんきょ会のイントロ動画([G])を作る。台本執筆・季節/服装の非重複・尺調整・生成物の目視QAというClaudeの判断を担い、決定的処理(音声合成/キャラ画/リップシンク)は publish CLI に委譲する。新規収録のイントロを作る時に使う。
---

# intro-builder — イントロ生成（Claudeの判断 ＋ publish CLI）

イントロは**判断ループ（台本執筆・服装/シチュの非重複・尺調整・目視QA）が本質**でCLI単独では作れない。
本スキルがその判断を担い、**決定的な機械処理は CLI に委譲**する。

## 使い分け
- **判断（このスキル＝Claude）**: 台本を書く／季節・服装・シチュを過去と被らせない／尺が長ければ詰める／生成画像・動画を**目視QA**して採否を決める。
- **決定的CLI**: `publish tts`（AIVis合成）/ `publish character-image`（nano banana2＋参照＋同一性）/ `publish lipsync`（DomoAI）。
- **sub-agent(Sonnet)**: 通常不要。台本を**全文字起こしから**書く必要がある時だけ（章+flow_summaryで足りるなら使わない）。

## 入力
- EDL `data/<date>/edl.json`（章・話者）。`<date>/flow_summary.txt` があれば全体像に使う。
- キャラ: 既定 **noa**（指定があれば9キャラ noa/yume/souta/tsukasa/ritsu/reika/suzu/priya/kasumi から）。

## 手順
1. **過去との非重複を確認**: メモリ `intro-generation-log` を読み、このキャラの**過去の服装/シチュ/季節**を把握（被らせない）。
2. **台本を書く**（SDD L176 調・**全角〜40字目安/≤10s**）: 「こんにちは。今日は〜してみました。詳しくは本編でどうぞ」。
   章/flow から内容を1〜2点だけ拾う。固有名は長読みでも入れすぎない（例「CVPR2026」は読みが長く尺を食う）。
3. **SBV2サーバ起動**（未起動なら・[[external-assets-and-keys]]）: github の SBV2 venv python で `tool/dub_local/server.py`(:8123) をバックグラウンド起動 → `/health` 確認。**GPU＝事前にVRAM確認**。
4. **音声合成＋尺確認**: `publish tts --text "<台本>" --voice noa --out data/<date>/intro/voice.wav`。
   出力の**実尺が >10s なら台本を詰めて再実行**（ループ）。余裕を持って ≤10s に収める。
5. **開始フレーム生成**: 収録日から季節を決め、手順1で被らない**服装/シチュの英語prompt断片(situation)**を作り
   `publish character-image --char noa --situation "<situation>" --out data/<date>/intro/frame.png`。
   → **画像を目視QA**（同一キャラか／顔40%＋・正面・口閉じ・背景クリーンか）。ダメなら situation を変えて再生成（画像は安価）。
6. **リップシンク（高コスト・$0.06/秒）**: フレームと音声が**QA合格してから**のみ実行。
   `publish lipsync --image .../frame.png --audio .../voice.wav --out data/<date>/intro/intro.mp4`（seconds は音声尺自動）。
   → 結果を**目視QA**（口元が動くか／目が閉じすぎないか／同一性）。気になれば situation/表情promptを変えて1回だけ作り直す（無駄打ち禁止）。
7. **生成ログに追記**: メモリ `intro-generation-log` に `日付/キャラ/服装/シチュ/季節` を1行追記。
8. **仕上げ合成**: `Videos/.../sounds/jingle/<sub>/` からジングルを1本選び（季節/重複は判断）、
   `publish intro-compose --video data/<date>/intro/intro.mp4 --script "<台本>" --char noa --jingle <選曲> --out data/<date>/intro/intro_final.mp4`。
   = FullHD配置＋**右上にロゴ＋本名フルネーム**（`--char` が mascot.md の本名を解決＝noaは「文月 乃亜」）＋**ピンク二重枠字幕で台本全文**＋ジングル(-20dB)。→ 完成イントロを目視QA。

## コスト規律
- **DomoAI(lipsync)だけ高い**（$0.06/秒）。**画像・音声のQAが通るまで lipsync を呼ばない**。試行は画像(セント)で。
- 検証だけなら lipsync は1秒で（`--seconds 1`）。本番は音声尺。

## 罠（[[external-assets-and-keys]]に詳細）
- 音声 **≤10s**（尺ループ必須）。キャラ画は `<id>_a*.webp` を参照・**chibi除外**・同一性維持。
- DomoAI は host=`api.domoai.com`＋Cloudflare対策UA（`publish/domoai.py` が処理済）。
- SBV2サーバは **github の SBV2 venv** で起動（`server.py` の SBV2_ROOT が github 側に解決）。
