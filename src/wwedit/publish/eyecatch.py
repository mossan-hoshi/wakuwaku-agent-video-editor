"""[H] チャプター/セクション アイキャッチ（2秒・**インク有機デザイン**＋ジングル）。

「のべつべ！」ロゴの世界観に合わせ、都会風グラデーションではなく **白地に多色インクが
有機的に滲み広がる** アニメで作る:
  - ロゴ配色（赤/青/紫/オレンジ/インディゴ）のインク滴を **メタボール＋ドメインワープ** で
    生成（境界がインクのように不定形）→ seed で配置/色/形が毎回変わる、
  - 開始直後に **インクがブワッと展開**（＝場面転換の勢い）、
  - 展開後に **タイトルがフェードイン**（白フチ濃紺で白地でも読める）。
インク背景は numpy で生成し rawvideo で ffmpeg へ流す（GL非依存）。音声は jingle のランダム区間。
"""

from __future__ import annotations

import random
import subprocess
import tempfile
from pathlib import Path

from wwedit.common.media import ffmpeg_path

LOGO = Path(__file__).resolve().parents[3] / "assets" / "logo" / "nobetube_logo.png"
_MEIRYO = r"C:\Windows\Fonts\meiryob.ttc"

# のべつべ！ロゴから抽出したインク配色（RGB）。鮮やかな多色。
INK_COLORS = [
    (224, 0, 0),     # red
    (0, 96, 160),    # blue
    (96, 32, 128),   # purple
    (224, 96, 0),    # orange
    (32, 32, 128),   # indigo
    (32, 0, 96),     # deep purple
    (192, 0, 0),     # crimson
    (224, 64, 0),    # vermilion
]
_TITLE_FILL = (24, 22, 56)   # 濃紺（白地でも黒すぎず締まる）
_BG = (252, 251, 249)        # ほぼ白（ロゴ地に合わせる）


