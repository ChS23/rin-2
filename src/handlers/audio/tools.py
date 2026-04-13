"""
Инструменты Audio Agent — обёртки над audio_engine.
Агент вызывает их в loop: render → analyze → fix → repeat.
"""

import os
import json
import asyncio

import numpy as np
import structlog
from agents import function_tool

from src.handlers.audio.engine import (
    SR, render_faust, render_midi, render_numpy,
    apply_effects, mix_layers, apply_fade, master,
    analyze_audio, save_wav, wav_to_ogg,
)

logger = structlog.get_logger("audio.tools")

AUDIO_DIR = "/app/data/scripts"


@function_tool
async def synth_faust(name: str, code: str, duration: float = 30.0) -> str:
    """Синтезировать звук через FAUST DSP код.
    name — имя слоя (для идентификации).
    code — FAUST DSP код. Обязательно: import("stdfaust.lib"); process выводит 2 канала.
    duration — длительность в секундах.

    FAUST шпаргалка:
    - Осцилляторы: os.osc(freq), os.sawtooth(freq), os.triangle(freq), os.square(freq)
    - Шум: no.noise
    - Фильтры: fi.lowpass(order, cutoff), fi.highpass(order, cutoff), fi.bandpass(order, freq, q)
    - Реверб: dm.zita_light (стерео вход → стерео выход)
    - Делей: de.delay(maxdelay, delay)
    - LFO: os.osc(rate) для медленной модуляции
    - Стерео: mono <: _, _ или mono <: dm.zita_light
    - Цепочка: signal : effect1 : effect2
    - Параллельно: sig1, sig2"""
    result = await render_faust(code, duration)
    if isinstance(result, str):
        return f"FAUST [{name}]: {result}"

    path = f"/tmp/audio_layer_{name}.wav"
    await save_wav(result, path)
    peak = float(np.max(np.abs(result)))
    if peak < 0.001:
        return f"FAUST [{name}]: рендер тихий (peak={peak:.5f}), проверь код — возможно сигнал не доходит до process"
    return f"FAUST [{name}]: {duration:.0f}с, peak={peak:.3f}. Файл: {path}"


@function_tool
async def synth_midi(name: str, tracks: str, bpm: int = 60, duration: float = 30.0) -> str:
    """Синтезировать через MIDI + FluidSynth (пианино, струнные, хор и т.д.).
    name — имя слоя.
    tracks — JSON: [{"program": 0, "notes": [{"pitch": 60, "vel": 80, "beat": 0, "dur": 4}]}]
    bpm — темп.
    duration — длительность.

    Программы GM: 0=пианино, 40=скрипка, 48=струнные, 51=хор, 73=флейта, 88=пад, 92=атмосфера."""
    result = await render_midi(tracks, bpm, duration)
    if isinstance(result, str):
        return result

    path = f"/tmp/audio_layer_{name}.wav"
    await save_wav(result, path)
    peak = float(np.max(np.abs(result)))
    return f"MIDI [{name}]: {duration:.0f}с, bpm={bpm}, peak={peak:.3f}. Файл: {path}"


@function_tool
async def synth_numpy(name: str, code: str, duration: float = 30.0) -> str:
    """Синтезировать через Python/numpy код (шум, тоны, эффекты).
    name — имя слоя.
    code — Python код. Доступно: np, SR (44100), duration. Создай переменную 'audio'.

    Примеры:
    - Шум: audio = np.random.randn(int(SR*duration)).astype(np.float32) * 0.1
    - Тон: t = np.linspace(0,duration,int(SR*duration)); audio = np.sin(2*np.pi*440*t).astype(np.float32)
    - Щелчки: audio = np.zeros(int(SR*duration)); [audio.__setitem__(slice(p,p+50), np.random.randn(50)*0.3) for p in np.random.randint(0,len(audio)-50,20)]"""
    result = await render_numpy(code, duration)
    if isinstance(result, str):
        return result

    path = f"/tmp/audio_layer_{name}.wav"
    await save_wav(result, path)
    peak = float(np.max(np.abs(result)))
    return f"numpy [{name}]: {duration:.0f}с, peak={peak:.3f}. Файл: {path}"


