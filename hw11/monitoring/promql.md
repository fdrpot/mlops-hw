# PromQL и границы сравнения

Scrape interval и evaluation interval — 5 s. Histogram buckets поступают от каждого
Pod; request/session ID остаются только в JSON-журнале. `pod`, `model_version`
и outcome имеют ограниченную кардинальность. Все длительности в секундах.

```promql
# p95 TTFC, суммирование bucket ДО вычисления перцентиля, le сохраняется
histogram_quantile(0.95, sum by (le, pod) (rate(hw11_ttfc_seconds_bucket[1m])))
# Совокупный p50 без усреднения готовых перцентилей
histogram_quantile(0.50, sum by (le) (rate(hw11_ttfc_seconds_bucket[1m])))
histogram_quantile(0.95, sum by (le, pod) (rate(hw11_total_seconds_bucket[1m])))
histogram_quantile(0.95, sum by (le, pod) (rate(hw11_queue_seconds_bucket[1m])))
sum by (pod) (rate(hw11_requests_total{outcome="complete"}[1m]))
sum by (pod,outcome) (rate(hw11_requests_total{outcome!="complete"}[1m]))
hw11_running
hw11_waiting
sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="hw11",container="api"}[1m]))
sum by (pod) (container_memory_working_set_bytes{namespace="hw11",container="api"})
sum(kube_pod_status_ready{namespace="hw11",condition="true",pod=~"hw11-api-.*"})
```

Для проверки по одному и тому же набору успешных ответов используйте разность
счётчиков между двумя сохранёнными scrape после завершения/до запуска, без rollout
и сброса метрик, и успешные клиентские записи этого промежутка. HTTP TTFC клиента
включает сеть и парсинг, серверный Histogram — время от admission до первой части.
Поэтому это связанные, но разные измеряемые величины; дополнительно сравните
переданный сервером `ttfc_seconds`. Bucket quantile интерполируется и не обязан
равняться точному эмпирическому клиентскому quantile. `rate`/`increase` применяют
экстраполяцию; для точного количества используйте снятые raw counter deltas.

Два алерта (`alerts.yml`, values позволяют менять пороги): p95 TTFC > 0.6 s и доля
отказов > 10%, для каждого минимум 10 наблюдений за 30 s и удержание 10 s. При
нагрузке сохраняются `/api/v1/alerts` с состояниями pending/firing, затем inactive.
Уведомления наружу не настроены; учебный опыт наблюдает состояние правил Prometheus.

Версии закреплены в values.yaml; первичные источники:

- https://github.com/prometheus/prometheus/releases/tag/v3.5.0
- https://github.com/grafana/grafana/releases/tag/v12.1.0
- https://github.com/kubernetes/kube-state-metrics/releases/tag/v2.16.0
- https://github.com/kubernetes-sigs/metrics-server/releases/tag/v0.8.0
- https://prometheus.io/docs/practices/histograms/
- https://prometheus.io/docs/prometheus/latest/configuration/configuration/#kubernetes_sd_config

metrics-server проверяет CPU resource metrics API для HPA. Локальный kind использует
самоподписанный kubelet TLS: `--kubelet-insecure-tls` и APIService insecureSkipTLSVerify
допустимы только для изолированного учебного стенда. Prometheus API proxy TLS проверяет
CA ServiceAccount. Никакие прежние контейнеры/volumes пользователя не удаляются.

## Проверенный калибровочный набор

`results/monitoring/checks.json`: восемь проверок PASS. Обнаружены два API scrape
и по одному cAdvisor/KSM, все `up`. Grafana 12.1.0 возвращает `database=ok` и
провизионенный дашборд из 12 панелей. CPU/RAM series присутствуют для обоих API Pod.

В отдельный тихий период выполнены 20 последовательных HTTP-запросов, все завершены;
ровно 20 записей `request_complete` сопоставлены с request ID и ровно 20 новых
наблюдений Histogram подтверждены разностью bucket-счётчиков. Данные и точная
PromQL с абсолютными `@ timestamp`: `results/monitoring/calibration.json`.
Клиентский эмпирический p95 TTFC = 0.227988 s, серверный эмпирический p95 =
0.225899 s. Histogram p95 = 0.390000 s: все 20 наблюдений попали в `(0.2,0.4]` s,
и линейная интерполяция даёт `0.2 + 0.95×0.2 = 0.39` s. Разница не связана с
потерей запросов: это разрешение заданных bucket-границ. Для SLO около 0.23 s
нужны более плотные границы в этом диапазоне; для учебного порога 0.6 s текущая
гистограмма применима с указанной погрешностью. Не усредняйте p95 двух Pod.

Повтор (только в отсутствие параллельной нагрузки):

```bash
KUBECONFIG="$PWD/.kubeconfig" python monitoring/runtime_check.py --calibrate
```

Сначала должны существовать четыре клиентских Pod (команда `scripts/experiments.py
clients`). Скрипт сохраняет сырые target/query/Grafana ответы, клиентские времена,
серверные журналы, начальные/конечные Histogram counters и результаты проверок.
