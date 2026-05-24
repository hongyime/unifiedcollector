import { useCallback, useEffect, useState } from "react";

export default function usePlayback(playback, speed) {
  const [isPlaying, setIsPlaying] = useState(true);
  const [currentTime, setCurrentTime] = useState(null);
  const togglePlaying = useCallback(() => setIsPlaying((value) => !value), []);
  const reset = useCallback(() => setCurrentTime(playback?.day_start_unix ?? null), [playback?.day_start_unix]);
  const setPlaybackTime = useCallback((value) => setCurrentTime(value), []);

  useEffect(() => {
    setCurrentTime(playback?.day_start_unix ?? null);
  }, [playback?.day_start_unix]);

  useEffect(() => {
    if (!isPlaying || currentTime == null || !playback) {
      return undefined;
    }

    let frameId = 0;
    let previous = performance.now();

    const tick = (now) => {
      const deltaSeconds = Math.max(0, (now - previous) / 1000);
      previous = now;
      setCurrentTime((value) => {
        if (value == null || value < playback.day_start_unix || value > playback.day_end_unix) {
          return playback.day_start_unix;
        }
        const next = value + deltaSeconds * speed;
        if (next > playback.day_end_unix) {
          return playback.day_start_unix;
        }
        return next;
      });
      frameId = window.requestAnimationFrame(tick);
    };

    frameId = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frameId);
  }, [isPlaying, currentTime, playback, speed]);

  return {
    currentTime,
    isPlaying,
    togglePlaying,
    reset,
    setCurrentTime: setPlaybackTime
  };
}
