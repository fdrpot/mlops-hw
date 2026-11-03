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
make check-original ORIGINAL_OUTPUT=/tmp/hw8910-original
# ожидаются падения восьми регрессионных тестов на исходном прототипе
```

Сохранённый исходный красный лог:
[`integration-before.log`](evidence/integration-before.log); зелёный:
[`check-all.log`](evidence/check-all.log). Отпечаток `tests/test_release.py` SHA256:
`03632e58fa10f8742a205ae04ecba956f7a7cd0a0cfb90fc7fa9d32d6f2c1d7e`.

## Процесс и восстановление со STUB

Каждый независимый опыт получает новый workspace; `init` не перезаписывает старый.

```bash
make init WORKSPACE=.lab-demo
make flow-fail WORKSPACE=.lab-demo  # ожидаемый сбой после подготовки
make flow WORKSPACE=.lab-demo       # подготовка из кеша
make flow WORKSPACE=.lab-demo       # повтор без второго события выпуска
make input-change WORKSPACE=.lab-demo
make flow WORKSPACE=.lab-demo       # изменённый вход готовится заново
make publications WORKSPACE=.lab-demo

make init WORKSPACE=.lab-crash-demo
make flow-crash WORKSPACE=.lab-crash-demo  # сбой между указателем и журналом
make flow WORKSPACE=.lab-crash-demo
make publications WORKSPACE=.lab-crash-demo  # одно событие после восстановления
```

Полная автоматизация опытов с кодами выхода и SHA256:

```bash
make process-evidence PROCESS_OUTPUT=evidence/process-new
make prefect-evidence WORKSPACE=.lab-prefect-new PROCESS_OUTPUT=evidence/prefect-new
make check-rollback
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
make accepted-hash REAL_WORKSPACE=.real
make gate REAL_WORKSPACE=.real
# ожидается отказ behavioral gate; accepted.json остаётся baseline
make accepted-hash REAL_WORKSPACE=.real
make serve-real PORT=8093 WARMUP_SECONDS=20 DEBUG=1
```

Генерация использует тот же `RealBackend`, что полная оценка. Настройки читаются
из принятого manifest, а не из локального train config. На этой машине измерены
MPS и bfloat16. Для CPU выполните
`make real-evaluate DEVICE=cpu REAL_WORKSPACE=.real-cpu`: нужна новая оценка,
править runtime/хеш в старом отчёте нельзя.

Для преподавателя без Docker-volumes предусмотрен запуск сохранённой baseline:

```bash
make install-model
make restore-baseline REAL_WORKSPACE=.real-review
# скачивает публичную точную revision; повторно сверяет каждый закреплённый файл
make serve-real REAL_WORKSPACE=.real-review PORT=8093 WARMUP_SECONDS=20 DEBUG=1
```

Требуется Apple MPS для сохранённого runtime; CPU требует новой оценки. Вариант
`make restore-baseline REAL_WORKSPACE=.real-review SNAPSHOT=/path/to/pinned/snapshot`
позволяет восстановить веса офлайн.
Адаптер доступен в локальном MLflow: run `041aa4e127c04f228eb8dc15b6025ca0`,
artifact `model/artifacts/adapter`, Registry `models:/my-qwen-adapter/1`.
Он не включён в Git/сдачу; все исходные eval/ответы уже приложены без весов.
Публичная baseline восстанавливается независимо от доступности этого сервера.

Проверки HTTP из другого терминала (запрос всегда с лимитом 32 как в eval):

```bash
make health PORT=8093
make ready PORT=8093  # 503 до конца прогрева; затем 200
make debug PORT=8093
make invalid-request PORT=8093  # 4xx без вызова генерации
make generate PORT=8093         # после успешной ready
make bench PORT=8093 BENCH_OUTPUT=evidence/benchmark-new
```

Один вычисляющий backend, очередь до 60 секунд, максимум 8192 символа тела prompt
и 2048 токенов с системной инструкцией; тело HTTP до 64 KiB. Реальный API допускает
ровно оценённый лимит 32; STUB допускает 1–32. До прогрева `/ready` и генерация
возвращают 503. `/debug` у реального сервиса включается только с `DEBUG=1`
в `make serve-real` для опытов.

Весь реальный опыт — отказ, прогрев, неверные запросы, 8+8 и повторная загрузка:

```bash
make real-evidence REAL_WORKSPACE=.real BENCH_OUTPUT=evidence/real-experience-new
```

Веса, кеши, окружения и Docker-volumes исключены из Git; небольшой архив сдачи
можно собрать из отслеживаемых файлов.
