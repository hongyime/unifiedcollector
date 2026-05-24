"""
Tests for P2.6: Fix VideoFrameExtractor._extract_keyframes() fallback

Bug: container.seek(0) was called with a byte offset, but PyAV's seek()
requires a timestamp in stream time_base units. This silently fails on many
video formats.

Fix: Re-open the container from the buffer (video_buffer.seek(0) +
self.av.open(video_buffer)) instead of seeking the existing container.

Validates: Requirements 2.15 (fix checking) and 3.12 (preservation checking)
"""
import io
import pytest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

def _make_fake_frame():
    """Return a minimal mock that looks like an av.VideoFrame."""
    frame = MagicMock()
    return frame


def _make_fake_stream(average_rate=30, duration=100, time_base=0.001):
    stream = MagicMock()
    stream.average_rate = average_rate
    stream.duration = duration
    stream.time_base = time_base
    stream.thread_type = 'AUTO'
    return stream


def _make_fake_packet(is_keyframe: bool, frames=None):
    packet = MagicMock()
    packet.is_keyframe = is_keyframe
    packet.decode.return_value = frames or [_make_fake_frame()]
    return packet


def _make_fake_container(packets=None, stream=None):
    """Build a fake av.Container."""
    container = MagicMock()
    if stream is None:
        stream = _make_fake_stream()
    container.streams.video = [stream]
    if packets is not None:
        container.demux.return_value = iter(packets)
    return container


def _make_extractor(av_module=None):
    """Instantiate VideoFrameExtractor with a mocked av module."""
    with patch('services.collector.video_extractor.VideoFrameExtractor.__init__', lambda self: None):
        from services.collector.video_extractor import VideoFrameExtractor
        extractor = VideoFrameExtractor.__new__(VideoFrameExtractor)
        extractor.av = av_module or MagicMock()
        extractor.max_frames = 30
        extractor.default_fps = 1.0
        extractor.round_video_fps = 2.0
        return extractor


# ---------------------------------------------------------------------------
# Fix-checking tests (bug condition: fallback path triggered)
# ---------------------------------------------------------------------------

class TestExtractKeyframesFallback:
    """
    Validates: Requirements 2.15
    WHEN _extract_keyframes() finds fewer than 5 keyframes
    THEN it SHALL re-open the container from the buffer (not seek the existing one).
    """

    def test_fallback_reopens_container_not_seeks(self):
        """
        The fallback must call video_buffer.seek(0) + av.open(), NOT container.seek().
        **Validates: Requirements 2.15**
        """
        av_mock = MagicMock()
        extractor = _make_extractor(av_mock)

        # Primary container returns 0 keyframe packets → triggers fallback
        primary_stream = _make_fake_stream()
        primary_container = _make_fake_container(packets=[], stream=primary_stream)

        # Fallback container returns some frames
        fallback_stream = _make_fake_stream()
        fallback_frames = [_make_fake_frame() for _ in range(3)]
        fallback_container = MagicMock()
        fallback_container.streams.video = [fallback_stream]
        fallback_container.decode.return_value = iter(fallback_frames)

        av_mock.open.return_value = fallback_container

        video_buffer = MagicMock(spec=io.BytesIO)

        frames = extractor._extract_keyframes(primary_container, primary_stream, video_buffer)

        # Buffer must be rewound before re-opening
        video_buffer.seek.assert_called_with(0)
        # av.open must be called with the buffer (re-open)
        av_mock.open.assert_called_once_with(video_buffer)
        # The old container must NOT have had seek() called on it
        primary_container.seek.assert_not_called()
        # Fallback container must be closed after use
        fallback_container.close.assert_called_once()

    def test_fallback_returns_frames_from_reopened_container(self):
        """
        Frames returned in the fallback path come from the re-opened container.
        **Validates: Requirements 2.15**
        """
        av_mock = MagicMock()
        extractor = _make_extractor(av_mock)

        primary_stream = _make_fake_stream()
        primary_container = _make_fake_container(packets=[], stream=primary_stream)

        fallback_stream = _make_fake_stream(average_rate=2)  # 2fps → interval=4 at 0.5fps
        expected_frames = [_make_fake_frame() for _ in range(5)]
        fallback_container = MagicMock()
        fallback_container.streams.video = [fallback_stream]
        fallback_container.decode.return_value = iter(expected_frames)

        av_mock.open.return_value = fallback_container

        video_buffer = MagicMock(spec=io.BytesIO)
        frames = extractor._extract_keyframes(primary_container, primary_stream, video_buffer)

        assert len(frames) > 0

    def test_fallback_container_closed_even_on_error(self):
        """
        The re-opened fallback container is closed even if _extract_at_fps raises.
        **Validates: Requirements 2.15**
        """
        av_mock = MagicMock()
        extractor = _make_extractor(av_mock)

        primary_stream = _make_fake_stream()
        primary_container = _make_fake_container(packets=[], stream=primary_stream)

        fallback_container = MagicMock()
        fallback_container.streams.video = [_make_fake_stream()]
        fallback_container.decode.side_effect = RuntimeError("decode error")
        av_mock.open.return_value = fallback_container

        video_buffer = MagicMock(spec=io.BytesIO)

        with pytest.raises(RuntimeError):
            extractor._extract_keyframes(primary_container, primary_stream, video_buffer)

        fallback_container.close.assert_called_once()

    def test_fallback_without_buffer_does_not_crash(self):
        """
        When video_buffer is None, the fallback path should not crash.
        **Validates: Requirements 2.15**
        """
        av_mock = MagicMock()
        extractor = _make_extractor(av_mock)

        primary_stream = _make_fake_stream()
        primary_container = _make_fake_container(packets=[], stream=primary_stream)
        primary_container.decode.return_value = iter([])

        # Should not raise even without a buffer
        frames = extractor._extract_keyframes(primary_container, primary_stream, video_buffer=None)
        assert isinstance(frames, list)


