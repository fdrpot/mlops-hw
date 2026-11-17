#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .tools/bin
case "$(uname -s)" in Darwin) os=darwin;; Linux) os=linux;; *) exit 2;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64;; x86_64) arch=amd64;; *) exit 2;; esac
curl -fL --retry 2 "https://kind.sigs.k8s.io/dl/v0.30.0/kind-$os-$arch" -o .tools/bin/kind
curl -fL --retry 2 "https://dl.k8s.io/release/v1.34.0/bin/$os/$arch/kubectl" -o .tools/bin/kubectl
curl -fL --retry 2 "https://dl.k8s.io/release/v1.34.0/bin/$os/$arch/kubectl.sha256" -o .tools/kubectl.sha256
python3 - <<'PY'
import hashlib
from pathlib import Path
assert hashlib.sha256(Path('.tools/bin/kubectl').read_bytes()).hexdigest() == Path('.tools/kubectl.sha256').read_text().strip()
PY
curl -fL --retry 2 "https://get.helm.sh/helm-v3.19.0-$os-$arch.tar.gz" -o .tools/helm.tar.gz
curl -fL --retry 2 "https://get.helm.sh/helm-v3.19.0-$os-$arch.tar.gz.sha256sum" -o .tools/helm.sha256
python3 - <<'PY'
import hashlib
from pathlib import Path
assert hashlib.sha256(Path('.tools/helm.tar.gz').read_bytes()).hexdigest() == Path('.tools/helm.sha256').read_text().split()[0]
PY
tar -xzf .tools/helm.tar.gz -C .tools
cp ".tools/$os-$arch/helm" .tools/bin/helm
chmod +x .tools/bin/{kind,kubectl,helm}
