"""Распознавание голосовых сообщений МАРСИ-Job (GLM ASR).

Пайплайн: OGG/OPUS (Telegram voice) → WAV 16кГц mono (ffmpeg) → нарезка
по тишина на сегменты ≤30 с (лимит GLM ASR) → транскрипция сегментов
с параметром prompt (склейка контекста + смещение определения языка
на русский) → итоговый текст для LLM-парсера резюме.

Параметры API: https://docs.bigmodel.cn — POST /audio/transcriptions,
model=glm-asr-2512, форматы .wav/.mp3, ≤25 МБ, ≤30 с на файл.
"""
import json
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ASR_URL = "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"
ASR_MODEL = "glm-asr-2512"
MAX_SEGMENT_SEC = 28.0          # запас до лимита 30 с
SEED_PROMPT = (
    "Русский язык. Кандидат рассказывает о себе: опыт, навыки, "
    "желаемая работа и условия."
)


class AsrError(Exception):
    pass


# ---------------------------------------------------------------------------
# Подготовка аудио (ffmpeg)
# ---------------------------------------------------------------------------


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ogg_to_wav(ogg_bytes: bytes, workdir: Optional[str] = None) -> bytes:
    """OGG/OPUS → WAV 16 кГц mono PCM16."""
    if not ffmpeg_available():
        raise AsrError("ffmpeg не установлен на сервере (нужен для обработки голосовых)")
    with tempfile.TemporaryDirectory(dir=workdir) as td:
        src = Path(td) / "in.ogg"
        dst = Path(td) / "out.wav"
        src.write_bytes(ogg_bytes)
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
             str(dst)],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0 or not dst.exists():
            raise AsrError(f"ffmpeg не смог конвертировать: {proc.stderr.decode(errors='ignore')[:200]}")
        return dst.read_bytes()


def audio_duration_sec(wav_path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(wav_path)],
        capture_output=True, timeout=30,
    )
    try:
        return float(proc.stdout.decode().strip())
    except ValueError:
        return 0.0


def detect_silences(wav_path: Path, noise: str = "-35dB", min_silence: float = 0.35):
    """Точки середины пауз — кандидаты на разрез."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(wav_path), "-af",
         f"silencedetect=noise={noise}:d={min_silence}", "-f", "null", "-"],
        capture_output=True, timeout=120,
    )
    text = proc.stderr.decode(errors="ignore")
    starts, ends = [], []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("silence_start:"):
            starts.append(float(line.split(":")[1].split()[0]))
        elif line.startswith("silence_end:"):
            ends.append(float(line.split(":")[1].split()[0]))
    return [(s, e) for s, e in zip(starts, ends)]


def split_points(duration: float, silences: List[Any], max_seg: float = MAX_SEGMENT_SEC) -> List[float]:
    """Границы сегментов ≤ max_seg; режем по последней паузе, влезающей в лимит."""
    if duration <= max_seg:
        return []
    mids = sorted(((s + e) / 2.0) for s, e in silences)
    points: List[float] = []
    last = 0.0
    i = 0
    while duration - last > max_seg:
        limit = last + max_seg
        best = None
        while i < len(mids) and mids[i] <= limit:
            if mids[i] > last + 1.0:  # слишком близко к предыдущему разрезу — пропускаем
                best = mids[i]
            i += 1
        cut = best if best is not None else limit
        points.append(round(cut, 2))
        last = cut
        if len(points) > 60:
            break
    return points


def split_wav(wav_bytes: bytes, workdir: Optional[str] = None) -> List[bytes]:
    """Нарезает WAV на сегменты ≤28 c (по паузам). Один вход — один выход минимум."""
    if not ffmpeg_available():
        raise AsrError("ffmpeg не установлен")
    with tempfile.TemporaryDirectory(dir=workdir) as td:
        src = Path(td) / "full.wav"
        src.write_bytes(wav_bytes)
        duration = audio_duration_sec(src)
        points = split_points(duration, detect_silences(src))
        if not points:
            return [wav_bytes]
        segments = []
        bounds = [0.0] + points + [duration]
        for i in range(len(bounds) - 1):
            seg = Path(td) / f"seg{i:02d}.wav"
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(src), "-ss", f"{bounds[i]:.2f}", "-to", f"{bounds[i+1]:.2f}",
                 "-c", "copy", str(seg)],
                capture_output=True, timeout=60,
            )
            if proc.returncode == 0 and seg.exists() and seg.stat().st_size > 0:
                segments.append(seg.read_bytes())
        return segments or [wav_bytes]


# ---------------------------------------------------------------------------
# HTTP: multipart без внешних зависимостей
# ---------------------------------------------------------------------------


def multipart_encode(fields: Dict[str, str], file_field: str, filename: str,
                     file_bytes: bytes) -> Tuple[bytes, str]:
    boundary = "----marsijobboundary7f3a9c2d"
    lines: List[bytes] = []
    for name, value in fields.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        lines.append(str(value).encode("utf-8") + b"\r\n")
    lines.append(f"--{boundary}\r\n".encode())
    lines.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    lines.append(b"Content-Type: audio/wav\r\n\r\n")
    lines.append(file_bytes + b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())
    body = b"".join(lines)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def transcribe_segment(wav_bytes: bytes, api_key: str,
                       prompt: str = SEED_PROMPT,
                       base_url: str = ASR_URL,
                       timeout: int = 180) -> str:
    """Один сегмент → текст. prompt продолжает контекст (длинное аудио)."""
    fields = {"model": ASR_MODEL, "stream": "false"}
    if prompt:
        fields["prompt"] = prompt
    body, content_type = multipart_encode(fields, "file", "audio.wav", wav_bytes)
    req = urllib.request.Request(
        base_url, data=body, method="POST",
        headers={"Content-Type": content_type, "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise AsrError(f"ASR-запрос не удался: {e}") from e
    if "error" in data:
        raise AsrError(f"ASR ошибка: {data['error'].get('message', data['error'])}")
    return (data.get("text") or "").strip()


def transcribe_voice(ogg_bytes: bytes, api_key: str,
                     workdir: Optional[str] = None,
                     max_segments: int = 12) -> str:
    """Полный пайплайн: OGG → WAV → сегменты → текст.

    max_segments ≈ 12 × 28 c ≈ 5.5 минуты голосового.
    """
    wav = ogg_to_wav(ogg_bytes, workdir=workdir)
    segments = split_wav(wav, workdir=workdir)[:max_segments]
    texts: List[str] = []
    for seg in segments:
        prompt = SEED_PROMPT if not texts else (SEED_PROMPT + " " + " ".join(texts[-2:]))
        text = transcribe_segment(seg, api_key, prompt=prompt)
        if text:
            texts.append(text)
    return " ".join(texts).strip()
