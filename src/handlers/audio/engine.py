"""
Гибридный движок генерации музыки.

Пайплайн:
  MIDI → FluidSynth → WAV (мелодии, инструменты)
  FAUST → DawDreamer → WAV (синтез, дроны, текстуры)
  numpy → WAV (шум, простые тоны)
  → DawDreamer mix graph → pedalboard мастеринг → OGG

Audio Agent использует эти функции в loop:
  plan → generate → render → analyze → fix → master
"""

import asyncio
import json
import os
import tempfile
import traceback
from pathlib import Path

import numpy as np
import structlog
from pedalboard import (
    Reverb, Chorus, Delay, Compressor, Gain,
    LowpassFilter, HighpassFilter, Distortion, Limiter,
    Bitcrush, Resample, PeakFilter, LowShelfFilter, HighShelfFilter,
    NoiseGate, Phaser, Clipping, PitchShift,
)
from pedalboard.io import AudioFile

logger = structlog.get_logger("audio_engine")

SR = 44100
SOUNDFONT = os.getenv("SOUNDFONT", "/usr/share/sounds/sf2/FluidR3_GM.sf2")
SCRIPTS_DIR = Path(os.getenv("SCRIPTS_DIR", "/app/data/scripts"))

# DawDreamer может быть недоступен (Python 3.13)
try:
    import dawdreamer as daw
    HAS_DAWDREAMER = True
except ImportError:
    HAS_DAWDREAMER = False


# ═══════════════════════════════════════════════════════════
#                    РЕНДЕРЕРЫ СЛОЁВ
# ═══════════════════════════════════════════════════════════

def _render_faust_sync(code: str, duration: float) -> np.ndarray | str:
    """Синхронный рендер FAUST (CPU-bound)."""
    if not HAS_DAWDREAMER:
        return "DawDreamer не установлен"
    try:
        engine = daw.RenderEngine(SR, 512)
        proc = engine.make_faust_processor("synth")
        proc.set_dsp_string(code)
        proc.compile()

        graph = [(proc, [])]
        engine.load_graph(graph)
        engine.render(duration)
        audio = engine.get_audio().astype(np.float32)

        if np.any(np.isnan(audio)) or np.any(np.isinf(audio)):
            return "FAUST error: аудио содержит NaN/Inf — нестабильный DSP код, перепиши"

        return audio
    except Exception as e:
        return f"FAUST error: {e}"


async def render_faust(code: str, duration: float = 30.0) -> np.ndarray | str:
    """Скомпилировать FAUST DSP код и отрендерить аудио (non-blocking)."""
    result = await asyncio.to_thread(_render_faust_sync, code, duration)
    if isinstance(result, np.ndarray):
        await logger.ainfo("FAUST rendered", duration=duration, shape=result.shape,
                           peak=float(np.max(np.abs(result))))
    return result


def _build_midi_sync(tracks_json, bpm, duration):
    """Синхронная сборка MIDI файла (CPU-bound)."""
    import mido

    try:
        tracks_data = json.loads(tracks_json) if isinstance(tracks_json, str) else tracks_json
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e}"

    ticks = 480
    tempo = mido.bpm2tempo(max(10, min(300, bpm)))
    mid = mido.MidiFile(ticks_per_beat=ticks)
    meta = mido.MidiTrack()
    mid.tracks.append(meta)
    meta.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))

    max_beat = duration * bpm / 60.0

    for ch_idx, td in enumerate(tracks_data[:15]):
        track = mido.MidiTrack()
        mid.tracks.append(track)
        ch = ch_idx % 16
        prog = max(0, min(127, td.get("program", 0)))
        track.append(mido.Message("program_change", channel=ch, program=prog, time=0))

        events = []
        for n in td.get("notes", []):
            beat = n.get("beat", 0)
            if beat >= max_beat:
                continue
            pitch = max(0, min(127, n.get("pitch", 60)))
            vel = max(0, min(127, n.get("vel", 80)))
            dur = min(max(0.05, n.get("dur", 1.0)), max_beat - beat)
            events.append((int(beat * ticks), "on", ch, pitch, vel))
            events.append((int((beat + dur) * ticks), "off", ch, pitch))

        events.sort(key=lambda e: e[0])
        prev = 0
        for ev in events:
            delta, prev = ev[0] - prev, ev[0]
            if ev[1] == "on":
                track.append(mido.Message("note_on", channel=ev[2], note=ev[3], velocity=ev[4], time=delta))
            else:
                track.append(mido.Message("note_off", channel=ev[2], note=ev[3], velocity=0, time=delta))

    with tempfile.NamedTemporaryFile(suffix=".mid", delete=False) as f:
        mid_path = f.name
        mid.save(mid_path)

    return mid_path, tracks_data


def _read_wav_sync(wav_path: str) -> np.ndarray:
    """Синхронное чтение WAV."""
    with AudioFile(wav_path) as f:
        return f.read(f.frames).astype(np.float32)


