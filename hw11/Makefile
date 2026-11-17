export KUBECONFIG := $(CURDIR)/.kubeconfig
export DOCKER_CONTEXT := colima-hw11
export PATH := $(CURDIR)/.tools/bin:$(PATH)
PYTHON ?= .venv/bin/python
.PHONY: install up check check-k8s a b c experiments report delete stop
install:
	uv sync --locked --group report
up:
	./scripts/stand.sh up
check:
	$(PYTHON) -m unittest discover -s tests -v
	.tools/bin/helm lint chart
check-k8s:
	HW11_K8S_TESTS=1 $(PYTHON) -m unittest discover -s tests -v
a b c:
	$(PYTHON) scripts/experiments.py $@
experiments:
	$(PYTHON) scripts/experiments.py all
report:
	$(PYTHON) scripts/report.py
delete:
	./scripts/stand.sh delete
stop:
	./scripts/stand.sh stop
