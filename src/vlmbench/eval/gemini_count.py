#!/usr/bin/env python3
"""Evaluate real edited images with count-style open questions using Gemini."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from tqdm import tqdm

from vlmbench.eval.scoring import normalize_count


THREAD_LOCAL = threading.local()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean_key(raw: str) -> str:
    key = raw.strip()
    if "=" in key:
        key = key.split("=", 1)[1].strip()
    if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
        key = key[1:-1].strip()
    return key


def resolve_key(root: Path, explicit: Path | None) -> str:
    candidates = [explicit] if explicit else []
    candidates.extend(parent / "gemini_api_key.txt" for parent in [root, *root.parents])
    for path in candidates:
        if path and path.exists():
            key = clean_key(path.read_text(encoding="utf-8"))
            if key:
                return key
    key = clean_key(os.environ.get("GEMINI_API_KEY", ""))
    if key:
        return key
    raise EnvironmentError("Set GEMINI_API_KEY or pass --api-key-file.")


def image_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    raise ValueError(f"Unsupported image type: {path}")


def ask_gemini(
    *,
    api_key: str,
    model: str,
    image_path: Path,
    question: str,
    temperature: float,
    max_output_tokens: int,
    timeout_ms: int,
) -> str:
    client = getattr(THREAD_LOCAL, "client", None)
    if client is None:
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
        THREAD_LOCAL.client = client
    prompt = f"Question: {question}\n\nAnswer with only the count or shortest possible count phrase. Do not explain."
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=image_path.read_bytes(), mime_type=image_mime(image_path)),
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=temperature,
            candidate_count=1,
            max_output_tokens=max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    raw = str(response.text or "").strip()
    if not raw:
        raise RuntimeError("Gemini returned an empty response.")
    return raw


def evaluate_row(
    row: dict[str, Any],
    *,
    root: Path,
    api_key: str,
    model: str,
    temperature: float,
    max_output_tokens: int,
    timeout_ms: int,
    retries: int,
) -> dict[str, Any]:
    image_path = root / str(row["image"])
    if not image_path.exists():
        raise FileNotFoundError(image_path)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            raw = ask_gemini(
                api_key=api_key,
                model=model,
                image_path=image_path,
                question=str(row["question"]),
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                timeout_ms=timeout_ms,
            )
            prediction = normalize_count(raw)
            expected = normalize_count(row["answer"])
            return {
                **row,
                "model": model,
                "temperature": temperature,
                "thinking_budget": 0,
                "max_output_tokens": max_output_tokens,
                "raw_prediction": raw,
                "prediction": prediction,
                "expected_normalized": expected,
                "correct": prediction == expected,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"[WARN] {row.get('id')} attempt {attempt}/{retries}: {type(exc).__name__}: {exc}", file=sys.stderr)
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Failed after {retries} retries: {last_error}") from last_error


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    correct = sum(bool(row.get("correct")) for row in rows)
    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else None,
        "prediction_distribution": dict(Counter(str(row.get("prediction", "")) for row in rows)),
    }


def main() -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=Path, default=root / "questions.jsonl")
    parser.add_argument("--model", default="gemini-3.5-flash")
    parser.add_argument("--output", type=Path, default=root / "results" / "gemini_3_5_flash_predictions.jsonl")
    parser.add_argument("--metrics", type=Path, default=root / "results" / "gemini_3_5_flash_metrics.json")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=12)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = load_jsonl(args.questions)
    existing = [] if args.overwrite else load_jsonl(args.output)
    completed = {str(row.get("id", "")) for row in existing}
    pending = [row for row in rows if str(row.get("id", "")) not in completed]

    print(f"Questions: {args.questions}")
    print(f"Model: {args.model}")
    print(f"Temperature: {args.temperature}; thinking budget: 0")
    print(f"Completed: {len(completed)}; pending: {len(pending)}; workers: {args.workers}")
    if args.dry_run:
        for row in pending[:20]:
            print(f"{row['id']} expected={normalize_count(row['answer'])} image_exists={(root / row['image']).exists()} image={row['image']}")
        return 0

    api_key = resolve_key(root, args.api_key_file) if pending else ""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if args.overwrite else "a"
    new_results: list[dict[str, Any]] = []
    with args.output.open(mode, encoding="utf-8") as handle:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    evaluate_row,
                    row,
                    root=root,
                    api_key=api_key,
                    model=args.model,
                    temperature=args.temperature,
                    max_output_tokens=args.max_output_tokens,
                    timeout_ms=args.timeout_seconds * 1000,
                    retries=args.retries,
                ): row
                for row in pending
            }
            for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures)):
                result = future.result()
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
                new_results.append(result)

    all_results = new_results if mode == "w" else existing + new_results
    metrics = summarize(all_results)
    write_json(args.metrics, metrics)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"Predictions: {args.output}")
    print(f"Metrics: {args.metrics}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
