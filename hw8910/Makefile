UV ?= uv
CHECK_ENV ?= .venv-check
WORKSPACE ?= .lab
REAL_WORKSPACE ?= .real
PORT ?= 8091
WARMUP_SECONDS ?= 0
DEBUG ?= 0
DEVICE ?= auto
MANIFEST ?= manifest-B.json
REPORT ?= report-B.json
FLOW_FLAGS ?=
SNAPSHOT ?=
ORIGINAL_OUTPUT ?= .lab-original
CHECK_LOG ?= evidence/check-all.log
FAILURE_LOG ?= evidence/intentional-failure.log
PROCESS_OUTPUT ?= evidence/process
BENCH_OUTPUT ?= evidence/benchmark
API_URL ?= http://127.0.0.1:$(PORT)
QUICK = UV_PROJECT_ENVIRONMENT="$(CHECK_ENV)" $(UV) run --frozen
MODEL = $(UV) run --frozen --extra model
DEMO = $(QUICK) python scripts/demo.py
FLOW_COMMAND = $(QUICK) python flow.py --workspace "$(WORKSPACE)" --manifest "$(MANIFEST)" --report "$(REPORT)"

.PHONY: install install-model check check-light check-original check-rollback intentional-failure \
	process-evidence prefect-evidence init flow flow-fail flow-crash flow-demo input-change \
	publications accepted-hash gate real-evaluate restore-baseline real-evidence \
	serve serve-real health ready debug generate invalid-request bench

install:
	UV_PROJECT_ENVIRONMENT="$(CHECK_ENV)" $(UV) sync --frozen

install-model:
	$(UV) sync --frozen --extra model

check:
	$(QUICK) python scripts/check.py --log "$(CHECK_LOG)"

check-light: install check

# Deliberate nonzero exit. Only the child process is patched; no source edits.
intentional-failure:
	$(QUICK) python scripts/check.py --inject-bug --log "$(FAILURE_LOG)"

check-original:
	$(DEMO) check-original --output "$(ORIGINAL_OUTPUT)"

check-rollback:
	PYTHONPATH=integration $(QUICK) python -m unittest \
		test_integration.ReleaseIntegrationTests.test_http_loaded_version_changes_only_after_restart_and_rolls_back -v

process-evidence:
	$(QUICK) python scripts/evidence.py --output "$(PROCESS_OUTPUT)"

prefect-evidence:
	$(QUICK) python scripts/prefect_evidence.py --workspace "$(WORKSPACE)" --output "$(PROCESS_OUTPUT)"

init:
	$(QUICK) python lab.py --workspace "$(WORKSPACE)" init

flow:
	$(FLOW_COMMAND) $(FLOW_FLAGS)

flow-fail:
	$(FLOW_COMMAND) --fail-after-prepare

flow-crash:
	$(FLOW_COMMAND) --fail-after-publish

input-change:
	$(DEMO) input-change --workspace "$(WORKSPACE)"

publications:
	$(DEMO) publications --workspace "$(WORKSPACE)"

accepted-hash:
	$(DEMO) accepted-hash --workspace "$(REAL_WORKSPACE)"

gate:
	$(QUICK) python flow.py --workspace "$(REAL_WORKSPACE)" \
		--manifest manifest-candidate.json --report release-report.json

# init deliberately refuses to overwrite an existing experiment.
flow-demo: init flow

real-evaluate:
	$(MODEL) python build_release.py --workspace "$(REAL_WORKSPACE)" --device "$(DEVICE)"

restore-baseline:
	$(MODEL) python scripts/restore_baseline.py --workspace "$(REAL_WORKSPACE)" \
		$(if $(strip $(SNAPSHOT)),--snapshot "$(SNAPSHOT)",)

real-evidence:
	$(MODEL) python scripts/real_evidence.py --workspace "$(REAL_WORKSPACE)" --benchmark-output "$(BENCH_OUTPUT)"

serve:
	$(QUICK) python service.py --workspace "$(WORKSPACE)" --port $(PORT) --warmup-seconds 1

serve-real:
	$(MODEL) python service.py --workspace "$(REAL_WORKSPACE)" --port $(PORT) \
		--backend real --warmup-seconds "$(WARMUP_SECONDS)" $(if $(filter 1,$(DEBUG)),--debug,)

health ready debug generate invalid-request:
	$(DEMO) http --url "$(API_URL)" --action "$@"

bench:
	$(QUICK) python benchmark.py --url http://127.0.0.1:$(PORT) --output "$(BENCH_OUTPUT)" --requests 8 --prompts-jsonl evidence/real/source/golden.jsonl
