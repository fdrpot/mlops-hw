"""Record real process/filesystem evidence for release failure and recovery.

Every command, output, return code and accepted-pointer hash is saved. Workspaces
are temporary and independent; only small diagnostic snapshots survive the run.
The optional --skip-crash supports the unmodified starter CLI, which did not have
the new crash-injection switch. It does not turn a missing switch into evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def rows(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class Evidence:
    def __init__(self, project: Path, output: Path, python: str, timeout: float):
        self.project = project.resolve()
        self.output = output
        self.python = python
        self.timeout = timeout
        self.commands = []
        self.checks = []
        self.snapshots = {}

    def check(self, name: str, passed: bool, detail=None) -> None:
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail})

    def command(self, name: str, workspace: Path, *arguments: str) -> dict:
        command = [self.python, str(self.project / "lab.py"), "--workspace",
                   str(workspace), *arguments]
        before = digest(workspace / "accepted.json")
        try:
            process = subprocess.run(command, cwd=self.project, capture_output=True,
                                     text=True, timeout=self.timeout)
            record = {"name": name, "command": command, "cwd": str(self.project),
                      "returncode": process.returncode, "stdout": process.stdout,
                      "stderr": process.stderr}
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout or b""
            stderr = error.stderr or b""
            record = {"name": name, "command": command, "cwd": str(self.project),
                      "returncode": None, "error": "timeout",
                      "stdout": stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout,
                      "stderr": stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr}
        record.update(accepted_before_sha256=before,
                      accepted_after_sha256=digest(workspace / "accepted.json"))
        self.commands.append(record)
        log = [f"command: {json.dumps(command, ensure_ascii=False)}",
               f"cwd: {self.project}", f"exit_code: {record['returncode']}",
               f"accepted_before_sha256: {before}",
               f"accepted_after_sha256: {record['accepted_after_sha256']}",
               "stdout:", record["stdout"], "stderr:", record["stderr"]]
        (self.output / f"{len(self.commands):02d}-{name}.log").write_text("\n".join(log), encoding="utf-8")
        return record

    def initialize(self, name: str, temporary_root: Path) -> Path:
        workspace = temporary_root / name
        record = self.command(name + "-init", workspace, "init")
        if record["returncode"] != 0:
            raise RuntimeError(f"Fixture initialization failed: {name}; see output logs")
        return workspace

    def snapshot(self, name: str, workspace: Path) -> None:
        self.snapshots[name] = {
            "accepted_sha256": digest(workspace / "accepted.json"),
            "accepted": load(workspace / "accepted.json"),
            "publications": rows(workspace / "publications.jsonl"),
            "attempts": rows(workspace / "attempts.jsonl"),
            "cache_entries": sorted(path.name for path in (workspace / "cache").glob("*.json")),
        }

    def refused(self, name: str, root: Path, report: str) -> None:
        workspace = self.initialize(name, root)
        record = self.command(name, workspace, "run", "--report", report)
        self.check(name + "-nonzero-exit", record["returncode"] not in (0, None), record["returncode"])
        self.check(name + "-pointer-byte-identical",
                   record["accepted_before_sha256"] == record["accepted_after_sha256"],
                   {"before": record["accepted_before_sha256"], "after": record["accepted_after_sha256"]})
        self.snapshot(name, workspace)

    def scenarios(self, root: Path, skip_crash: bool) -> None:
        workspace = self.initialize("recovery", root)
        failed = self.command("prepare-injected-failure", workspace, "run", "--fail-after-prepare")
        self.check("prepare-failure-nonzero", failed["returncode"] not in (0, None))
        self.check("prepare-failure-preserves-pointer",
                   failed["accepted_before_sha256"] == failed["accepted_after_sha256"])
        recovered = self.command("prepare-new-process-recovery", workspace, "run")
        self.check("recovery-success", recovered["returncode"] == 0)
        self.check("recovery-uses-durable-cache", "CACHE_HIT" in recovered["stdout"], recovered["stdout"])
        (workspace / "input.txt").write_text("Изменённые байты по тому же пути\n", encoding="utf-8")
        changed = self.command("input-changed-same-path", workspace, "run")
        self.check("changed-input-invalidates-cache",
                   changed["returncode"] == 0 and "PREPARED" in changed["stdout"] and "CACHE_HIT" not in changed["stdout"],
                   changed["stdout"])
        for field, value in (("tokenizer_revision", "r2"), ("template_sha256", "c" * 64),
                             ("max_length", 64), ("code_revision", "prepare-v2")):
            config = load(workspace / "config.json")
            config[field] = value
            (workspace / "config.json").write_text(json.dumps(config), encoding="utf-8")
            record = self.command("config-changed-" + field, workspace, "run")
            self.check(field + "-invalidates-cache",
                       record["returncode"] == 0 and "PREPARED" in record["stdout"] and "CACHE_HIT" not in record["stdout"],
                       record["stdout"])
        self.snapshot("recovery-and-invalidation", workspace)

        self.refused("quality-rejection", root, "report-reject.json")
        self.refused("incomplete-report", root, "report-incomplete.json")
        self.refused("foreign-report", root, "report-A.json")

        workspace = self.initialize("nonfinite-report", root)
        report = load(workspace / "report-B.json")
        report["metrics"]["accuracy"] = float("nan")
        (workspace / "report-B.json").write_text(json.dumps(report), encoding="utf-8")
        nonfinite = self.command("nonfinite-report", workspace, "run")
        self.check("nonfinite-report-refused", nonfinite["returncode"] not in (0, None))
        self.check("nonfinite-report-preserves-pointer",
                   nonfinite["accepted_before_sha256"] == nonfinite["accepted_after_sha256"])
        self.snapshot("nonfinite-report", workspace)

        workspace = self.initialize("repeat", root)
        first = self.command("publication-first-process", workspace, "run")
        second = self.command("publication-second-process", workspace, "run")
        publications = rows(workspace / "publications.jsonl")
        self.check("repeat-success", first["returncode"] == second["returncode"] == 0)
        self.check("repeat-pointer-byte-identical", second["accepted_before_sha256"] == second["accepted_after_sha256"])
        self.check("repeat-one-publication-event", len(publications) == 1, publications)
        self.snapshot("repeat", workspace)

        if not skip_crash:
            workspace = self.initialize("publication-crash", root)
            failed = self.command("crash-between-pointer-and-journal", workspace, "run", "--fail-after-publish")
            self.check("publication-crash-nonzero", failed["returncode"] not in (0, None))
            self.check("publication-crash-updated-pointer",
                       failed["accepted_before_sha256"] != failed["accepted_after_sha256"])
            recovered = self.command("publication-crash-new-process-recovery", workspace, "run")
            repeated = self.command("publication-crash-repeat", workspace, "run")
            publications = rows(workspace / "publications.jsonl")
            self.check("publication-crash-recovery-success", recovered["returncode"] == repeated["returncode"] == 0)
            self.check("publication-crash-recovery-same-pointer",
                       recovered["accepted_before_sha256"] == recovered["accepted_after_sha256"])
            self.check("publication-crash-exactly-one-event", len(publications) == 1, publications)
            self.snapshot("publication-crash", workspace)

    def save(self, skip_crash: bool) -> dict:
        result = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "project": str(self.project), "python": self.python,
            "synthetic": True, "description": "STUB ONLY process and filesystem evidence; not model evaluation",
            "crash_scenario_skipped": skip_crash,
            "passed": bool(self.checks) and all(item["passed"] for item in self.checks),
            "checks": self.checks, "commands": self.commands, "snapshots": self.snapshots,
        }
        (self.output / "evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("reports/process"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--skip-crash", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "evidence.json").exists():
        parser.error("output already contains evidence.json; choose a fresh --output")
    evidence = Evidence(args.project, args.output, args.python, args.timeout)
    try:
        with tempfile.TemporaryDirectory(prefix="hw8910-evidence-") as temporary:
            evidence.scenarios(Path(temporary), args.skip_crash)
    except Exception as error:
        evidence.check("scenario-execution-complete", False,
                       f"{type(error).__name__}: {error}")
    finally:
        result = evidence.save(args.skip_crash)
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"]),
                      "failed": [item["name"] for item in result["checks"] if not item["passed"]],
                      "output": str(args.output)}, ensure_ascii=False))
    return int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
