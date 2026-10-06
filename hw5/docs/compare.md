# Базовая модель против адаптера

Адаптер: `models/adapter_all_layers`. Генерация жадная, до 40 токенов.

## 1. List all pods in the default namespace

**База:**

> list pods in the default namespace

**Адаптер:**

> kubectl get pods -n default

## 2. Stream live logs from a pod with 50 previous lines

**База:**

> stream-live-logs --pod <pod-name> --lines 50

**Адаптер:**

> kubectl logs -n myapp -c myapp -f --previous 50

## 3. Compare disk usage of two directories

**База:**

> none

**Адаптер:**

> duf /var/log /var/log/backup

## 4. Show disk usage of files in current directory sorted by size

**База:**

> show disk usage of files in current directory sorted by size

**Адаптер:**

> duf --sort=size

## 5. Execute an interactive bash shell inside a running pod for debugging

**База:**

> ```bash / kubectl exec -it <pod-name> -- bash / ```

**Адаптер:**

> podman exec -it <pod> /bin/bash
