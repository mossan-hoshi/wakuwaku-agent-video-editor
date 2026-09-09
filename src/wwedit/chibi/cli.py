"""``wwedit chibi`` サブコマンド（[V] ゆっくり風ちびキャラ）。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich import print as rprint

from wwedit.edl.schema import load_edl, save_edl

chibi_app = typer.Typer(help="ゆっくり風ちびキャラ（アセット/感情/口パク）", no_args_is_help=True)
emotions_app = typer.Typer(help="感情割当（chibi-emotion-assigner スキルの入出力）",
                           no_args_is_help=True)
chibi_app.add_typer(emotions_app, name="emotions")


@chibi_app.command(name="base")
def base_cmd(
    char: str = typer.Argument(..., help="キャラID（noa/suzu/...）"),
    force: bool = typer.Option(False, "--force", help="既存の背景抜き結果を作り直す"),
) -> None:
    """ベースちび画像を novtube から取り込み背景抜きする（課金なし）。"""
    from wwedit.chibi.assets import ensure_base

    try:
        p = ensure_base(char, force=force)
    except FileNotFoundError as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    rprint(f"[green]ベース[/]: {p}")


@chibi_app.command(name="base-gen")
def base_gen_cmd(
    char: str = typer.Argument(..., help="キャラID"),
    model: str = typer.Option(None, help="画像モデル（既定=nano banana 2）"),
    yes: bool = typer.Option(False, "--yes", help="承認ゲートをスキップ"),
    force: bool = typer.Option(False, "--force", help="既存ベースを引き直す（明示リテイク）"),
) -> None:
    """ちびベースを**描き起こす**（**課金1枚**）。既存素材のキメポーズを捨てたいとき用。

    参照は フルアート `<char>_a*.webp`（誰か・服装）→ 自分の既存ちび絵 → 他キャラのちび絵
    （画風・構図のみ）。腰から上・正面・両腕を下ろした中立の立ち姿で描かせる。
    """
    from wwedit.chibi.assets import (
        BASE_CHIBI_MODEL,
        chibi_base_prompt,
        generate_chibi_base,
    )

    model = model or BASE_CHIBI_MODEL
    rprint(f"[cyan]ベース描き起こし[/]: {char}（課金 1 枚・model={model}）")
    rprint(f"[dim]--- prompt ---\n{chibi_base_prompt(char)}[/]")
    if not yes and not typer.confirm("生成しますか?（課金が発生します）"):
        raise typer.Exit(1)
    try:
        p = generate_chibi_base(char, model=model, force=force)
    except (FileExistsError, FileNotFoundError, RuntimeError) as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    rprint(f"[green]ベース[/]: {p}")


@chibi_app.command(name="base-rebuild")
def base_rebuild_cmd(
    char: str = typer.Argument(..., help="キャラID"),
    bottom_frac: float = typer.Option(
        0.88, "--bottom-frac", help="残す割合（キャラ実効高さに対する）。1.0=切らない"),
) -> None:
    """`base_gen.png` から**下流を全部作り直す**（**無課金**）。

    `base_gen.png`（生成が返した生画像）を手で描き直したあとに叩く。
    背景抜き → 1024正規化 → 腰上トリム → キャンバス正規化 を通しでやり直す。

    手で切り抜いた**透過PNG**を置いた場合はそのアルファをそのまま使う（抜き直さない）。
    白背景のままなら白背景マットで抜く。
    """
    from wwedit.chibi.assets import char_dir, remove_bg, trim_chibi_base
    from wwedit.chibi.geometry import REF_CANVAS

    d = char_dir(char)
    gen = d / "base_gen.png"
    if not gen.exists():
        rprint(f"[red]生画像が無い[/]: {gen}")
        raise typer.Exit(1)
    remove_bg(gen, d / "base_untrimmed.png", size=(REF_CANVAS, REF_CANVAS))
    p = trim_chibi_base(char, bottom_frac)
    rprint(f"[green]再構成[/]: {p}（{gen.name} → 背景抜き → トリム {bottom_frac}）")


@chibi_app.command(name="base-trim")
def base_trim_cmd(
    char: str = typer.Argument(..., help="キャラID"),
    bottom_frac: float = typer.Option(
        0.88, "--bottom-frac", help="残す割合（キャラ実効高さに対する）。0.88=腰上"),
) -> None:
    """ベースの下側を切って**腰上に詰める**（**無課金**・何度でもやり直せる）。

    生成AIが脚まで描いてしまったときに引き直す代わりに使う。切る前の絵は
    `base_untrimmed.png` に残り、常にそこから切り直すのでフラクションを振っても劣化しない。
    """
    from wwedit.chibi.assets import trim_chibi_base

    try:
        p = trim_chibi_base(char, bottom_frac)
    except (FileNotFoundError, ValueError) as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    rprint(f"[green]トリム[/]: {p}（bottom_frac={bottom_frac}）")


@chibi_app.command(name="gen")
def gen_cmd(
    char: str = typer.Argument(..., help="キャラID"),
    emotion: str = typer.Argument(
        ..., help="感情（normal/smile/surprised/troubled/angry/thinking）"
    ),
    model: str = typer.Option(None, help="画像モデル（既定=nano banana 2 lite）"),
    yes: bool = typer.Option(False, "--yes", help="承認ゲートをスキップ（承認済みの時のみ）"),
    force: bool = typer.Option(False, "--force", help="既存を作り直す（1枚勝負の明示リテイク）"),
    redraw_closed: bool = typer.Option(
        False, "--redraw-closed",
        help="normal の口閉じもAIに描かせる（ベースの口が笑い口で口パクに合わないキャラ用）"),
) -> None:
    """感情×口閉/口開ペアを生成する（**課金**・承認ゲートあり・1枚勝負）。"""
    from wwedit.chibi.assets import (
        CHIBI_EMOTIONS,
        DEFAULT_CHIBI_MODEL,
        REDRAW_CLOSED_CHARS,
        check_pair_alignment,
        chibi_emotion_prompt,
        generate_mouth_image,
        mouth_pair_paths,
    )

    if emotion not in CHIBI_EMOTIONS:
        raise typer.BadParameter(f"感情は {'/'.join(CHIBI_EMOTIONS)} のいずれか")
    model = model or DEFAULT_CHIBI_MODEL
    # ベースが平常表情でないキャラ（noa=ウインク / priya=笑い口）は既定で描き直す
    redraw = redraw_closed or char in REDRAW_CLOSED_CHARS
    closed_p, open_p = mouth_pair_paths(char, emotion)
    todo = [m for m, p in (("closed", closed_p), ("open", open_p))
            if force or not p.exists()]
    paid = [m for m in todo
            if not (m == "closed" and emotion == "normal" and not redraw)]
    if todo:
        rprint(f"[cyan]生成対象[/]: {char}/{emotion} → {', '.join(todo)}"
               f"（課金 {len(paid)} 枚・model={model}）")
        for m in todo:
            rprint(f"[dim]--- prompt ({m}) ---\n{chibi_emotion_prompt(char, emotion, m)}[/]")
        if not yes and not typer.confirm("生成しますか?（課金が発生します）"):
            raise typer.Exit(1)
        for m in todo:
            try:
                p = generate_mouth_image(char, emotion, m, model=model, force=force,
                                         reuse_base=not redraw)
            except (FileExistsError, FileNotFoundError, RuntimeError) as e:
                rprint(f"[red]{e}[/]")
                raise typer.Exit(1) from e
            rprint(f"  {m}: {p}")
    if closed_p.exists() and open_p.exists():
        drift = check_pair_alignment(closed_p, open_p)
        flag = "[yellow]位置ドリフト大（顔が泳ぐ可能性）[/]" if drift > 0.05 else "OK"
        rprint(f"  整合: 口以外の差分率 {drift:.3f} {flag}")


@chibi_app.command(name="ensure")
def ensure_cmd(
    edl_path: Path = typer.Argument(..., help="対象 EDL（voice-cast/感情割当 済み）"),
    yes: bool = typer.Option(False, "--yes", help="承認ゲートをスキップ"),
    model: str = typer.Option(None, help="画像モデル（既定=nano banana 2 lite）"),
) -> None:
    """EDLに必要なちびアセット（キャラ×使用感情）の不足分を列挙→承認→一括生成する。"""
    from wwedit.chibi.assets import DEFAULT_CHIBI_MODEL, missing_assets

    edl = load_edl(edl_path)
    if not edl.character_cast:
        raise typer.BadParameter("character_cast が無い（先に publish voice-cast）")
    chars = sorted(set(edl.character_cast.values()))
    emotions = sorted({u.emotion for u in edl.utterances if u.emotion} | {"normal"})
    missing = missing_assets(chars, emotions)
    if not missing:
        rprint(f"[green]アセットは揃っている[/]（{'/'.join(chars)} × {'/'.join(emotions)}）")
        return
    paid = [m for m in missing if m[2] in ("closed", "open")
            and not (m[2] == "closed" and m[1] == "normal")]
    rprint(f"[cyan]不足アセット[/] {len(missing)} 件（うち課金 {len(paid)} 枚・"
           f"model={model or DEFAULT_CHIBI_MODEL}）:")
    for c, e, w in missing:
        rprint(f"  {c}/{e or '-'}: {w}")
    if not yes and not typer.confirm("生成しますか?（課金が発生します）"):
        raise typer.Exit(1)
    # base → gen(closed/open) の順で埋める
    done_pairs: set[tuple[str, str]] = set()
    for c, e, w in missing:
        if w == "base":
            from wwedit.chibi.assets import ensure_base

            ensure_base(c)
            rprint(f"  base: {c} OK")
        elif w in ("closed", "open") and (c, e) not in done_pairs:
            gen_cmd(char=c, emotion=e, model=model, yes=True, force=False,
                    redraw_closed=False)
            done_pairs.add((c, e))
    rprint("[green]chibi ensure 完了[/]")


def _default_chars() -> list[str]:
    """オリジナルキャラ全員（``tsukuyomi`` は他社キャラなので除外）。"""
    from wwedit.publish.voice_cast import NON_ORIGINAL_CHARS
    from wwedit.subtitle.ass import CHAR_THEME_HEX

    return sorted(set(CHAR_THEME_HEX) - set(NON_ORIGINAL_CHARS))


@chibi_app.command(name="ensure-all")
def ensure_all_cmd(
    chars: str = typer.Option("", help="対象キャラ（カンマ区切り。既定=オリジナル全9体）"),
    emotions: str = typer.Option("", help="対象感情（カンマ区切り。既定=全6種）"),
    blink: bool = typer.Option(True, "--blink/--no-blink",
                               help="瞬き素材（目つむり1枚/キャラ）も作る"),
    model: str = typer.Option(None, help="画像モデル（既定=nano banana 2 lite）"),
    yes: bool = typer.Option(False, "--yes", help="承認ゲートをスキップ"),
    force: bool = typer.Option(False, "--force", help="既存も作り直す（要 --backup 検討）"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="計画と前提だけ確認する（課金なし）"),
    limit: int = typer.Option(0, help="今回の課金枚数の上限（0=無制限）"),
    backup: bool = typer.Option(True, "--backup/--no-backup",
                                help="--force 時に既存キャラdirを退避（assets は git 管理外）"),
) -> None:
    """EDL に依存せず**全キャラ分**のちびアセットを揃える（収録前のまとめ生成）。

    ``ensure`` は「この EDL に必要な分」なので EDL 必須。こちらは全体生成用。
    **1枚失敗してもバッチ全体は止めず**、最後に失敗一覧を出す（110枚規模で途中終了すると
    再開が面倒なため）。
    """
    import shutil
    from datetime import datetime

    from wwedit.chibi.assets import (
        CHIBI_EMOTIONS,
        DEFAULT_CHIBI_MODEL,
        assets_root,
        char_dir,
        detect_regions,
        ensure_base,
        generate_eyes_closed,
        paid_jobs,
        plan_generation,
        resolve_chibi_base,
    )

    char_list = [c.strip() for c in chars.split(",") if c.strip()] or _default_chars()
    emo_list = [e.strip() for e in emotions.split(",") if e.strip()] or list(CHIBI_EMOTIONS)
    jobs = plan_generation(char_list, emo_list, blink=blink, force=force)
    paid = paid_jobs(jobs)

    rprint(f"[cyan]対象[/] {len(char_list)}キャラ × {len(emo_list)}感情"
           f"{'＋瞬き' if blink else ''} / model={model or DEFAULT_CHIBI_MODEL}")
    rprint(f"[cyan]計画[/] {len(jobs)} 件（うち**課金 {len(paid)} 枚**）")

    # プリフライト: ベース素材とAPIキーを**課金前に**確認する
    missing_base = []
    for c in char_list:
        try:
            resolve_chibi_base(c)
        except FileNotFoundError:
            missing_base.append(c)
    if missing_base:
        rprint(f"[red]ベース画像が無いキャラ[/]: {', '.join(missing_base)}")
    if paid:
        try:
            from wwedit.publish.thumbnail import _api_key

            _api_key()
            rprint("[green]APIキー OK[/]")
        except RuntimeError as e:
            rprint(f"[red]{e}[/]")
            missing_base.append("(APIキー)")

    if dry_run:
        for c, e, w in jobs:
            rprint(f"  {c}/{e or '-'}: {w}")
        rprint("[yellow]--dry-run のため生成しません[/]")
        return
    if missing_base:
        raise typer.Exit(1)
    if not jobs:
        rprint("[green]アセットは揃っている[/]")
        return
    if limit and len(paid) > limit:
        rprint(f"[yellow]--limit {limit} により課金 {len(paid)} → {limit} 枚に絞ります[/]")
    if not yes and not typer.confirm(
            f"生成しますか?（課金 {min(len(paid), limit) if limit else len(paid)} 枚）"):
        raise typer.Exit(1)

    if force and backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for c in char_list:
            src = char_dir(c)
            if src.exists():
                dst = assets_root() / "_backup" / f"{c}-{stamp}"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dst)
                rprint(f"  退避: {src} → {dst}")

    spent, failed = 0, []
    done_pairs: set[tuple[str, str]] = set()
    for c, e, w in jobs:
        if limit and spent >= limit:
            rprint(f"[yellow]--limit {limit} に達したので中断（再実行で続きから）[/]")
            break
        try:
            if w == "base":
                ensure_base(c, force=force)
                rprint(f"  base: {c} OK")
            elif w == "eyes":
                generate_eyes_closed(c, model=model or DEFAULT_CHIBI_MODEL, force=force)
                detect_regions(c, force=True)     # 目が取れたので頭部矩形を精度の高い方へ
                spent += 1
                rprint(f"  eyes: {c} OK")
            elif (c, e) not in done_pairs:
                gen_cmd(char=c, emotion=e, model=model, yes=True, force=force,
                        redraw_closed=False)
                done_pairs.add((c, e))
                spent += 2 if e != "normal" else 1
        except Exception as ex:                    # 1枚の失敗でバッチを止めない
            failed.append((c, e, w, str(ex)))
            rprint(f"  [red]失敗[/] {c}/{e or '-'} {w}: {ex}")

    # 目パッチ（無課金）はここでまとめて作る
    for c in char_list:
        try:
            _build_patches_for(c, emo_list)
        except Exception as ex:
            failed.append((c, "", "patch", str(ex)))
            rprint(f"  [red]目パッチ失敗[/] {c}: {ex}")

    if failed:
        rprint(f"[red]{len(failed)} 件失敗[/]（再実行で続きから）:")
        for c, e, w, msg in failed:
            rprint(f"  {c}/{e or '-'} {w}: {msg}")
        raise typer.Exit(1)
    rprint("[green]chibi ensure-all 完了[/]")


def _build_patches_for(char: str, emotions: list[str]) -> None:
    """瞬きの目パッチ（``m*_e1.png``）を作る（無課金）。素材が無ければ何もしない。"""
    from wwedit.chibi.assets import BLINKABLE_EMOTIONS, build_eye_patches, char_dir

    if not (char_dir(char) / "eyes_closed.png").exists():
        return
    made = 0
    for e in emotions:
        if e in BLINKABLE_EMOTIONS:
            made += len(build_eye_patches(char, e))
    if made:
        rprint(f"  瞬きパッチ: {char} {made} 枚（無課金）")


@chibi_app.command(name="rebuild")
def rebuild_cmd(
    char: str = typer.Argument(..., help="キャラID"),
    emotion: str = typer.Option("", help="感情（空=全部）"),
) -> None:
    """生成済みの ``*_raw.png`` から後処理だけをやり直す（**課金なし**）。

    背景抜き → 寸法合わせ → 頭部アンカー登録 → 口合成 → 目パッチ、の再構成。
    プロンプトを直しても**画像そのものを引き直さずに済む範囲**はここで直せる。
    """
    import shutil

    from wwedit.chibi.assets import (
        CHIBI_EMOTIONS,
        _match_size,
        char_dir,
        compose_mouth_only,
        detect_regions,
        mouth_pair_paths,
        register_closed,
        remove_bg,
    )

    targets = [emotion] if emotion else list(CHIBI_EMOTIONS)
    d = char_dir(char)
    base = d / "base_rgba.png"
    if not base.exists():
        rprint(f"[red]ベースが無い[/]: {base}")
        raise typer.Exit(1)
    for e in targets:
        closed_p, open_p = mouth_pair_paths(char, e)
        c_raw = closed_p.with_name("mouth_closed_raw.png")
        if c_raw.exists():
            remove_bg(c_raw, closed_p)
            _match_size(closed_p, base)
            r = register_closed(char, e, closed_p)
            note = f"(登録 dx={r[0]:+.2f} dy={r[1]:+.2f} s={r[2]:.4f})" if r else ""
            rprint(f"  {e}/closed 再構成 {note}")
        elif e == "normal" and closed_p.exists():
            # normal の口閉じはベースのコピー（生成raw が無い）。ベースを作り直したり
            # 背景抜きを直したときに**ここだけ古いまま取り残される**ので追随させる。
            shutil.copyfile(base, closed_p)
            rprint("  normal/closed ベースから再コピー")
        gen = open_p.with_name("mouth_open_gen.png")
        if closed_p.exists() and gen.exists():
            compose_mouth_only(closed_p, gen, open_p)
            rprint(f"  {e}/open 口合成をやり直し")
    detect_regions(char, force=True)
    _build_patches_for(char, targets)
    rprint("[green]rebuild 完了（課金なし）[/]")


@chibi_app.command(name="fix-mouth")
def fix_mouth_cmd(
    char: str = typer.Argument(..., help="キャラID"),
    emotion: str = typer.Argument(..., help="対象の感情（normal 以外）"),
) -> None:
    """口閉じ画像の**口だけ**を normal の口で差し替える（**無課金**・撮り直しの代わり）。

    「口は閉じたまま」がモデルに守られなかったとき用（`chibi sheet` の mouth_open が
    1.6 を超えたら候補）。口以外は1画素も触らない。口開き側も自動で作り直す。
    """
    from PIL import Image

    from wwedit.chibi import geometry as G
    from wwedit.chibi.assets import fix_closed_mouth, mouth_pair_paths

    try:
        p, frac = fix_closed_mouth(char, emotion)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    reg = G.load_regions(p.parent.parent)
    ref = Image.open(mouth_pair_paths(char, "normal")[0]).convert("RGBA")
    img = Image.open(p).convert("RGBA")
    after = G.check_mouth_closed(img, ref, tuple(reg["mouth_box"]))
    seam = G.check_patch_seam(img, [tuple(reg["mouth_box"])])
    rprint(f"[green]口を差し替えた[/]: {p}（マスク面積 {frac:.3f}）")
    rprint(f"  mouth_open={after:.2f}（1.6以下でOK） / 継ぎ目 {seam:.2f}（1.0前後で目立たない）")


@chibi_app.command(name="restore")
def restore_cmd(
    char: str = typer.Argument(..., help="キャラID"),
    at: str = typer.Option("", help="退避のタイムスタンプ（空=最新）"),
) -> None:
    """``_backup`` から退避したアセットを戻す（``--force`` の取り消し）。"""
    import shutil

    from wwedit.chibi.assets import assets_root, char_dir

    root = assets_root() / "_backup"
    cands = sorted(root.glob(f"{char}-*")) if root.exists() else []
    if at:
        cands = [p for p in cands if p.name.endswith(at)]
    if not cands:
        rprint(f"[red]退避が無い[/]: {root}/{char}-*")
        raise typer.Exit(1)
    src = cands[-1]
    dst = char_dir(char)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    rprint(f"[green]復元[/]: {src} → {dst}")


@chibi_app.command(name="sheet")
def sheet_cmd(
    char: str = typer.Argument(..., help="キャラID"),
    out: Path = typer.Option(None, help="出力PNG（既定 <assets>/<char>/_sheet.png）"),
    thumb: int = typer.Option(320, help="1コマの表示px（実際の合成サイズに合わせる）"),
) -> None:
    """全感情×口×目のコンタクトシートを作り、幾何の検査値を焼き込む（**課金なし**）。

    110枚規模の検品を、動画をレンダせずに一目で行うための道具。
    """
    from PIL import Image, ImageDraw

    from wwedit.chibi import geometry as G
    from wwedit.chibi.assets import (
        CHIBI_EMOTIONS,
        char_dir,
        detect_regions,
        mouth_pair_paths,
        sprite_path,
    )

    d = char_dir(char)
    ref_p = mouth_pair_paths(char, "normal")[0]
    if not ref_p.exists():
        rprint(f"[red]normal が無い[/]: {ref_p}")
        raise typer.Exit(1)
    reg = detect_regions(char)
    head = tuple(reg["head_box"])
    mouth = tuple(reg["mouth_box"])
    ref = Image.open(ref_p).convert("RGBA")

    cols, rows = [], []
    for e in CHIBI_EMOTIONS:
        closed_p, _open_p = mouth_pair_paths(char, e)
        if not closed_p.exists():
            continue
        img = Image.open(closed_p).convert("RGBA")
        dx, dy, ds = G.check_head_anchor(img, ref, head)
        pd = G.pose_delta(img, ref, head)
        mc = G.check_mouth_closed(img, ref, mouth)
        row = []
        for m in range(2):
            for eye in (0, 1):
                p = sprite_path(char, e, m, eye)
                if not p.exists():
                    continue
                t = Image.open(p).convert("RGBA").resize((thumb, thumb), Image.LANCZOS)
                bg = Image.new("RGBA", (thumb, thumb), (240, 240, 244, 255))
                bg.alpha_composite(t)
                ImageDraw.Draw(bg).text((4, 4), f"m{m}e{eye}", fill=(70, 74, 80, 255))
                row.append(bg)
        if row:
            rows.append((e, row, (dx, dy, ds, pd, mc)))
        cols.append(len(row))

    if not rows:
        rprint("[red]スプライトが無い[/]")
        raise typer.Exit(1)
    w = max(cols) * thumb + 300
    sheet = Image.new("RGBA", (w, thumb * len(rows)), (255, 255, 255, 255))
    dr = ImageDraw.Draw(sheet)
    for i, (e, row, (dx, dy, ds, pd, mc)) in enumerate(rows):
        for j, t in enumerate(row):
            sheet.paste(t, (j * thumb, i * thumb))
        x = max(cols) * thumb + 8
        dr.text((x, i * thumb + 8), e, fill=(20, 20, 24, 255))
        dr.text((x, i * thumb + 28),
                f"anchor dx={dx:+.1f} dy={dy:+.1f} ds={ds:+.4f}", fill=(60, 60, 66, 255))
        dr.text((x, i * thumb + 46), f"pose_iou={pd:.3f}",
                fill=((200, 40, 40, 255) if pd > 0.97 else (30, 120, 40, 255)))
        dr.text((x, i * thumb + 64), f"mouth_open={mc:.2f}",
                fill=((200, 40, 40, 255) if mc > 1.6 else (30, 120, 40, 255)))
    out = out or (d / "_sheet.png")
    sheet.convert("RGB").save(out)
    rprint(f"[green]コンタクトシート[/]: {out}")
    rprint("  pose_iou>0.97=ポーズが変わっていない / mouth_open>1.6=口が閉じていない")


@chibi_app.command(name="fx-preview")
def fx_preview_cmd(
    out: Path = typer.Option(Path("chibi_fx_preview.mp4"), help="出力mp4"),
    char: str = typer.Option("", help="重ねるキャラ（空=エフェクトのみ）"),
    size: int = typer.Option(320, help="ちびキャラの表示高さpx（実合成に合わせる）"),
) -> None:
    """[E] 感情エフェクトを実寸で確認する（**課金なし**・compose へ配線する前の目視用）。"""
    import subprocess
    import tempfile

    from PIL import Image

    from wwedit.chibi import fx
    from wwedit.chibi.assets import mouth_pair_paths

    w, h = 1920, 1080
    mx, my = 24, 24
    fxp = int(size * 0.35)
    tmp = Path(tempfile.mkdtemp())
    sprite = None
    if char:
        p = mouth_pair_paths(char, "normal")[0]
        if p.exists():
            sprite = Image.open(p).convert("RGBA").resize((size, size), Image.LANCZOS)

    n = 0
    for emo in fx.FX_EMOTIONS:
        frames = fx.fx_frames(emo, fxp)
        for i in list(range(fx.FX_FRAMES)) + [None] * 6:
            frame = Image.new("RGBA", (w, h), (24, 26, 30, 255))
            # 右半分を明るくして、明暗どちらの映像でも読めるか同時に見る
            frame.paste(Image.new("RGBA", (w // 2, h), (232, 234, 238, 255)), (w // 2, 0))
            if sprite is not None:
                frame.alpha_composite(sprite, (mx, h - size - my))
                frame.alpha_composite(sprite, (w - size - mx, h - size - my))
            if i is not None:
                f = Image.open(frames[i]).convert("RGBA")
                frame.alpha_composite(f, (mx + int(size * 0.62), h - my - int(size * 1.02)))
                frame.alpha_composite(
                    f, (w - mx - int(size * 0.62) - fxp, h - my - int(size * 1.02)))
            frame.convert("RGB").save(tmp / f"{n:05d}.png")
            n += 1

    from wwedit.compose.ffmpeg_compose import video_encode_args

    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-framerate", str(fx.FX_FPS),
         "-i", str(tmp / "%05d.png"), *video_encode_args(None, 20, "medium"),
         "-r", "30", str(out)], check=True)
    rprint(f"[green]エフェクトのプレビュー[/]: {out}（{n} フレーム・課金なし）")


@chibi_app.command(name="motion-preview")
def motion_preview_cmd(
    char: str = typer.Argument(..., help="キャラID"),
    emotion: str = typer.Option(
        "all", help="見る感情。`all` は全感情を順に切り替える（本番と同じ挙動）"),
    seconds: float = typer.Option(0.0, help="尺(秒)。0=感情ごとに4秒"),
    fps: int = typer.Option(30, help="フレームレート（合成チェーンと同じ30が既定）"),
    height: int = typer.Option(320, help="表示高さ(px)。合成時の実寸に合わせる"),
    out: Path = typer.Option(None, help="出力mp4（既定 assets/chibi/<char>/_motion.mp4）"),
) -> None:
    """口パク＋瞬き＋感情切替の**動き**を mp4 で確認する（**無課金**）。

    合成と同じ `MOUTH_WAVE` / `blink_times` をそのまま使うので、本番の見え方と一致する
    （本番も GIF ではなく ffconcat→ffmpeg なので、出力形式も揃える）。
    明暗どちらの映像でも読めるよう、背景を左右で暗/明に分けて描く。
    """
    import shutil as _shutil
    import subprocess
    import tempfile

    from PIL import Image, ImageDraw

    from wwedit.chibi import timeline as T
    from wwedit.chibi.assets import CHIBI_EMOTIONS, blink_emotions, sprite_path

    dst = out or (Path("assets/chibi") / char / "_motion.mp4")
    if emotion == "all":
        emos = [e for e in CHIBI_EMOTIONS if sprite_path(char, e, 0).exists()]
    else:
        emos = [emotion]
    if not emos:
        rprint(f"[red]アセットが無い[/]: {char}")
        raise typer.Exit(1)
    hold = 4.0
    total = seconds or hold * len(emos)
    hold = total / len(emos)
    sprites: dict[tuple[str, int, int], Image.Image] = {}

    def _sprite(emo: str, m: int, e: int) -> Image.Image:
        if (emo, m, e) not in sprites:
            p = sprite_path(char, emo, m, eye=e or None)
            if not p.exists():
                p = sprite_path(char, emo, m)
            im = Image.open(p).convert("RGBA")
            sprites[(emo, m, e)] = im.resize(
                (round(im.width * height / im.height), height), Image.LANCZOS)
        return sprites[(emo, m, e)]

    # 発話スパン：喋る→黙る→喋る。無言時に口が閉じることも確認できる
    spans = [(t0, t0 + hold * 0.55) for t0 in (i * hold for i in range(len(emos)))]
    spans += [(t0 + hold * 0.68, t0 + hold * 0.95) for t0 in
              (i * hold for i in range(len(emos)))]
    blinkable = blink_emotions(char)
    blinks = T.blink_times(total, speaker=char, fps=fps)
    # h264 は偶数寸法が要る
    w = (max(_sprite(e, 0, 0).width for e in emos) + 120) // 2 * 2
    h = (height + 40) // 2 * 2
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="wwedit-motion-"))
    try:
        n = 0
        for i in range(int(total * fps)):
            t = i / fps
            emo = emos[min(int(t / hold), len(emos) - 1)]
            m = 0
            for s, e in spans:
                if s <= t < e:
                    m = T.MOUTH_WAVE[int((t - s) / T.MOUTH_STEP_S) % len(T.MOUTH_WAVE)]
                    break
            eye = 1 if (emo in blinkable
                        and any(s <= t < e for s, e in blinks)) else 0
            f = Image.new("RGBA", (w, h), (24, 26, 30, 255))
            f.paste(Image.new("RGBA", (w // 2, h), (232, 234, 238, 255)), (w // 2, 0))
            ImageDraw.Draw(f).text((8, 6), emo, fill=(255, 255, 255, 255))
            sp = _sprite(emo, m, eye)
            f.alpha_composite(sp, ((w - sp.width) // 2, h - height - 10))
            f.convert("RGB").save(tmp / f"{n:05d}.png")
            n += 1
        from wwedit.compose.ffmpeg_compose import video_encode_args

        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-framerate", str(fps),
             "-i", str(tmp / "%05d.png"), *video_encode_args(None, 20, "medium"),
             "-r", str(fps), str(dst)], check=True)
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)
    has_eye = any(sprite_path(char, e, 0, eye=1).exists() for e in emos)
    note = "" if has_eye else "（瞬きアセット未生成のため口パクのみ）"
    rprint(f"[green]動きプレビュー[/]: {dst}"
           f"（{n}フレーム / {'→'.join(emos)}・課金なし）{note}")


@chibi_app.command(name="preview")
def preview_cmd(
    edl_path: Path = typer.Argument(..., help="対象 EDL（voice-cast/感情/アセット 済み）"),
    seconds: float = typer.Option(30.0, help="出力の先頭からこの秒数だけレンダする"),
    out: Path = typer.Option(None, help="出力mp4（既定 data/<date>/chibi_preview.mp4）"),
) -> None:
    """ちびキャラ付きの短いプレビューをレンダする（口パク同期・サイズ感の目視用）。"""
    from wwedit.compose.ffmpeg_compose import compose_kept
    from wwedit.edl.schema import TimeRange

    edl = load_edl(edl_path)
    keep = edl.kept_ranges()
    if not keep:
        raise typer.BadParameter("keep区間が無い")
    # 出力の先頭 seconds 分に相当する keep 区間を切り出す
    sel: list[TimeRange] = []
    acc = 0.0
    for r in keep:
        if acc + r.duration >= seconds:
            sel.append(TimeRange(start=r.start, end=r.start + (seconds - acc)))
            break
        sel.append(r)
        acc += r.duration
    out_path = out or (edl_path.parent / "chibi_preview.mp4")
    rprint(f"[dim]プレビュー合成中[/]: 先頭{seconds:.0f}s → {out_path}")
    result = compose_kept(
        edl, out_path, ranges=sel, framed=bool(edl.framing), subtitles=bool(edl.subtitles),
        chibi=True, data_dir=edl_path.parent,
    )
    rprint(f"[green]プレビュー完了[/]: {result}")


@emotions_app.command(name="audio")
def emotions_audio(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe 済み）"),
    device: str = typer.Option("cuda", help="推論デバイス"),
    refresh: bool = typer.Option(False, help="既存の結果を捨てて測り直す"),
) -> None:
    """[E] **元の収録マイク音声**を emotion2vec+ に掛けて、有声区間ごとの感情を測る。

    重いのは一度だけ。結果は ``chibi_audio_emotion.json`` に残り、閾値の調整は後処理で行う。
    """
    from wwedit.chibi.audio_emotion import AUDIO_EMOTION_JSON, analyze_spans, audio_spans

    edl = load_edl(edl_path)
    out = edl_path.parent / AUDIO_EMOTION_JSON
    if out.exists() and not refresh:
        rprint(f"[yellow]既にある[/]: {out}（測り直すなら --refresh）")
        return
    items = audio_spans(edl)
    if not items:
        rprint("[red]判定できる発話区間がありません[/]")
        raise typer.Exit(1)
    rprint(f"[dim]感情を測定中[/]: {len(items)}区間（元の収録音声・合成音ではない）...")
    analyze_spans(items, out, device=device)
    rprint(f"[green]音声の感情[/]: {out}")
    rprint("[dim]次: chibi emotions prepare（この結果を手がかりに付ける）[/]")


@emotions_app.command(name="prepare")
def emotions_prepare(
    edl_path: Path = typer.Argument(..., help="対象 EDL（transcribe/cut 済み）"),
) -> None:
    """発話TSVを書き出す（→ chibi-emotion-assigner スキルで感情を割当てる）。

    ``chibi_audio_emotion.json`` があれば **audio 列**として手がかりに入る。
    """
    from wwedit.chibi.audio_emotion import AUDIO_EMOTION_JSON
    from wwedit.chibi.emotion import EMOTION_TSV, write_emotion_input

    edl = load_edl(edl_path)
    tsv = edl_path.parent / EMOTION_TSV
    audio = edl_path.parent / AUDIO_EMOTION_JSON
    n = write_emotion_input(edl, tsv, audio_json=audio if audio.exists() else None)
    hint = "音声判定つき" if audio.exists() else "テキストのみ（先に chibi emotions audio）"
    rprint(f"[green]感情入力[/]: {tsv}（{n}区間・{hint}）")
    rprint("[dim]次: chibi-emotion-assigner スキル → chibi emotions apply[/]")


@emotions_app.command(name="apply")
def emotions_apply(
    edl_path: Path = typer.Argument(..., help="対象 EDL"),
    decisions: Path = typer.Option(
        None, help="決定JSON（既定 data/<date>/chibi_emotion_decisions.json）"
    ),
) -> None:
    """決定JSONを EDL へ適用する（key形式なら ``emotion_cues``＝時刻付きキュー）。"""
    from wwedit.chibi.emotion import EMOTION_DECISIONS, apply_emotion_decisions

    edl = load_edl(edl_path)
    dec = decisions or (edl_path.parent / EMOTION_DECISIONS)
    if not dec.exists():
        raise typer.BadParameter(f"{dec} が無い（先に chibi-emotion-assigner スキル）")
    n = apply_emotion_decisions(edl, dec)
    save_edl(edl, edl_path)
    rprint(f"[green]感情適用[/]: {n}件（未割当=normal）"
           f"{f' / キュー{len(edl.emotion_cues)}件' if edl.emotion_cues else ''}")
