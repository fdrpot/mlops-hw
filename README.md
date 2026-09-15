# MLOps — домашние задания

Каждое задание — самостоятельный проект со своими зависимостями,
`params.yaml`, исходниками, проверками и отчётами.

| Папка | Задание | Отчёт |
|---|---|---|
| [hw1](hw1/README.md) | Окружение, генерация и бенчмарк | [hardware.md](hw1/docs/hardware.md) |
| [hw2](hw2/README.md) | Параметры модели, активации, LoRA и память | [anatomy.md](hw2/docs/anatomy.md) |

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
