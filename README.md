# MLOps — домашние задания

Каждое задание — самостоятельный проект со своими зависимостями,
`params.yaml`, исходниками, проверками и отчётами.

| Папка | Задание | Отчёт |
|---|---|---|
| [hw1](hw1/README.md) | Окружение, генерация и бенчмарк | [hardware.md](hw1/docs/hardware.md) |
| [hw2](hw2/README.md) | Параметры модели, активации, LoRA и память | [anatomy.md](hw2/docs/anatomy.md) |
| [hw3](hw3/README.md) | Версионирование и качество данных | [datasheet.md](hw3/docs/datasheet.md) |
| [hw4](hw4/README.md) | Токенизация и подготовка обучения | [tokenize_report.md](hw4/docs/tokenize_report.md) |
| [hw5](hw5/README.md) | Обучение и сравнение | [compare.md](hw5/docs/compare.md) |
| [hw67](hw67/README.md) | MLflow и полный gate | [conclusion.md](hw67/docs/conclusion.md) |
| [hw8910](hw8910/README.md) | Prefect, безопасный выпуск и FastAPI | [report.md](hw8910/docs/report.md) |

## ДЗ 1

```bash
cd hw1
uv sync --locked
make generate
make bench
make check
```

## ДЗ 2

```bash
cd hw2
uv sync --locked
make inspect
make check
```