# ---------------------------------------------------------------------------
# Preservation-checking tests (non-bug condition: enough keyframes found)
# ---------------------------------------------------------------------------

class TestExtractKeyframesNormalPath:
    """
    Validates: Requirements 3.12
    WHEN enough keyframes are found (>= 5)
    THEN the fallback path is NOT triggered and no re-open occurs.
    """

    def test_no_fallback_when_enough_keyframes(self):
        """
        With >= 5 keyframes, av.open() must NOT be called again.
        **Validates: Requirements 3.12**
        """
        av_mock = MagicMock()
        extractor = _make_extractor(av_mock)

        keyframe_packets = [
            _make_fake_packet(is_keyframe=True, frames=[_make_fake_frame()])
            for _ in range(6)
        ]
        primary_stream = _make_fake_stream()
        primary_container = _make_fake_container(packets=keyframe_packets, stream=primary_stream)

        video_buffer = MagicMock(spec=io.BytesIO)
        frames = extractor._extract_keyframes(primary_container, primary_stream, video_buffer)

        av_mock.open.assert_not_called()
        primary_container.seek.assert_not_called()
        assert len(frames) == 6

    def test_non_keyframe_packets_skipped(self):
        """
        Non-keyframe packets are ignored; only keyframe packets contribute frames.
        **Validates: Requirements 3.12**
        """
        av_mock = MagicMock()
        extractor = _make_extractor(av_mock)

        packets = (
            [_make_fake_packet(is_keyframe=False) for _ in range(10)]
            + [_make_fake_packet(is_keyframe=True, frames=[_make_fake_frame()]) for _ in range(6)]
        )
        primary_stream = _make_fake_stream()
        primary_container = _make_fake_container(packets=packets, stream=primary_stream)

        video_buffer = MagicMock(spec=io.BytesIO)
        frames = extractor._extract_keyframes(primary_container, primary_stream, video_buffer)

        assert len(frames) == 6
        av_mock.open.assert_not_called()

    def test_max_frames_cap_respected(self):
        """
        Extraction stops at max_frames even if more keyframes are available.
        **Validates: Requirements 3.12**
        """
        av_mock = MagicMock()
        extractor = _make_extractor(av_mock)
        extractor.max_frames = 5

        keyframe_packets = [
            _make_fake_packet(is_keyframe=True, frames=[_make_fake_frame()])
            for _ in range(20)
        ]
        primary_stream = _make_fake_stream()
        primary_container = _make_fake_container(packets=keyframe_packets, stream=primary_stream)

        video_buffer = MagicMock(spec=io.BytesIO)
        frames = extractor._extract_keyframes(primary_container, primary_stream, video_buffer)

        assert len(frames) <= extractor.max_frames