def _run(cmd: list[str], **kw):
    """ffmpeg/ffprobe 実行（Windows cp932 でも壊れないよう utf-8/replace でデコード）。"""
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def _audio_dur(path: str | Path) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
              "default=nw=1:nk=1", str(path)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _title_card(title: str, out_png: Path, *, w: int = 1920, h: int = 1080) -> Path:
    """中央に**白いプレート**を敷きその上に濃紺タイトル＋ロゴ赤の下線。インク上でも確実に読める。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(_MEIRYO, 100)
    tw = int(draw.textlength(title, font=font))
    cx, cy = w // 2, h // 2
    padx, pady = 64, 46
    half_w = tw // 2 + padx
    px0, py0, px1, py1 = cx - half_w, cy - 64 - pady, cx + half_w, cy + 64 + pady
    rad = 28
    # 影（インクからプレートを浮かせる）→ 白プレート（不透明）→ 細い縁
    draw.rounded_rectangle([px0, py0 + 8, px1, py1 + 8], radius=rad, fill=(20, 18, 40, 70))
    draw.rounded_rectangle([px0, py0, px1, py1], radius=rad, fill=(255, 255, 255, 255))
    draw.rounded_rectangle([px0, py0, px1, py1], radius=rad,
                           outline=(*_TITLE_FILL, 60), width=3)
    # タイトル（濃紺）
    draw.text((cx - tw // 2, cy - 64), title, font=font, fill=(*_TITLE_FILL, 255))
    # アクセント下線（ロゴ赤）
    draw.rounded_rectangle([cx - tw // 2, cy + 70, cx + tw // 2, cy + 80],
                           radius=5, fill=(224, 0, 0, 255))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return out_png


def _value_noise(h: int, w: int, gy: int, gx: int, rng) -> object:
    """**周期的**な格子値ノイズ（smoothstep バイリニア補間・0..1）。

    格子を modulo 参照して全画面で seamless にタイルさせる（後段で np.roll しても継ぎ目が出ない）。
    """
    import numpy as np

    grid = rng.random((gy, gx)).astype("float32")
    ys = np.linspace(0, gy, h, endpoint=False).astype("float32")
    xs = np.linspace(0, gx, w, endpoint=False).astype("float32")
    y0 = np.floor(ys).astype(int) % gy
    x0 = np.floor(xs).astype(int) % gx
    y1 = (y0 + 1) % gy
    x1 = (x0 + 1) % gx
    ty = ys - np.floor(ys)
    tx = xs - np.floor(xs)
    sy = (ty * ty * (3 - 2 * ty))[:, None]
    sx = (tx * tx * (3 - 2 * tx))[None, :]
    g00 = grid[np.ix_(y0, x0)]
    g01 = grid[np.ix_(y0, x1)]
    g10 = grid[np.ix_(y1, x0)]
    g11 = grid[np.ix_(y1, x1)]
    top = g00 * (1 - sx) + g01 * sx
    bot = g10 * (1 - sx) + g11 * sx
    return top * (1 - sy) + bot * sy


def _fbm(h: int, w: int, rng, *, octaves: int = 4, base: int = 3,
        base_x: int | None = None) -> object:
    """fractal brownian motion（複数オクターブ加算・0..1）＝有機的なゆらぎ。

    ``base_x`` を別指定すると x/y で格子密度が変わり **異方的（筆ストローク状の筋）** になる。
    """
    import numpy as np

    bx = base_x if base_x is not None else base
    out = np.zeros((h, w), "float32")
    amp, tot = 1.0, 0.0
    for o in range(octaves):
        k = 2 ** o
        out += amp * _value_noise(h, w, base * k, bx * k, rng)
        tot += amp
        amp *= 0.5
    return out / tot


def _ink_backend():
    """フレームループのバックエンドを選ぶ。CUDA が使えれば torch(GPU)・無ければ numpy(CPU)。

    インクは 1920×1080 のピクセル演算×60フレームで CPU だと重い（~80s）。GPU なら数秒。
    フットプリントは数百MBと小さく安全（重い学習とは別物）。利用不可なら自動で CPU。
    """
    try:
        import torch

        if torch.cuda.is_available():
            return "torch", torch
    except Exception:
        pass
    return "numpy", None


def _render_ink(out_mp4: Path, *, seed: int, duration: float,
                out_w: int, out_h: int, fps: int) -> Path:
    """白地に多色インクが滲み広がるアニメを生成（フル解像度・GPU/CPU→rawvideo→ffmpeg mp4）。"""
    import numpy as np

    backend, torch = _ink_backend()
    use_torch = backend == "torch"

    def dev(a):
        return torch.from_numpy(np.ascontiguousarray(a)).cuda() if use_torch else a

    # ハードステップ（x>0→1.0 / それ以外→0.0）。縁を**一切ボカさない**ための2値化。
    stepf = ((lambda x: (x > 0.0).to(torch.float32)) if use_torch
             else (lambda x: (x > 0.0).astype("float32")))

    rng = np.random.default_rng(seed)
    prng = random.Random(seed)
    gw, gh = out_w, out_h  # **フル解像度で生成**（拡大しない＝ボケない）
    nframes = max(2, int(round(duration * fps)))

    # ドメインワープ場（境界をインクのように不定形にする）＋細部ノイズ（縁のラギッド化）。
    # ワープは静的（蠢きより滲み拡大が主役）。これで座標歪み・距離場を**源ごとに1回だけ**計算でき、
    # フル解像度でもフレームループを軽くできる。
    warp_x = _fbm(gh, gw, rng)
    warp_y = _fbm(gh, gw, rng)
    # 2スケールの細部: 中域=シルエットを崩す / 高域=繊維・飛沫（縁のギザギザ）。
    det_mid = _fbm(gh, gw, rng, octaves=6, base=5)
    det_hi = _fbm(gh, gw, rng, octaves=5, base=14)
    wamp = gw * 0.22  # ワープ強め＝丸くない不定形シルエット
    yy, xx = np.mgrid[0:gh, 0:gw].astype("float32")
    xw = xx + (warp_x - 0.5) * wamp
    yw = yy + (warp_y - 0.5) * wamp

    # インク源（色/咲くタイミング/距離場・縁ゆらぎ）。**単色ベタ**（内部テクスチャ無し）。
    cols = prng.sample(INK_COLORS, k=prng.randint(4, 6))
    sources = []
    for col in cols:
        cx = prng.uniform(0.14, 0.86) * gw
        cy = prng.uniform(0.18, 0.82) * gh
        rmax = prng.uniform(0.34, 0.50) * gw  # 下限を上げ「小さすぎる源」を無くす
        t0 = prng.uniform(0.0, 0.16)          # 咲き始めのばらつき
        d2 = (xw - cx) ** 2 + (yw - cy) ** 2   # 距離場（静的）
        sh = prng.randint(0, gw)
        # 中域でシルエットを大きく歪ませ、高域で縁に繊維・飛沫（しきい付近で島が分離→スプラッシュ）
        detadj = ((np.roll(det_mid, sh, axis=1) - 0.5) * 1.05
                  + (np.roll(det_hi, sh * 2 % gw, axis=1) - 0.5) * 0.55)
        sources.append((rmax, t0, dev(d2.astype("float32")),
                        dev(detadj.astype("float32")), dev(np.array(col, "float32"))))

    ff = ffmpeg_path()
    proc = subprocess.Popen(
        [ff, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{gw}x{gh}", "-r", str(fps), "-i", "-",
         "-t", f"{duration}", "-c:v", "libx264", "-preset", "medium",
         "-crf", "15", "-pix_fmt", "yuv420p", str(out_mp4)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    bg = dev(np.array(_BG, "float32"))
    img = (torch.empty((gh, gw, 3), device="cuda") if use_torch
           else np.empty((gh, gw, 3), "float32"))
    for fi in range(nframes):
        t = fi / max(1, nframes - 1) * duration
        img[:] = bg
        for (rmax, t0, d2, detadj, col) in sources:
            # **サイズ0から**成長し duration 全体で広がり続ける（開始フレームは真っ白）。
            prog = min(1.0, max(0.0, (t - t0) / max(1e-3, duration - t0)))
            r2 = (rmax * prog ** 0.72) ** 2                    # prog=0 → r2=0 → インク無し
            # 乗算形（ゼロ割れ無し）。輪郭は detadj のギザギザで不定形＝2値ボケ無し。
            a3 = stepf(r2 * (0.5 + detadj) - d2)[..., None]
            img *= (1.0 - a3)
            img += a3 * col
        if use_torch:
            frame = img.clamp(0, 255).to(torch.uint8).cpu().numpy()
        else:
            frame = np.clip(img, 0, 255).astype("uint8")
        proc.stdin.write(frame.tobytes())

    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0 or not out_mp4.exists():
        raise RuntimeError("インク背景の生成に失敗（ffmpeg rawvideo パイプ）")
    return out_mp4


def generate_eyecatch(
    title: str,
    out_path: str | Path,
    *,
    seed: int = 0,
    jingle: str | Path | None = None,
    duration: float = 2.0,
    jingle_offset: float | None = None,
    logo_path: str | Path = LOGO,
    out_w: int = 1920,
    out_h: int = 1080,
    fps: int = 30,
) -> Path:
    """2秒アイキャッチ mp4 生成（**白地インク有機**＋タイトル＋ロゴ＋ジングル・seed で変化）。"""
    rng = random.Random(seed)
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp())

    ink_mp4 = _render_ink(work / "ink.mp4", seed=seed, duration=duration,
                          out_w=out_w, out_h=out_h, fps=fps)
    title_png = _title_card(title, work / "title.png", w=out_w, h=out_h)

    logo_path = Path(logo_path)
    has_logo = logo_path.exists()
    has_aud = bool(jingle)
    off = 0.0
    if has_aud:
        jdur = _audio_dur(jingle)
        off = jingle_offset if jingle_offset is not None else (
            round(rng.uniform(0, max(0.0, jdur - duration - 0.1)), 2) if jdur > duration else 0.0)

    # 入力: 0=インク背景 1=title (2=logo) (3=jingle)
    t_title = round(duration * 0.32, 3)   # インク展開後にタイトル登場
    fin = max(1, int(fps * 0.12))
    fout = max(1, int(fps * (duration - 0.26)))
    fil = [
        f"[1:v]fade=in:st={t_title}:d=0.3:alpha=1[ti]",
        "[0:v][ti]overlay=0:0[vt]",
    ]
    last = "vt"
    if has_logo:
        fil.append("[2:v]scale=170:170[lg]")
        fil.append(f"[{last}][lg]overlay=W-w-44:H-h-40[vl]")
        last = "vl"
    # 端は白へフェード（インク世界観・黒落ちにしない）
    fil.append(f"[{last}]fade=in:0:{fin}:color=white,"
               f"fade=out:{fout}:{fin}:color=white[vout]")
    amap = None
    if has_aud:
        aidx = 3 if has_logo else 2
        fil.append(f"[{aidx}:a]afade=in:st=0:d=0.2,afade=out:st={duration - 0.3}:d=0.3[aout]")
        amap = "[aout]"

    # タイトルは alpha フェードするため -loop で全尺フレーム化（静止画1枚だと fade が効かない）
    cmd = [ffmpeg_path(), "-y", "-i", str(ink_mp4),
           "-loop", "1", "-framerate", str(fps), "-t", f"{duration}", "-i", str(title_png)]
    if has_logo:
        cmd += ["-i", str(logo_path)]
    if has_aud:
        cmd += ["-ss", f"{off}", "-t", f"{duration}", "-i", str(Path(jingle).resolve())]
    cmd += ["-filter_complex", ";".join(fil), "-map", "[vout]"]
    if amap:
        cmd += ["-map", amap]
    cmd += ["-t", f"{duration}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p"]
    if amap:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(out_path)]

    proc = _run(cmd)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-18:])
        raise RuntimeError(f"アイキャッチ生成失敗:\n{tail}")
    return out_path
