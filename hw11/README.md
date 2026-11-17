# ДЗ 11 — Kubernetes под нагрузкой

Продолжение сервиса [ДЗ 8–10](../hw8910/README.md): FastAPI, проверка SHA256
загруженного артефакта, готовность после прогрева, ограничение очереди, stateless
запросы. Добавлены потоковый NDJSON, CPU-имитатор, локальный LRU-кеш, метрики,
Helm, эксплуатационные опыты. Каждый ответ явно содержит `backend_mode=simulated`.
Веса модели не нужны. Все задержки названы **TTFC / inter-chunk latency**.

[Отчёт и четыре графика](report.html) · [Условие](assignment.html) ·
[Сырые запросы](results/requests.jsonl) · [PromQL](monitoring/promql.md) ·
[Экспорт Grafana](monitoring/dashboard.json).

## Запуск

Проверено на macOS arm64, Colima/VZ. Нужны Colima, Docker CLI, Python 3.12 и uv.
Инструменты устанавливаются локально в `.tools` (kind 0.30.0, kubectl 1.34.0,
Helm 3.19.0); Kubernetes node image закреплён digest в `kind.yaml`.

```bash
cd hw11
./scripts/install-tools.sh
uv sync --locked --group report
make up
export KUBECONFIG="$PWD/.kubeconfig"
export DOCKER_CONTEXT=colima-hw11
export PATH="$PWD/.tools/bin:$PATH"
make check
make a
make b  # 12 прогонов по 120 секунд, ещё время на 20-секундный прогрев
make c
make check-k8s
python scripts/environment.py
make report
```

`make up` создаёт отдельный профиль Colima `hw11` с 4 CPU, 6 GiB RAM и диском
до 16 GiB. Другие контейнеры не останавливаются. `DOCKER_CONTEXT` и `KUBECONFIG`
ограничивают команды этим стендом. Исходный свободный диск и фактические размеры
сохранены в `results/*disk*.txt` и `results/environment.json`.

Docker system df учитывает образ приложения примерно как 239 MB (содержимое по
image inspect — 51 MB); node image — около 1.5 GB. Верхняя граница диска VM — 16 GiB. Prometheus хранит не более 1 часа/128 MB метрик, его `emptyDir` ограничен
256 MiB, Grafana — 128 MiB. PVC нет. Фактически используемый размер измеряется,
а максимальный размер VM не приравнивается к занятому месту.

Мониторинг доступен локально:

```bash
kubectl -n hw11 port-forward service/hw11-grafana 3000:3000
# http://localhost:3000 — анонимный Viewer, учебный dashboard HW11
kubectl -n hw11 port-forward service/hw11-prometheus 9090:9090
# http://localhost:9090
```

Для ручного потокового запроса откройте `kubectl -n hw11 port-forward service/hw11-normal 8000:8000`
и выполните в другом терминале:

```bash
curl -N http://localhost:8000/generate -H 'Content-Type: application/json' \
  -d '{"request_id":"manual-1","session_id":"dialog-1","prompt":"continue","history":[{"role":"user","content":"CONTROL_ALPHA"}],"max_chunks":3}'
```

## Повторяемость опытов

Четыре client Pod имеют разные IP; оба Service выбирают одни и те же API Pod.
`hw11-normal`: `sessionAffinity: None`; `hw11-sticky`: `ClientIP`, 10800 секунд.
HTTP keep-alive выключен, у каждого запроса отдельное соединение, скрытых повторов нет.
IP источника виден в структурированных серверных журналах.

B: uniform/hot80 × sticky/normal × три повтора. Каждый измерительный интервал
120 секунд, 4 запроса/секунду, 3 части, интервал частей 0.15 секунды. Перед каждым
прогоном реплики заменяются: кеш пуст, backend прогревается 20 секунд. LRU вмещает
16 префиксов; стоимость hit/miss 0.04/0.20 секунды; CPU 0.02 секунды на часть;
одновременно выполняется один запрос на Pod, очередь 64, тайм-аут 60 секунд.
Расписания, payload и ограничения одинаковы для пары политик. Уникальное тело
имеет общий префикс из восьми возможных ключей. Это имитация кеша, не KV engine.

A сохраняет исходную неисправность `model.probeMode=broken` и исправление `fixed`.
Штатный rollout A→B пересекается с 40-секундным потоком, затем проверяется неготовая
B, возврат к A и SIGKILL процесса. Эксперимент с отказом отделён от штатного rollout.
C использует настоящее CPU-вычисление, resource metrics API и HPA 2→3→2; отдельно
показывает горячего клиента и управляемые срабатывания двух Prometheus alerts.

Генератор использует монотонные часы; сохраняет плановую и фактическую отправку,
первую часть, завершение, статус, Pod, версию, части, очередь и интервалы. Итоговый
`done` обязателен: HTTP 200 без него — оборванный поток. Отказы, тайм-ауты и сетевые
ошибки учитываются отдельно. Успешные перцентили всегда сопровождаются размером выборки.

Артефакты `artifacts/model-A.json`/`model-B.json` включены в образ и чарт. Приложение
сверяет содержимое с закреплённым SHA256, а не только с объявленной версией; ошибка
оставляет Pod неготовым. Диалог целиком передаётся в `history`, поэтому замена Pod
теряет лишь ускоряющий кеш. Собственные Kubernetes-проверки делают реальные HTTP
запросы и заменяют Pod только этого стенда.

Сырые файлы повторных запусков дописываются. Для полностью нового измерительного
набора сначала переместите `results` в отдельную папку; сохранённые результаты
сдачи не перезаписывайте без копии. Нагрузочные опыты нельзя выполнять параллельно.

## Удаление

```bash
make stop    # освободить CPU/RAM, сохранить стенд
make delete  # удалить только kind hw11 и Colima profile hw11 со всеми его данными
# локальные .tools/ и .venv/ при необходимости удалить отдельно
```

Текущий стенд после снятия доказательств можно остановить, а результаты остаются
в Git. Не используйте `docker system prune`: к этой работе относятся только
профиль `hw11`, namespace `hw11` и явно перечисленные ресурсы metrics-server.
