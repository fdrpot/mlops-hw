"""Строгая оценка меток и допуск без ухудшения качества."""
import json
import math


def _index(items):
    if not isinstance(items, list) or not items:
        raise ValueError('Нужен непустой список')
    result = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']:
            raise ValueError('Нужен непустой строковый id')
        if item['id'] in result:
            raise ValueError('Повторяющийся id: ' + item['id'])
        result[item['id']] = item
    return result


def _names(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(s, str) and bool(s) for s in value)
            and len(value) == len(set(value)))


def _counts(items):
    n = len(items)
    correct = sum(r['correct'] for r in items)
    valid = sum(r['valid'] for r in items)
    return dict(n=n, correct=correct, valid=valid,
                errors=sum(r['error'] is not None for r in items),
                accuracy=correct / n, format_rate=valid / n)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Повтор JSON-ключа')
        result[key] = value
    return result


def score(rows, outputs):
    """Верните overall, slices и per_item. Не удаляйте плохие ответы из знаменателя.

    Строгий ответ: JSON с единственным строковым полем answer из choices.
    outputs: [{id, raw, error, latency_seconds}, ...].
    В overall и каждом срезе: n, correct, valid, errors, accuracy, format_rate.
    В per_item: id, raw, parsed, valid, correct, error, slices.
    Несовпадающие/дублирующиеся id и пустой набор должны вызвать ValueError.
    """
    tasks, answers = _index(rows), _index(outputs)
    if tasks.keys() != answers.keys():
        raise ValueError('ID заданий и ответов различаются')
    items = []
    for row in rows:
        if (not _names(row.get('choices')) or row.get('expected') not in row['choices']
                or not _names(row.get('slices'))):
            raise ValueError('Некорректные choices, expected или slices')
        output = answers[row['id']]
        raw, error = output.get('raw'), output.get('error')
        parsed, valid = None, False
        if error is None and isinstance(raw, str):
            try:
                value = json.loads(raw, object_pairs_hook=_unique_object)
                valid = (isinstance(value, dict) and set(value) == {'answer'}
                         and isinstance(value['answer'], str) and value['answer'] in row['choices'])
                if valid:
                    parsed = value['answer']
            except (ValueError, RecursionError):
                pass
        items.append(dict(id=row['id'], raw=raw, parsed=parsed, valid=valid,
                          correct=valid and parsed == row['expected'], error=error,
                          slices=list(row['slices'])))
    slices = {s: _counts([r for r in items if s in r['slices']])
              for s in sorted({s for r in items for s in r['slices']})}
    return dict(overall=_counts(items), slices=slices, per_item=items)


def _check_metrics(actual, expected):
    if not isinstance(actual, dict):
        raise ValueError('Отсутствуют метрики')
    for key in ('n', 'correct', 'valid', 'errors'):
        if type(actual.get(key)) is not int or actual[key] != expected[key]:
            raise ValueError('Метрика не соответствует counts: ' + key)
    for key in ('accuracy', 'format_rate'):
        value = actual.get(key)
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not math.isclose(value, expected[key], rel_tol=0, abs_tol=1e-12)):
            raise ValueError('Некорректная доля: ' + key)


def _check_report(report):
    if not isinstance(report, dict):
        raise ValueError('Отсутствует отчёт')
    items = _index(report.get('per_item'))
    for item in items.values():
        if (type(item.get('valid')) is not bool or type(item.get('correct')) is not bool
                or 'error' not in item or not _names(item.get('slices'))
                or (item['correct'] and not item['valid'])
                or (item['error'] is not None and item['valid'])):
            raise ValueError('Некорректный per_item')
    _check_metrics(report.get('overall'), _counts(list(items.values())))
    names = {s for item in items.values() for s in item['slices']}
    if not isinstance(report.get('slices'), dict) or report['slices'].keys() != names:
        raise ValueError('Отсутствует или изменён срез')
    for name in names:
        _check_metrics(report['slices'][name], _counts([r for r in items.values() if name in r['slices']]))
    return {key: set(item['slices']) for key, item in items.items()}


def quality_gate(baseline, candidate, critical_slices):
    """Верните {passed: True, ...} или вызовите ValueError с причиной отказа.

    Проверьте совместимость протокола/набора/baseline, метрики и counts,
    все id и срезы, общую accuracy/format_rate, критические срезы и behavior.
    Missing, NaN, пустой набор и ошибки генерации означают отказ.
    Пороги задаются до измерения; учебный допуск регрессии равен нулю.
    """
    try:
        for key in ('protocol_sha256', 'golden_sha256', 'baseline_spec_sha256'):
            if (not isinstance(baseline.get(key), str) or not baseline[key]
                    or baseline[key] != candidate.get(key)):
                raise ValueError('Несовместимость: ' + key)
        for report in (baseline, candidate):
            model = report.get('model')
            if (report.get('status') != 'complete' or not isinstance(model, dict)
                    or not isinstance(model.get('kind'), str) or not model['kind']):
                raise ValueError('Неполный отчёт или отсутствует модель')
            if model['kind'] == 'base' and (not model.get('name') or len(model.get('revision', '')) != 40):
                raise ValueError('База не закреплена')
            if model['kind'] == 'adapter' and any(not model.get(k) for k in ('name', 'version', 'run_id', 'uri')):
                raise ValueError('Версия адаптера не закреплена')
        a, b = _check_report(baseline), _check_report(candidate)
        if a != b:
            raise ValueError('Различаются id или состав срезов')
        if baseline['overall']['n'] < 50 or len(baseline['slices']) < 3:
            raise ValueError('Нужно минимум 50 вопросов и три среза')
        if not _names(critical_slices) or not set(critical_slices) <= baseline['slices'].keys():
            raise ValueError('Не заданы существующие критические срезы')
        for report in (baseline, candidate):
            if report['overall']['errors']:
                raise ValueError('Есть ошибки генерации')
        for key in ('accuracy', 'format_rate'):
            if candidate['overall'][key] < baseline['overall'][key]:
                raise ValueError('Общая регрессия: ' + key)
        for name in critical_slices:
            if candidate['slices'][name]['accuracy'] < baseline['slices'][name]['accuracy']:
                raise ValueError('Регрессия критического среза: ' + name)
        ba, bb = (_check_report(r.get('behavioral')) for r in (baseline, candidate))
        if ba != bb or len(bb) < 3:
            raise ValueError('Неполные или несовместимые behavioral-кейсы')
        if any(r['behavioral']['overall']['errors'] for r in (baseline, candidate)):
            raise ValueError('Ошибка генерации behavioral')
        behavior = candidate['behavioral']['overall']
        if behavior['correct'] != behavior['n']:
            raise ValueError('Не пройдены обязательные behavioral-кейсы')
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError('Неполный или некорректный отчёт') from exc
    return dict(passed=True, protocol_sha256=candidate['protocol_sha256'],
                golden_sha256=candidate['golden_sha256'], critical_slices=critical_slices,
                baseline_accuracy=baseline['overall']['accuracy'],
                candidate_accuracy=candidate['overall']['accuracy'],
                behavioral_passed=behavior['correct'])
