UV ?= uv
CHECK_ENV ?= .venv-check
WORKSPACE ?= .lab
REAL_WORKSPACE ?= .real
PORT ?= 8091
PROCESS_OUTPUT ?= evidence/process
BENCH_OUTPUT ?= evidence/benchmark
QUICK = UV_PROJECT_ENVIRONMENT="$(CHECK_ENV)" $(UV) run --frozen
MODEL = $(UV) run --frozen --extra model

.PHONY: install install-model check check-light intentional-failure process-evidence init flow flow-demo real-evaluate serve serve-real bench

install:
	UV_PROJECT_ENVIRONMENT="$(CHECK_ENV)" $(UV) sync --frozen

install-model:
	$(UV) sync --frozen --extra model

check:
	$(QUICK) python scripts/check.py --log evidence/check-all.log

check-light: install check

# Deliberate nonzero exit. Only the child process is patched; no source edits.
intentional-failure:
	$(QUICK) python scripts/check.py --inject-bug --log evidence/intentional-failure.log

process-evidence:
	$(QUICK) python scripts/evidence.py --output "$(PROCESS_OUTPUT)"

init:
	$(QUICK) python lab.py --workspace "$(WORKSPACE)" init

flow:
	$(QUICK) python flow.py --workspace "$(WORKSPACE)"

# init deliberately refuses to overwrite an existing experiment.
flow-demo: init flow

real-evaluate:
	$(MODEL) python build_release.py --workspace "$(REAL_WORKSPACE)"

serve:
	$(QUICK) python service.py --workspace "$(WORKSPACE)" --port $(PORT) --warmup-seconds 1

serve-real:
	$(MODEL) python service.py --workspace "$(REAL_WORKSPACE)" --port $(PORT) --backend real --warmup-seconds 0

bench:
	$(QUICK) python benchmark.py --url http://127.0.0.1:$(PORT) --output "$(BENCH_OUTPUT)" --requests 8 --prompts-jsonl evidence/real/source/golden.jsonl
