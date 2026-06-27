from wwedit.framing.codec_motion import (
    FramePacket,
    _adaptive_threshold,
    _motion_signal,
)


def test_motion_signal_interpolates_keyframes():
    # キーフレーム(大サイズ)は近傍の非キーフレーム値で補間され、動き信号から除外される
    packets = [
        FramePacket(time=0.0, size=40000, is_key=True),  # I (intraコスト大)
        FramePacket(time=0.04, size=20, is_key=False),
        FramePacket(time=0.08, size=30, is_key=False),
        FramePacket(time=0.12, size=39000, is_key=True),  # 周期I
        FramePacket(time=0.16, size=25, is_key=False),
    ]
    sig = _motion_signal(packets)
    # 先頭I → 右隣20で補間, 周期I(idx3) → (30+25)/2
    assert sig[0] == 20.0
    assert sig[3] == (30.0 + 25.0) / 2.0
    # 非キーフレームはそのまま
    assert sig[1] == 20.0 and sig[2] == 30.0


def test_adaptive_threshold_floor():
    # ほぼ静止(小さい値ばかり) → floor が効く
    sig = [20.0, 30.0, 25.0, 22.0]
    thr = _adaptive_threshold(sig, k=6.0, floor_bytes=800.0)
    assert thr == 800.0


def test_adaptive_threshold_above_floor():
    # 大きなばらつき → 中央値+k*MAD が floor を超える
    sig = [20.0, 30.0, 25.0, 5000.0, 6000.0, 7000.0]
    thr = _adaptive_threshold(sig, k=6.0, floor_bytes=100.0)
    assert thr > 100.0
