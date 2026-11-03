# ДЗ 8+9+10 — проверяемый выпуск и сервис модели

Семь дефектов исходного прототипа исправлены. Процесс оформлен в Prefect,
API — FastAPI. Исходник: `d6c70a7`, исправления: `270e274`; публичные тесты не изменены.

**Реальный результат:** кандидат из HW67 отклонён прежним behavioral gate.
Сервис загружает прежнюю baseline `Qwen/Qwen3-1.7B`, revision
`70d244cc86ccca08cf5af4e1e306ecf908b1ad5e`. Для baseline не заявляется новый PASS:
её происхождение явно записано как `retained_baseline`.

- [Отчёт и измерения](docs/report.md)
- [Семь причин и регрессии](defects.csv)
- [Полный реальный release-report](evidence/real/release-report.json)
- [Реальные ответы baseline/candidate](evidence/real/source/)
- [HTTP smoke и перезапуск](evidence/real-smoke.json)
- [Запросы замера](evidence/benchmark/requests.jsonl), [таблица](evidence/benchmark/summary.csv)
- [Видео, 6 минут](.submission/demo.mp4) — локальный файл, исключён из Git.

## Быстрые проверки без весов

Нужны Python 3.12 и `uv`; все зависимости закреплены в `uv.lock`.
Лёгкое окружение `.venv-check` отделено от `.venv` с моделью.

```bash
cd hw8910
make check-light
make intentional-failure  # ожидаемый ненулевой exit; исходники не меняются
```

`make check` компилирует код, запускает 13 публичных и 18 собственных тестов.
Они используют настоящие HTTP, subprocess и файловую систему; веса не загружаются.
Локальный CI засчитывается заданием. `ci.example.yml` — шаблон; удалённого run и
защиты merge обязательной проверкой здесь не заявляется.

Исходные восемь интеграционных регрессий можно повторить на старом коммите:

```bash
mkdir -p /tmp/hw8910-original
git archive d6c70a7 hw8910 | tar -x -C /tmp/hw8910-original
HW8910_TARGET_ROOT=/tmp/hw8910-original/hw8910 \
  .venv-check/bin/python -m unittest discover -s integration -p test_integration.py -v
```

Сохранённый исходный красный лог:
[`integration-before.log`](evidence/integration-before.log); зелёный:
[`check-all.log`](evidence/check-all.log). Отпечаток `tests/test_release.py` SHA256:
`03632e58fa10f8742a205ae04ecba956f7a7cd0a0cfb90fc7fa9d32d6f2c1d7e`.

## Процесс и восстановление со STUB

Каждый независимый опыт получает новый workspace; `init` не перезаписывает старый.

```bash
make init WORKSPACE=.lab-demo
.venv-check/bin/python flow.py --workspace .lab-demo --fail-after-prepare
.venv-check/bin/python flow.py --workspace .lab-demo
.venv-check/bin/python flow.py --workspace .lab-demo
```

Полная автоматизация опытов с кодами выхода и SHA256:

```bash
.venv-check/bin/python scripts/evidence.py --output evidence/process-new
.venv-check/bin/python scripts/prefect_evidence.py \
  --workspace .lab-prefect-new --output evidence/prefect-new
```

Сохранены [25/25 проверок процесса](evidence/process/evidence.json) и
[реальные состояния Prefect](evidence/prefect/summary.json), включая `Cached`
между процессами. STUB ONLY проверяет управление выпуском, а не качество модели.

## Настоящая модель

На исходной машине уже есть веса HF и локальный MLflow HW67. Новый sweep не нужен:

```bash
cd ../hw67
make up
cd ../hw8910
make install-model
make real-evaluate
.venv/bin/python flow.py --workspace .real \
  --manifest manifest-candidate.json --report release-report.json
# ожидается отказ behavioral gate; accepted.json остаётся baseline
make serve-real PORT=8091
```

Генерация использует тот же `RealBackend`, что полная оценка. Настройки читаются
из принятого manifest, а не из локального train config. На этой машине измерены
MPS и bfloat16. Для CPU задайте `build_release.py --device cpu` и новый workspace:
нужна новая оценка, править runtime/хеш в старом отчёте нельзя.

Для преподавателя без Docker-volumes предусмотрен запуск сохранённой baseline:

```bash
make install-model
.venv/bin/python scripts/restore_baseline.py --workspace .real-review
# скачивает публичную точную revision; повторно сверяет каждый закреплённый файл
.venv/bin/python service.py --workspace .real-review --backend real \
  --port 8091 --warmup-seconds 0
```

Требуется Apple MPS для сохранённого runtime; CPU требует новой оценки. Вариант
`--snapshot /path/to/pinned/snapshot` позволяет восстановить веса офлайн.
Адаптер доступен в локальном MLflow: run `041aa4e127c04f228eb8dc15b6025ca0`,
artifact `model/artifacts/adapter`, Registry `models:/my-qwen-adapter/1`.
Он не включён в Git/сдачу; все исходные eval/ответы уже приложены без весов.
Публичная baseline восстанавливается независимо от доступности этого сервера.

Пример запроса (всегда ровно 32 новых токена как в eval):

```bash
curl http://127.0.0.1:8091/ready
curl -H 'Content-Type: application/json' http://127.0.0.1:8091/generate \
  -d '{"prompt":"Choose the command listing files: A) ls B) rm","max_new_tokens":32}'
make bench BENCH_OUTPUT=evidence/benchmark-new
```

Один вычисляющий backend, очередь до 60 секунд, максимум 8192 символа тела prompt
и 2048 токенов с системной инструкцией; тело HTTP до 64 KiB. Реальный API допускает
ровно оценённый лимит 32; STUB допускает 1–32. До прогрева `/ready` и генерация
возвращают 503. `/debug` у реального сервиса включается только `--debug` для опытов.

Весь реальный опыт — отказ, прогрев, неверные запросы, 8+8 и повторная загрузка:

```bash
.venv/bin/python scripts/real_evidence.py --workspace .real \
  --benchmark-output evidence/benchmark-new
```

Сборка шестиминутного обзора доказательств с синтетической озвучкой:

```bash
uv sync --frozen --extra model --extra video
.venv/bin/python scripts/make_video.py --output .submission/demo.mp4
```

Видео — размеченный обзор настоящих сохранённых журналов, не живая запись
терминала и не голос студента. Веса, кеши, окружения и Docker-volumes исключены
из Git; небольшой архив сдачи можно собрать из `git archive` и добавить видео.
