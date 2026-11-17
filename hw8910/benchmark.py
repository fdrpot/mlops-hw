"""Small, auditable HTTP benchmark; all attempts, including errors, are retained."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener


PROMPTS = (
    "Как вывести текущий рабочий каталог в Linux? Ответь одной командой.",
    "Как показать скрытые файлы в текущем каталоге? Ответь одной командой.",
    "Как создать каталог reports? Ответь одной командой.",
    "Как скопировать файл input.txt в backup.txt? Ответь одной командой.",
    "Как посчитать строки в файле data.txt? Ответь одной командой.",
    "Как вывести последние 10 строк файла app.log? Ответь одной командой.",
    "Как найти слово error в файле app.log? Ответь одной командой.",
    "Как вывести список запущенных процессов Linux? Ответь одной командой.",
)
HTTP = build_opener(ProxyHandler({}))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def wait_ready(base_url: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last_error = "not contacted"
    while time.monotonic() < deadline:
        try:
            with HTTP.open(base_url + "/ready", timeout=min(5, timeout)) as response:
                payload = json.load(response)
                if response.status == 200 and payload.get("ready") is True:
                    return payload
                last_error = repr(payload)
        except (HTTPError, URLError, OSError, ValueError) as error:
            last_error = str(error)
        time.sleep(0.25)
    raise TimeoutError(f"API did not become ready: {last_error}")


def attempt(base_url: str, prompt: str, index: int, concurrency: int,
            timeout: float, allow_stub: bool) -> dict:
    row = {
        "client_request_id": str(uuid.uuid4()), "started_at": utc_now(),
        "prompt_index": index, "prompt": prompt, "concurrency": concurrency,
        "max_new_tokens": 32, "status": None, "success": False, "error": None,
        "request_id": None, "model_id": None, "runtime_sha256": None,
        "output_tokens": None, "generation_seconds": None, "queue_seconds": None,
        "server_total_seconds": None, "answer": None,
    }
    request = Request(
        base_url + "/generate",
        data=json.dumps({"prompt": prompt, "max_new_tokens": 32}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    started = time.perf_counter()
    try:
        with HTTP.open(request, timeout=timeout) as response:
            row["status"] = response.status
            payload = json.load(response)
        row["response"] = payload
        if not isinstance(payload, dict):
            raise ValueError("response must be a JSON object")
        for field in ("request_id", "model_id", "runtime_sha256", "output_tokens",
                      "generation_seconds", "queue_seconds", "answer"):
            row[field] = payload.get(field)
        row["server_total_seconds"] = payload.get("total_seconds")
        if not isinstance(row["answer"], str) or not row["model_id"]:
            raise ValueError("response lacks answer or actual model_id")
        if type(row["output_tokens"]) is not int or row["output_tokens"] < 0:
            raise ValueError("response lacks a valid actual output token count")
        row["synthetic"] = "STUB ONLY" in row["answer"]
        if row["synthetic"] and not allow_stub:
            raise ValueError("STUB ONLY response cannot be a real-model benchmark; use --allow-stub for diagnostics")
        row["success"] = True
    except HTTPError as error:
        row["status"] = error.code
        row["error"] = f"HTTPError: {error}"
        row["error_body"] = error.read().decode("utf-8", errors="replace")
    except (URLError, OSError, ValueError, TypeError) as error:
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        row["client_seconds"] = time.perf_counter() - started
        row["finished_at"] = utc_now()
    return row


def average(rows: list[dict], field: str) -> float | None:
    values = [r[field] for r in rows if isinstance(r.get(field), (int, float))]
    return statistics.mean(values) if values else None


def summarize(rows: list[dict], concurrency: int, batch_seconds: float) -> dict:
    good = [row for row in rows if row["success"]]
    bad = [row for row in rows if not row["success"]]
    return {
        "concurrency": concurrency, "n": len(rows), "successes": len(good),
        "errors": len(bad), "batch_seconds": batch_seconds,
        "successful_requests_per_second": len(good) / batch_seconds,
        "mean_success_client_seconds": average(good, "client_seconds"),
        "max_success_client_seconds": max((r["client_seconds"] for r in good), default=None),
        "mean_error_client_seconds": average(bad, "client_seconds"),
        "max_error_client_seconds": max((r["client_seconds"] for r in bad), default=None),
        "mean_generation_seconds": average(good, "generation_seconds"),
        "mean_queue_seconds": average(good, "queue_seconds"),
        "total_output_tokens": sum(r.get("output_tokens") or 0 for r in good),
        "model_ids": ";".join(sorted({str(r["model_id"]) for r in good})),
        "runtime_sha256": ";".join(sorted({str(r["runtime_sha256"]) for r in good})),
    }


def run(base_url: str, output: Path, count: int = 8, timeout: float = 300,
        ready_timeout: float = 180, allow_stub: bool = False) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    raw_path = output / "requests.jsonl"
    if raw_path.exists():
        raise FileExistsError(f"Refusing to overwrite prior attempts: {raw_path}")
    base_url = base_url.rstrip("/")
    metadata = {
        "started_at": utc_now(), "base_url": base_url, "requests_per_mode": count,
        "concurrency": [1, 2], "max_new_tokens": 32, "timeout_seconds": timeout,
        "cpu_reduced_sample": count == 4, "allow_stub": allow_stub,
        "prompts": list(PROMPTS[:count]),
        "measurement": "complete JSON response latency; TTFT/ITL/p95 not measured",
    }
    metadata_path = output / "metadata.json"
    try:
        metadata["readiness"] = wait_ready(base_url, ready_timeout)
    except Exception as error:
        metadata["error"] = f"{type(error).__name__}: {error}"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        raise
    summaries = []
    with raw_path.open("x", encoding="utf-8") as raw:
        for concurrency in (1, 2):
            rows = []
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [executor.submit(attempt, base_url, prompt, index, concurrency,
                                           timeout, allow_stub)
                           for index, prompt in enumerate(PROMPTS[:count], start=1)]
                for future in as_completed(futures):
                    row = future.result()
                    rows.append(row)
                    raw.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                    raw.flush()
            summary = summarize(rows, concurrency, time.perf_counter() - started)
            summaries.append(summary)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    (output / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2) + "\n")
    metadata["finished_at"] = utc_now()
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return summaries


def main() -> int:
    global PROMPTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8091")
    parser.add_argument("--output", type=Path, default=Path("reports/benchmark"))
    parser.add_argument("--requests", type=int, choices=(4, 8), default=8,
                        help="4 is the explicitly labelled slow-CPU exception")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--ready-timeout", type=float, default=180)
    parser.add_argument("--allow-stub", action="store_true", help="diagnostic STUB benchmark only")
    parser.add_argument("--prompts-jsonl", type=Path,
                        help="Use the input field of the first 8 measured golden questions")
    args = parser.parse_args()
    if args.timeout <= 0 or args.ready_timeout <= 0:
        parser.error("timeouts must be positive")
    if args.prompts_jsonl:
        rows = [json.loads(line) for line in args.prompts_jsonl.read_text().splitlines() if line.strip()]
        selected = tuple(row['input'] for row in rows[:8])
        if len(selected) < args.requests or any(not isinstance(p, str) or not p.strip() for p in selected):
            parser.error("prompt file needs enough nonempty input fields")
        PROMPTS = selected
    summaries = run(args.url, args.output, args.requests, args.timeout,
                    args.ready_timeout, args.allow_stub)
    return int(any(summary["errors"] for summary in summaries))


if __name__ == "__main__":
    raise SystemExit(main())
