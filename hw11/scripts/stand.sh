#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_CONTEXT=colima-hw11
export KUBECONFIG="$PWD/.kubeconfig"
export PATH="$PWD/.tools/bin:$PATH"
case "${1:-up}" in
  up)
    colima start --profile hw11 --cpu 4 --memory 6 --disk 16 --vm-type vz --mount-type virtiofs
    # Colima changes the global Docker context; keep existing projects on default.
    docker context use colima
    if ! kind get clusters | rg -qx hw11; then
      kind create cluster --name hw11 --config kind.yaml --kubeconfig "$KUBECONFIG" --wait 120s
    fi
    docker build -t hw11-api:local .
    kind load docker-image hw11-api:local --name hw11
    kubectl apply -f monitoring/metrics-server.yaml
    helm upgrade --install hw11 chart --namespace hw11 --create-namespace --wait --timeout 5m
    ;;
  delete)
    kind delete cluster --name hw11
    colima delete --profile hw11 --force
    ;;
  stop) colima stop --profile hw11 ;;
  *) echo 'usage: scripts/stand.sh {up|stop|delete}' >&2; exit 2 ;;
esac