async def render_midi(tracks_json: str, bpm: int = 60, duration: float = 30.0) -> np.ndarray | str:
    """Отрендерить MIDI через FluidSynth (non-blocking)."""
    mid_path, result = await asyncio.to_thread(_build_midi_sync, tracks_json, bpm, duration)
    if mid_path is None:
        return result  # ошибка

    tracks_data = result
    wav_path = mid_path.replace(".mid", ".wav")

    try:
        proc = await asyncio.create_subprocess_exec(
            "fluidsynth", "-ni", "-g", "1.5", SOUNDFONT, mid_path,
            "-F", wav_path, "-r", str(SR),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

        if not os.path.exists(wav_path):
            return f"FluidSynth error: {stderr.decode()[:300]}"

        audio = await asyncio.to_thread(_read_wav_sync, wav_path)

        await logger.ainfo("MIDI rendered", bpm=bpm, tracks=len(tracks_data),
                           shape=audio.shape, peak=float(np.max(np.abs(audio))))
        return audio

    finally:
        for p in [mid_path, wav_path]:
            try:
                os.unlink(p)
            except OSError:
                pass


def _render_numpy_sync(code: str, duration: float) -> np.ndarray | str:
    namespace = {"np": np, "numpy": np, "SR": SR, "sr": SR, "duration": duration}
    try:
        exec(code, namespace)
        if "audio" in namespace:
            audio = np.array(namespace["audio"], dtype=np.float32)
            if audio.ndim == 1:
                audio = audio.reshape(1, -1)
            return audio
        return "Переменная 'audio' не создана"
    except Exception as e:
        return f"numpy error: {traceback.format_exc()[-500:]}"


async def render_numpy(code: str, duration: float = 30.0) -> np.ndarray | str:
    """Выполнить Python/numpy код для генерации аудио (non-blocking)."""
    return await asyncio.to_thread(_render_numpy_sync, code, duration)


# ══════════════════════════════════════════════════════��════
#                    ЭФФЕКТЫ
# ═══════════════════════════════════════════════���═══════════

EFFECTS_MAP = {
    "reverb": lambda p: Reverb(
        room_size=p.get("room_size", 0.8),
        damping=p.get("damping", 0.5),
        wet_level=p.get("wet", 0.4),
    ),
    "delay": lambda p: Delay(
        delay_seconds=p.get("seconds", 0.4),
        feedback=p.get("feedback", 0.3),
        mix=p.get("mix", 0.25),
    ),
    "chorus": lambda p: Chorus(
        rate_hz=p.get("rate", 1.0),
        depth=p.get("depth", 0.25),
        mix=p.get("mix", 0.3),
    ),
    "compressor": lambda p: Compressor(
        threshold_db=p.get("threshold", -16),
        ratio=p.get("ratio", 4.0),
    ),
    "lowpass": lambda p: LowpassFilter(
        cutoff_frequency_hz=p.get("cutoff", 2000),
    ),
    "highpass": lambda p: HighpassFilter(
        cutoff_frequency_hz=p.get("cutoff", 200),
    ),
    "gain": lambda p: Gain(gain_db=p.get("db", 0)),
    "distortion": lambda p: Distortion(drive_db=p.get("drive", 10)),
    "limiter": lambda p: Limiter(threshold_db=p.get("threshold", -1)),
    "bitcrush": lambda p: Bitcrush(bit_depth=p.get("bit_depth", 8)),
    "resample": lambda p: Resample(target_sample_rate=p.get("target_sample_rate", 16000)),
    "peak_eq": lambda p: PeakFilter(
        cutoff_frequency_hz=p.get("cutoff", 1000),
        gain_db=p.get("gain_db", 0),
        q=p.get("q", 1.0),
    ),
    "low_shelf": lambda p: LowShelfFilter(
        cutoff_frequency_hz=p.get("cutoff", 200),
        gain_db=p.get("gain_db", 0),
    ),
    "high_shelf": lambda p: HighShelfFilter(
        cutoff_frequency_hz=p.get("cutoff", 8000),
        gain_db=p.get("gain_db", 0),
    ),
    "noisegate": lambda p: NoiseGate(threshold_db=p.get("threshold", -60)),
    "phaser": lambda p: Phaser(
        rate_hz=p.get("rate", 1.0),
        depth=p.get("depth", 0.5),
        mix=p.get("mix", 0.5),
    ),
    "clipping": lambda p: Clipping(threshold_db=p.get("threshold_db", -6)),
    "pitchshift": lambda p: PitchShift(semitones=p.get("semitones", 0)),
}


def _apply_effects_sync(audio: np.ndarray, effects: list[dict]) -> np.ndarray:
    if audio.ndim == 1:
        audio = audio.reshape(1, -1)
    for fx_def in effects:
        fx_type = fx_def.get("type", "")
        params = {k: v for k, v in fx_def.items() if k != "type"}
        if fx_type in EFFECTS_MAP:
            effect = EFFECTS_MAP[fx_type](params)
            audio = effect(audio, SR)
    return audio


async def apply_effects(audio: np.ndarray, effects: list[dict]) -> np.ndarray:
    """Применить цепочку эффектов к аудио (non-blocking)."""
    return await asyncio.to_thread(_apply_effects_sync, audio, effects)


# ═══════════════════════════════════════════════════════════
#                    МИКШИРОВАНИЕ
# ═══════════════════════════════════════════════════════════

def mix_layers(layers: list[tuple[np.ndarray, float]], max_samples: int) -> np.ndarray:
    """Смикшировать слои с volume балансом.

    layers: [(audio_array, volume), ...]
    """
    output = np.zeros((2, max_samples), dtype=np.float32)

    for audio, vol in layers:
        if audio.ndim == 1:
            audio = np.stack([audio, audio])  # моно → стерео
        if audio.shape[0] == 1:
            audio = np.concatenate([audio, audio], axis=0)  # моно → стерео
        if audio.shape[1] > max_samples:
            audio = audio[:, :max_samples]

        output[:, :audio.shape[1]] += audio[:2] * vol

    return output


def apply_fade(audio: np.ndarray, fade_in_sec: float = 2.0, fade_out_sec: float = 3.0) -> np.ndarray:
    """Fade in/out."""
    if audio.ndim == 2:
        length = audio.shape[1]
    else:
        length = len(audio)
        audio = audio.reshape(1, -1)

    fi = min(int(SR * fade_in_sec), length // 2)
    fo = min(int(SR * fade_out_sec), length // 2)

    if fi > 0:
        audio[:, :fi] *= np.linspace(0, 1, fi, dtype=np.float32)
    if fo > 0:
        audio[:, -fo:] *= np.linspace(1, 0, fo, dtype=np.float32)

    return audio


def _master_sync(audio: np.ndarray, gain_db: float) -> np.ndarray:
    if audio.ndim == 1:
        audio = audio.reshape(1, -1)
    audio = Compressor(threshold_db=-18, ratio=3)(audio, SR)
    audio = Gain(gain_db=gain_db)(audio, SR)
    audio = Limiter(threshold_db=-1)(audio, SR)
    return np.clip(audio, -0.99, 0.99)


async def master(audio: np.ndarray, gain_db: float = 6.0) -> np.ndarray:
    """Финальный мастеринг (non-blocking)."""
    return await asyncio.to_thread(_master_sync, audio, gain_db)


# ══════════════════════════════════════��════════════════════
#                    АНАЛИЗ
# ═══════════════════════════════════════════════════════════

def _analyze_audio_sync(path: str) -> str:
    try:
        with AudioFile(path) as f:
            audio = f.read(f.frames)
            sr = f.samplerate
            channels = f.num_channels

        duration = audio.shape[1] / sr if audio.ndim == 2 else len(audio) / sr
        peak = float(np.max(np.abs(audio)))
        rms = float(np.sqrt(np.mean(audio ** 2)))
        rms_db = 20 * np.log10(rms) if rms > 0 else -100

        mono = audio[0] if audio.ndim == 2 else audio
        n = min(len(mono), sr)
        fft = np.abs(np.fft.rfft(mono[:n]))
        freqs = np.fft.rfftfreq(n, 1 / sr)
        top_indices = np.argsort(fft)[-5:][::-1]
        dominant = [(int(freqs[i]), float(fft[i])) for i in top_indices if freqs[i] > 20]

        size_kb = os.path.getsize(path) // 1024
        status = "Tiho!" if peak < 0.1 else "OK" if peak < 0.95 else "Clipping!"

        return (
            f"Duration: {duration:.1f}s | Channels: {channels} | Size: {size_kb}KB\n"
            f"Peak: {peak:.3f} | RMS: {rms:.4f} ({rms_db:.1f} dB)\n"
            f"Dominant frequencies: {', '.join(f'{f}Hz' for f, _ in dominant[:3])}\n"
            f"{status}"
        )
    except Exception as e:
        return f"Analysis error: {e}"


async def analyze_audio(path: str) -> str:
    """Анализ аудиофайла (non-blocking)."""
    return await asyncio.to_thread(_analyze_audio_sync, path)


# ═══════════════════════════════════════════════════════════
#                    СОХРАНЕНИЕ
# ═══════════════════════════════════���═══════════════════════

def _save_wav_sync(audio: np.ndarray, path: str) -> str:
    if audio.ndim == 1:
        audio = audio.reshape(1, -1)
    with AudioFile(path, 'w', SR, audio.shape[0]) as f:
        f.write(audio.astype(np.float32))
    return f"Saved: {path} ({os.path.getsize(path) // 1024}KB)"


async def save_wav(audio: np.ndarray, path: str) -> str:
    """Сохранить аудио в WAV (non-blocking)."""
    return await asyncio.to_thread(_save_wav_sync, audio, path)


async def wav_to_ogg(wav_path: str, ogg_path: str) -> str:
    """WAV → OGG Opus через opusenc."""
    proc = await asyncio.create_subprocess_exec(
        "opusenc", wav_path, ogg_path,
        "--bitrate", "128",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=60)

    if os.path.exists(ogg_path):
        size_kb = os.path.getsize(ogg_path) // 1024
        return f"Encoded: {ogg_path} ({size_kb}KB)"
    return "opusenc failed"
