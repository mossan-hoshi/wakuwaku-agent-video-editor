"""framing.loading_screen の pure 関数テスト（描画/ffmpeg は重いので別）。"""

from __future__ import annotations

from wwedit.framing.loading_screen import DOT_STATES, dot_for_frame, layout_boxes


def test_dot_cycle_advances_each_period():
    fps, period = 10, 0.5  # 5フレームごとに1段
    assert dot_for_frame(0, fps, period) == ""
    assert dot_for_frame(4, fps, period) == ""
    assert dot_for_frame(5, fps, period) == "."
    assert dot_for_frame(10, fps, period) == ".."
    assert dot_for_frame(15, fps, period) == "..."
    assert dot_for_frame(20, fps, period) == ""  # 1周してループ


def test_dot_states_are_four():
    assert DOT_STATES == ["", ".", "..", "..."]


def test_layout_centers_logo_and_text_below():
    (lx, ly, lw, lh), (cx, ty) = layout_boxes(1920, 1080, 2000, 2000, logo_frac=0.34)
    assert lw == int(1920 * 0.34)
    assert lh == lw  # 正方ロゴ
    assert lx == (1920 - lw) // 2  # 水平中央
    assert cx == 960  # テキスト水平中央
    assert ty > ly + lh  # テキストはロゴの下


def test_layout_handles_zero_logo_width():
    (lx, ly, lw, lh), _ = layout_boxes(1920, 1080, 0, 0)
    assert lw > 0 and lh == lw  # ゼロ割回避
