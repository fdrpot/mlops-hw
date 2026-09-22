"""Стадия collect: CLI-1M Parquet → нормализованный chat JSONL."""

import hashlib
import json
import shlex
import time
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

from src.config import load_params, source_files

COLUMNS = ["messages", "id", "shell", "language", "bucket", "quality_tier", "source", "license_spdx", "quality_flags"]


def stable_number(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest(), 16)


def command_tool(command: str) -> str | None:
    """Извлечь исполняемую утилиту из команды без запуска команды."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens.pop(0)
    while tokens and tokens[0] in {"sudo", "env", "command", "nohup"}:
        tokens.pop(0)
    if not tokens or tokens[0] in {"|", "&&", ";"}:
        return None
    return Path(tokens[0]).name.lower()


def choose_prompt(example_id: str, variants: list[str]) -> str:
    return variants[stable_number(example_id) % len(variants)]


def iter_source_rows(paths: list[Path]):
    for source in paths:
        files = sorted(source.glob("*.parquet")) if source.is_dir() else [source]
        if not files:
            raise SystemExit(f"в источнике нет parquet-файлов: {source}")
        for file in files:
            parquet = pq.ParquetFile(file)
            for batch in parquet.iter_batches(batch_size=5000, columns=COLUMNS):
                yield from batch.to_pylist()


def main() -> None:
    params = load_params()
    cfg = params["collect"]
    paths = params["paths"]
    version = cfg["version"]
    allowed_buckets = list(cfg["versions"][version]["buckets"])
    allowed_licenses = set(cfg["licenses"])
    prompts = cfg["system_prompts"]
    if len(prompts) < 3:
        raise SystemExit("collect.system_prompts должен содержать минимум три варианта")

    started = time.perf_counter()
    scanned = 0
    rejected = Counter()
    candidates: dict[str, list[dict]] = defaultdict(list)

    for row in iter_source_rows(source_files(params)):
        scanned += 1
        if row["language"] != cfg["language"]:
            rejected["language"] += 1
            continue
        if row["shell"] != cfg["shell"]:
            rejected["shell"] += 1
            continue
        if row["license_spdx"] not in allowed_licenses:
            rejected["license"] += 1
            continue
        if row.get("quality_flags"):
            rejected["quality_flags"] += 1
            continue
        bucket = next((name for name in allowed_buckets if name in (row["bucket"] or [])), None)
        if bucket is None:
            rejected["bucket"] += 1
            continue
        messages = row.get("messages") or []
        if len(messages) != 3 or [m.get("role") for m in messages] != ["system", "user", "assistant"]:
            rejected["messages"] += 1
            continue
        user = str(messages[1].get("content") or "").strip()
        assistant = str(messages[2].get("content") or "").strip()
        tool = command_tool(assistant)
        if not user or not assistant or tool is None:
            rejected["content"] += 1
            continue
        topic = f"{bucket}/{tool}"
        candidates[topic].append({
            "id": f"cli1m-{row['id']}",
            "topic": topic,
            "messages": [
                {"role": "system", "content": choose_prompt(str(row["id"]), prompts)},
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            ],
            "_license": row["license_spdx"],
            "_quality": row["quality_tier"],
            "_source": row["source"],
        })

    selected: list[dict] = []
    cap = int(cfg["max_per_topic"])
    for topic in sorted(candidates):
        ranked = sorted(candidates[topic], key=lambda row: stable_number(row["id"]))
        selected.extend(ranked[:cap])
    selected.sort(key=lambda row: row["id"])

    out = Path(paths["raw"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in selected:
            public = {key: row[key] for key in ("id", "topic", "messages")}
            fh.write(json.dumps(public, ensure_ascii=False) + "\n")

    elapsed = round(time.perf_counter() - started, 2)
    metrics = {
        "version": version,
        "dataset": cfg["dataset"],
        "rows_scanned": scanned,
        "rows_eligible": sum(len(rows) for rows in candidates.values()),
        "rows_written": len(selected),
        "topics": len({row["topic"] for row in selected}),
        "system_prompt_variants": len({row["messages"][0]["content"] for row in selected}),
        "buckets": dict(sorted(Counter(row["topic"].split("/", 1)[0] for row in selected).items())),
        "licenses": dict(sorted(Counter(row["_license"] for row in selected).items())),
        "quality_tiers": dict(sorted(Counter(row["_quality"] for row in selected).items())),
        "source_kinds": dict(Counter(row["_source"] for row in selected).most_common()),
        "rejected": dict(sorted(rejected.items())),
    }
    metrics_path = Path(paths["metrics_collect"])
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"collect: {version}, просмотрено {scanned}, пригодно {metrics['rows_eligible']}, записано {len(selected)} строк в {metrics['topics']} темах, {elapsed} с")


if __name__ == "__main__":
    main()
