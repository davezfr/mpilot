from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .srt import Cue, with_replacement_text


INTERNAL_CHUNK_SIZE = 100
INTERNAL_MAX_CONCURRENT_CHUNKS = 8
INTERNAL_CHUNK_START_STAGGER_MIN_SECONDS = 0.5
INTERNAL_CHUNK_START_STAGGER_MAX_SECONDS = 1.0
TIMESTAMP_RE = re.compile(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->")


@dataclass(frozen=True)
class TranslationOptions:
    source_language: str
    target_language: str
    backend: str = "codex-cli"
    model: str = "gpt-5.4-mini"
    max_retries: int = 3
    temperature: float = 0
    openai_base_url: str = "http://127.0.0.1:11434/v1"
    openai_api_key: str = "ollama"
    openai_timeout: float = 300
    codex_timeout: float = 1800
    work_dir: Optional[Path] = None
    keep_work_dir: bool = False
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None


@dataclass(frozen=True)
class Chunk:
    index: int
    cues: List[Cue]


@dataclass(frozen=True)
class TranslationRun:
    cues: List[Cue]
    summary: Dict[str, Any]


@dataclass(frozen=True)
class TranslatedChunk:
    index: int
    texts: List[str]
    summary: Dict[str, Any]


def chunk_cues(cues: List[Cue], chunk_size: int = INTERNAL_CHUNK_SIZE) -> List[Chunk]:
    return [Chunk(index + 1, cues[offset : offset + chunk_size]) for index, offset in enumerate(range(0, len(cues), chunk_size))]


def translation_worker_count(chunks: List[Chunk]) -> int:
    if not chunks:
        return 0
    return min(INTERNAL_MAX_CONCURRENT_CHUNKS, len(chunks))


def _stagger_codex_chunk_start(options: TranslationOptions, submitted_chunk_count: int, total_chunks: int) -> None:
    if options.backend != "codex-cli":
        return
    if submitted_chunk_count <= 0 or total_chunks <= 1:
        return
    delay = random.uniform(INTERNAL_CHUNK_START_STAGGER_MIN_SECONDS, INTERNAL_CHUNK_START_STAGGER_MAX_SECONDS)
    time.sleep(delay)


def build_translation_prompt(options: TranslationOptions, chunk: Chunk, retry_note: Optional[str] = None) -> str:
    items = [{"number": cue.number, "text": cue.text} for cue in chunk.cues]
    retry = ""
    if retry_note:
        retry = "\nPrevious attempt failed validation:\n%s\nFix that exact issue in this retry.\n" % retry_note
    return """Translate subtitle cue text from {source} to {target}.

Rules:
- Return JSON only.
- Return exactly one object per input cue.
- Preserve each cue number exactly.
- Translate text only. Do not return timestamps or SRT blocks.
- Personal names, brands, organizations, acronyms, and code identifiers stay in the source form unless an explicit glossary says otherwise.
- Generic speaker labels may be translated naturally.
- If a source cue is a sentence fragment, the translation may also be a fragment.
{retry}
Input JSON:
{payload}

Expected response shape:
{{"translations":[{{"number":"1","translation":"..."}}]}}
""".format(
        source=options.source_language,
        target=options.target_language,
        retry=retry,
        payload=json.dumps({"cues": items}, ensure_ascii=False, indent=2),
    )


def fake_response(chunk: Chunk) -> Dict[str, Any]:
    return {"translations": [{"number": cue.number, "translation": "假译：%s" % cue.text} for cue in chunk.cues]}


def validate_chunk_response(chunk: Chunk, response: Dict[str, Any]) -> List[str]:
    translations = response.get("translations")
    if not isinstance(translations, list):
        raise ValueError("chunk %s: missing translations array" % chunk.index)
    if len(translations) != len(chunk.cues):
        raise ValueError("chunk %s: translation count mismatch source=%s target=%s" % (chunk.index, len(chunk.cues), len(translations)))

    validated = []
    for offset, (cue, item) in enumerate(zip(chunk.cues, translations), start=1):
        if not isinstance(item, dict):
            raise ValueError("chunk %s: translation item %s is not an object" % (chunk.index, offset))
        number = str(item.get("number", "")).strip()
        if number != cue.number:
            raise ValueError("chunk %s: cue number mismatch at item %s: %s != %s" % (chunk.index, offset, cue.number, number))
        translation = str(item.get("translation", item.get("text", ""))).strip()
        if not translation:
            raise ValueError("chunk %s: empty translation for cue %s" % (chunk.index, cue.number))
        if TIMESTAMP_RE.search(translation):
            raise ValueError("chunk %s: translation for cue %s contains a timestamp" % (chunk.index, cue.number))
        validated.append(translation)
    return validated


def write_translation_schema(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["translations"],
                "properties": {
                    "translations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["number", "translation"],
                            "properties": {
                                "number": {"type": "string"},
                                "translation": {"type": "string"},
                            },
                        },
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def openai_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return normalized + "/chat/completions"


def extract_chat_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenAI-compatible response missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("OpenAI-compatible response missing choices[0].message")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part.get("text") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
        if parts:
            return "\n".join(parts)
    raise ValueError("OpenAI-compatible response missing text content")


def run_openai_compatible(options: TranslationOptions, chunk: Chunk, prompt: str, schema_path: Path) -> Dict[str, Any]:
    body = {
        "model": options.model,
        "messages": [
            {
                "role": "system",
                "content": "You are a subtitle translation API worker. Return JSON only and follow the provided schema exactly.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": options.temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "subtitle_chunk_translation",
                "schema": json.loads(schema_path.read_text(encoding="utf-8")),
                "strict": True,
            },
        },
    }
    request = urllib.request.Request(
        openai_endpoint(options.openai_base_url),
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer %s" % options.openai_api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=options.openai_timeout) as response:
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        error_text = error.read().decode("utf-8", errors="replace")
        raise RuntimeError("OpenAI-compatible endpoint failed with HTTP %s: %s" % (error.code, error_text[-2000:]))
    return json.loads(extract_chat_content(json.loads(response_text)))


def run_codex_cli(options: TranslationOptions, chunk: Chunk, prompt: str, schema_path: Path, chunk_dir: Path) -> Dict[str, Any]:
    prompt_path = chunk_dir / "prompt.txt"
    output_path = chunk_dir / "output.json"
    log_path = chunk_dir / "codex.log"
    prompt_path.write_text(prompt, encoding="utf-8")

    codex_home = chunk_dir / "codex-home"
    if codex_home.exists():
        shutil.rmtree(codex_home)
    codex_home.mkdir(parents=True)
    auth = Path.home() / ".codex" / "auth.json"
    if auth.exists():
        shutil.copy2(auth, codex_home / "auth.json")

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    command = [
        "codex",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--cd",
        str(chunk_dir),
        "-m",
        options.model,
        "--json",
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
        "-",
    ]
    try:
        with prompt_path.open("rb") as stdin, log_path.open("wb") as log:
            proc = subprocess.run(
                command,
                stdin=stdin,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=options.codex_timeout,
            )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("codex-cli backend timed out after %s seconds" % options.codex_timeout) from error
    finally:
        shutil.rmtree(codex_home, ignore_errors=True)
    if proc.returncode != 0:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        raise RuntimeError("codex-cli backend failed with exit %s: %s" % (proc.returncode, log_text[-2000:]))
    return json.loads(output_path.read_text(encoding="utf-8"))


def run_backend(options: TranslationOptions, chunk: Chunk, prompt: str, schema_path: Path, chunk_dir: Path) -> Dict[str, Any]:
    if options.backend == "fake":
        return fake_response(chunk)
    if options.backend == "openai-compatible":
        return run_openai_compatible(options, chunk, prompt, schema_path)
    if options.backend == "codex-cli":
        return run_codex_cli(options, chunk, prompt, schema_path, chunk_dir)
    raise ValueError("unsupported backend: %s" % options.backend)


def translate_chunk(
    chunk: Chunk,
    options: TranslationOptions,
    schema_path: Path,
    work_dir: Path,
    source_cue_count: int,
    total_chunks: int,
    max_concurrent_chunks: int,
    emit_progress: Callable[..., None],
    completed_chunk_count: Callable[[], int],
    cancel_event: Optional[threading.Event] = None,
) -> TranslatedChunk:
    retry_note = None
    chunk_dir = work_dir / ("chunk-%03d" % chunk.index)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, options.max_retries + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("translation cancelled")
        emit_progress(
            "translating",
            "Translating subtitle chunk %s of %s." % (chunk.index, total_chunks),
            cue_count=source_cue_count,
            chunk_index=chunk.index,
            total_chunks=total_chunks,
            completed_chunks=completed_chunk_count(),
            max_concurrent_chunks=max_concurrent_chunks,
            first_cue=chunk.cues[0].number,
            last_cue=chunk.cues[-1].number,
            attempt=attempt,
        )
        prompt = build_translation_prompt(options, chunk, retry_note=retry_note)
        try:
            response = run_backend(options, chunk, prompt, schema_path, chunk_dir)
            validated = validate_chunk_response(chunk, response)
            return TranslatedChunk(
                index=chunk.index,
                texts=validated,
                summary={
                    "index": chunk.index,
                    "attempts": attempt,
                    "first_cue": chunk.cues[0].number,
                    "last_cue": chunk.cues[-1].number,
                },
            )
        except Exception as error:
            retry_note = str(error)
            if attempt >= options.max_retries:
                raise
    raise RuntimeError("chunk %s did not produce a translation" % chunk.index)


def translate_cues(source_cues: List[Cue], options: TranslationOptions) -> TranslationRun:
    if options.max_retries < 1:
        raise ValueError("max_retries must be greater than 0")
    started = time.monotonic()
    chunks = chunk_cues(source_cues)
    max_concurrent_chunks = translation_worker_count(chunks)
    progress_lock = threading.Lock()

    def emit_progress(stage: str, message: str, **details: Any) -> None:
        with progress_lock:
            _emit_progress(options, stage, message, **details)

    completed_chunks = 0
    completed_chunks_lock = threading.Lock()

    def completed_chunk_count() -> int:
        with completed_chunks_lock:
            return completed_chunks

    def mark_chunk_completed() -> int:
        nonlocal completed_chunks
        with completed_chunks_lock:
            completed_chunks += 1
            return completed_chunks

    _emit_progress(
        options,
        "translating",
        "Translating subtitle cues.",
        cue_count=len(source_cues),
        total_chunks=len(chunks),
        completed_chunks=0,
        max_concurrent_chunks=max_concurrent_chunks,
    )
    work_dir = options.work_dir or Path(tempfile.mkdtemp(prefix="mpilot.subtitles."))
    cleanup = options.work_dir is None and not options.keep_work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    schema_path = work_dir / "translation.schema.json"
    write_translation_schema(schema_path)

    translated_texts = []
    translated_chunks: Dict[int, List[str]] = {}
    chunk_summaries_by_index: Dict[int, Dict[str, Any]] = {}
    try:
        if chunks:
            next_chunk_index = 0
            pending: Dict[Any, Chunk] = {}
            cancel_event = threading.Event()

            def submit_next_chunk(executor: ThreadPoolExecutor) -> None:
                nonlocal next_chunk_index
                if next_chunk_index >= len(chunks):
                    return
                _stagger_codex_chunk_start(options, next_chunk_index, len(chunks))
                chunk = chunks[next_chunk_index]
                next_chunk_index += 1
                future = executor.submit(
                    translate_chunk,
                    chunk,
                    options,
                    schema_path,
                    work_dir,
                    len(source_cues),
                    len(chunks),
                    max_concurrent_chunks,
                    emit_progress,
                    completed_chunk_count,
                    cancel_event,
                )
                pending[future] = chunk

            with ThreadPoolExecutor(max_workers=max_concurrent_chunks) as executor:
                for _ in range(max_concurrent_chunks):
                    submit_next_chunk(executor)
                while pending:
                    future = next(as_completed(pending))
                    chunk = pending.pop(future)
                    try:
                        translated_chunk = future.result()
                    except Exception:
                        cancel_event.set()
                        for pending_future in pending:
                            pending_future.cancel()
                        raise
                    translated_chunks[translated_chunk.index] = translated_chunk.texts
                    chunk_summaries_by_index[translated_chunk.index] = translated_chunk.summary
                    completed = mark_chunk_completed()
                    emit_progress(
                        "translating",
                        "Translated subtitle chunk %s of %s." % (chunk.index, len(chunks)),
                        cue_count=len(source_cues),
                        chunk_index=chunk.index,
                        total_chunks=len(chunks),
                        completed_chunks=completed,
                        max_concurrent_chunks=max_concurrent_chunks,
                        first_cue=chunk.cues[0].number,
                        last_cue=chunk.cues[-1].number,
                    )
                    submit_next_chunk(executor)
        chunk_summaries = [chunk_summaries_by_index[chunk.index] for chunk in chunks]
        for chunk in chunks:
            translated_texts.extend(translated_chunks[chunk.index])
        target_cues = with_replacement_text(source_cues, translated_texts)
        return TranslationRun(
            cues=target_cues,
            summary={
                "backend": options.backend,
                "model": options.model,
                "cue_count": len(source_cues),
                "chunk_size": INTERNAL_CHUNK_SIZE,
                "chunks": len(chunks),
                "max_concurrent_chunks": max_concurrent_chunks,
                "chunk_start_stagger_seconds": {
                    "min": INTERNAL_CHUNK_START_STAGGER_MIN_SECONDS,
                    "max": INTERNAL_CHUNK_START_STAGGER_MAX_SECONDS,
                }
                if options.backend == "codex-cli" and len(chunks) > 1
                else None,
                "chunk_summaries": chunk_summaries,
                "work_dir": str(work_dir),
                "timings": {"total_seconds": round(time.monotonic() - started, 3)},
            },
        )
    finally:
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)


def _emit_progress(options: TranslationOptions, stage: str, message: str, **details: Any) -> None:
    callback = options.progress_callback
    if callback is None:
        return
    payload = {
        "stage": stage,
        "message": message,
        "details": {key: value for key, value in details.items() if value is not None},
    }
    try:
        callback(payload)
    except Exception:
        pass