@function_tool
async def mix_audio(output_name: str, layers: str, duration: float = 30.0,
                    fade_in: float = 2.0, fade_out: float = 3.0) -> str:
    """Смикшировать слои в один файл.
    output_name — имя выходного файла (без расширения).
    layers — JSON: [{"file": "/tmp/audio_layer_drone.wav", "volume": 0.7}, ...]
    duration — максимальная длительность.
    fade_in/fade_out — плавные входы/выходы в секундах."""
    try:
        layers_data = json.loads(layers) if isinstance(layers, str) else layers
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"

    from pedalboard.io import AudioFile as AF
    rendered = []
    for ld in layers_data:
        fpath = ld.get("file", "")
        vol = ld.get("volume", 0.5)
        if not os.path.exists(fpath):
            continue
        with AF(fpath) as f:
            audio = f.read(f.frames).astype(np.float32)
        rendered.append((audio, vol))

    if not rendered:
        return "Нет слоёв для микширования"

    max_samples = int(SR * duration)
    mixed = mix_layers(rendered, max_samples)
    mixed = apply_fade(mixed, fade_in, fade_out)

    path = f"/tmp/audio_mix_{output_name}.wav"
    await save_wav(mixed, path)
    peak = float(np.max(np.abs(mixed)))
    return f"Mix [{output_name}]: {len(rendered)} слоёв, {duration:.0f}с, peak={peak:.3f}. Файл: {path}"


@function_tool
async def add_effects(input_path: str, effects: str) -> str:
    """Применить эффекты к аудиофайлу.
    input_path — путь к WAV файлу.
    effects — JSON: [{"type": "reverb", "room_size": 0.8, "wet": 0.4}, {"type": "lowpass", "cutoff": 2000}]

    Доступные эффекты: reverb, delay, chorus, compressor, lowpass, highpass, gain, distortion, limiter."""
    if not os.path.exists(input_path):
        return f"Файл не найден: {input_path}"

    try:
        effects_data = json.loads(effects) if isinstance(effects, str) else effects
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"

    from pedalboard.io import AudioFile as AF
    with AF(input_path) as f:
        audio = f.read(f.frames).astype(np.float32)

    audio = await apply_effects(audio, effects_data)

    # Перезаписываем
    with AF(input_path, 'w', SR, audio.shape[0]) as f:
        f.write(audio)

    peak = float(np.max(np.abs(audio)))
    return f"Effects applied to {input_path}: {[e.get('type') for e in effects_data]}, peak={peak:.3f}"


@function_tool
async def master_and_export(input_path: str, output_name: str, gain_db: float = 6.0) -> str:
    """Финальный мастеринг и экспорт в OGG.
    input_path — путь к WAV (обычно результат mix_audio).
    output_name — имя файла (без расширения), сохранится в папку скриптов.
    gain_db — усиление (6-10 для нормальной громкости)."""
    if not os.path.exists(input_path):
        return f"Файл не найден: {input_path}"

    from pedalboard.io import AudioFile as AF
    with AF(input_path) as f:
        audio = f.read(f.frames).astype(np.float32)

    audio = await master(audio, gain_db)

    ogg_path = os.path.join(AUDIO_DIR, f"{output_name}.ogg")
    wav_tmp = input_path
    await save_wav(audio, wav_tmp)
    result = await wav_to_ogg(wav_tmp, ogg_path)

    return f"Exported: {ogg_path} | {result}"


@function_tool
async def check_audio(path: str) -> str:
    """Проанализировать аудиофайл — громкость, частоты, проблемы.
    Используй после рендера чтобы проверить качество."""
    if not os.path.exists(path):
        return f"Файл не найден: {path}"
    return await analyze_audio(path)


audio_tools = [
    synth_faust, synth_midi, synth_numpy,
    mix_audio, add_effects,
    master_and_export, check_audio,
]
