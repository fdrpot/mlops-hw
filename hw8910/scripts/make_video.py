"""Render a disclosed, narrated walkthrough of actual saved HW8910 evidence.

Optional rendering dependencies: Pillow and imageio-ffmpeg. Narration uses macOS
`say -v Milena`. No live terminal activity or human speech is simulated. Missing
evidence is labelled and, unless --allow-missing is used, stops final rendering.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SIZE = (1920, 1080)
FONT = Path('/System/Library/Fonts/Supplemental/Arial.ttf')
BOLD = Path('/System/Library/Fonts/Supplemental/Arial Bold.ttf')
MONO = Path('/System/Library/Fonts/Menlo.ttc')


def font(size, bold=False, mono=False):
    path = MONO if mono else BOLD if bold else FONT
    return ImageFont.truetype(str(path), size)


def read(path):
    return path.read_text(encoding='utf-8', errors='replace')


def find(directory, patterns):
    for pattern in patterns:
        matches = sorted(directory.glob(pattern))
        if matches:
            return matches[0]
    return None


def literal(path, needles=(), limit=18):
    if path is None or not path.is_file():
        return 'ДОКАЗАТЕЛЬСТВО НЕ НАЙДЕНО.\nНе считать выполненным опытом.'
    lines = read(path).splitlines()
    if needles:
        selected = [line for line in lines if any(needle in line for needle in needles)]
        if selected:
            return '\n'.join(selected[-limit:])
    return '\n'.join(lines[:limit])


def compact_json(path, fields):
    if path is None or not path.is_file():
        return literal(None)
    try:
        value = json.loads(read(path))
        if isinstance(value, dict):
            value = {key: value[key] for key in fields if key in value}
        return json.dumps(value, ensure_ascii=False, indent=2)
    except ValueError:
        return literal(path)


def evidence_text(paths, project, extractor=literal):
    parts = []
    for path in paths:
        if path:
            try:
                label = str(path.relative_to(project))
            except ValueError:
                label = str(path)
            parts.append(f'ИСТОЧНИК: {label}\n{extractor(path)}')
        else:
            parts.append(literal(None))
    return '\n\n'.join(parts)


def narration(path):
    sections = re.split(r'^##\s+\d+\s*\|\s*(.+)$', read(path), flags=re.MULTILINE)
    return [(sections[index].strip(), sections[index + 1].strip())
            for index in range(1, len(sections), 2)]


def build_slides(args):
    project, evidence = args.project, args.evidence_dir
    before = find(evidence, ['integration-before.log', '**/integration-before.log'])
    after = find(evidence, ['integration-after.log', '**/integration-after.log', 'check-all.log', '*tests-green*.log'])
    process = find(evidence, ['process/evidence.json', '**/evidence.json'])
    prefect = find(evidence, ['prefect-recovery.log', 'prefect-recover.log', 'prefect-success.log'])
    manifest = args.manifest or find(evidence, ['real/accepted.json']) or find(project, ['.real/accepted.json', '.runtime/accepted.json', 'artifacts/accepted.json'])
    report = args.release_report or find(evidence, ['real/release-report.json']) or find(project, ['.real/release-report.json', 'artifacts/release-report.json'])
    smoke = args.smoke or find(evidence, ['real-api.json', 'real-smoke.json', '*smoke*.json', '**/*smoke*.json'])
    benchmark = find(args.benchmark_dir, ['summary.json', 'summary.csv'])
    config = project / 'pyproject.toml'
    report_doc = find(project, ['docs/report.md', 'REPORT.md', 'README.md'])

    def process_excerpt(path):
        if path.suffix != '.json':
            return literal(path, ('Cached', 'Finished in state', 'Completed'), limit=4)
        value = json.loads(read(path))
        commands = value.get('commands', [])
        selected = []
        for command in commands:
            if command.get('name') in ('prepare-injected-failure', 'prepare-new-process-recovery', 'input-changed-same-path'):
                selected.append(f"{command['name']} exit={command['returncode']}\n{command.get('stdout', '').strip()}")
        return '\n\n'.join(selected) or literal(path)

    def publication_excerpt(path):
        value = json.loads(read(path))
        checks = [item for item in value.get('checks', []) if any(key in item['name'] for key in ('pointer', 'crash', 'publication-event'))]
        return '\n'.join(f"{item['name']}: {item['passed']}" for item in checks)

    def smoke_excerpt(path):
        try:
            value = json.loads(read(path))
        except ValueError:
            return literal(path, limit=25)

        def response(item):
            if isinstance(item, dict):
                if 'answer' in item and 'model_id' in item:
                    return item
                for child in item.values():
                    found = response(child)
                    if found:
                        return found
            elif isinstance(item, list):
                for child in item:
                    found = response(child)
                    if found:
                        return found
            return None

        answer = response(value)
        if answer is None:
            return literal(path, limit=25)
        if 'STUB ONLY' in str(answer.get('answer')):
            raise ValueError('The real-smoke source contains a STUB ONLY answer')
        fields = ('answer', 'request_id', 'model_id', 'runtime_sha256', 'output_tokens',
                  'generation_seconds', 'queue_seconds', 'total_seconds')
        return json.dumps({key: answer[key] for key in fields if key in answer},
                          ensure_ascii=False, indent=2)

    def benchmark_excerpt(path):
        if path.suffix != '.json':
            return literal(path)
        records = json.loads(read(path))
        panels = []
        for row in records:
            def number(key):
                value = row.get(key)
                return f'{value:.4f}' if isinstance(value, (int, float)) else str(value)
            panels.append('\n'.join([
                f"concurrency={row.get('concurrency')}  n={row.get('n')}  success={row.get('successes')}  errors={row.get('errors')}",
                f"batch_seconds={number('batch_seconds')}",
                f"successful_req/s={number('successful_requests_per_second')}",
                f"mean_client_s={number('mean_success_client_seconds')}",
                f"max_client_s={number('max_success_client_seconds')}",
                f"mean_generation_s={number('mean_generation_seconds')}",
                f"mean_queue_s={number('mean_queue_seconds')}",
                f"output_tokens={row.get('total_output_tokens')}",
                f"model_ids={row.get('model_ids')}",
            ]))
        return '\n\n'.join(panels)

    content = [
        (['Обзор сохранённых доказательств', 'Синтетическая озвучка: Milena', 'Это не живая запись терминала', 'STUB и реальная модель показаны отдельно'],
         [config], evidence_text([config], project, lambda p: literal(p, limit=16))),
        (['7 независимых причин', 'Одинаковые контракты до и после', 'Настоящие HTTP / subprocess / файлы', 'Исходные тесты сохранены'],
         [before, after], evidence_text([before, after], project,
           lambda p: literal(p, ('Ran ', 'FAILED', 'OK', 'ERROR:', 'FAIL:'), limit=8))),
        (['Подготовка → артефакт → gate → публикация', 'Сбой после подготовки', 'Новый процесс: CACHE_HIT', 'Изменённый вход: PREPARED'],
         [process] + ([prefect] if prefect else []),
         evidence_text([process] + ([prefect] if prefect else []), project, process_excerpt)),
        (['Правило качества HW67 сохранено', 'Отчёты связаны SHA-256', 'Runtime оценки совпадает с API', 'Отказ кандидата допустим'],
         [manifest, report], evidence_text([manifest, report], project,
           lambda p: compact_json(p, ('model_id', 'model_sha256', 'runtime_sha256', 'status', 'passed', 'evaluation_run_id', 'metrics')))),
        (['tempfile → fsync → os.replace', 'Отказ сохраняет старые байты', 'Попытки отдельно от публикаций', 'Повтор восстанавливает журнал'],
         [process], evidence_text([process], project, publication_excerpt)),
        (['Health: процесс жив', 'Ready: загрузка + прогрев завершены', '4xx до вычисления', 'Фактическая модель и число токенов'],
         [smoke], evidence_text([smoke], project, smoke_excerpt)),
        (['STUB ONLY: A → B → A', 'Новый указатель не меняет память', 'Перезапуск загружает принятую версию', 'Отдельный реальный smoke'],
         [after], evidence_text([after], project,
           lambda p: literal(p, ('rollback-proof', 'loaded_versions', 'test_http_loaded_version'), limit=8))),
        (['Одинаковые запросы при concurrency 1 и 2', 'max_new_tokens = 32', 'Каждая попытка сохранена', 'Очередь отделена от генерации'],
         [benchmark], evidence_text([benchmark], project, benchmark_excerpt)),
        (['Prefill: обработка входа', 'Decode: последовательные новые токены', 'KV-cache растёт с контекстом', 'TTFT / ITL / устойчивый p95 не измерялись'],
         [report_doc], evidence_text([report_doc], project, lambda p: literal(p, ('make ', 'uv ', 'benchmark', 'http', 'огранич', 'Огранич', 'видео', 'Видео'), limit=18))),
    ]
    sections = narration(args.narration)
    if len(sections) != len(content):
        raise ValueError(f'Expected {len(content)} narration sections, found {len(sections)}')
    result = []
    for index, ((title, speech), (bullets, sources, panel)) in enumerate(zip(sections, content), start=1):
        result.append({'number': index, 'title': title, 'speech': speech, 'bullets': bullets,
                       'sources': [str(path) if path else None for path in sources], 'panel': panel,
                       'missing': any(path is None or not path.is_file() for path in sources)})
    return result


def render_slide(slide, destination):
    picture = Image.new('RGB', SIZE, '#101824')
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 0, SIZE[0], 10), fill='#8fa9ff')
    draw.text((64, 38), 'MLOps 26 · HW 8+9+10 · ОБЗОР СОХРАНЁННЫХ ДОКАЗАТЕЛЬСТВ', font=font(23), fill='#a9b9d0')
    draw.text((64, 94), f"{slide['number']:02d}  {slide['title']}", font=font(47, bold=True), fill='#ffffff')
    draw.rounded_rectangle((64, 190, 696, 946), 22, fill='#1c293a')
    draw.rounded_rectangle((732, 190, 1856, 946), 22, fill='#070e17', outline='#394b66', width=2)
    y = 234
    for bullet in slide['bullets']:
        wrapped = textwrap.wrap(bullet, width=29)
        draw.text((94, y), '•', font=font(34), fill='#a8baff')
        for line in wrapped:
            draw.text((132, y), line, font=font(31), fill='#ecf0f8')
            y += 43
        y += 38
    draw.text((94, 838), 'Синтетическая озвучка: Milena', font=font(22), fill='#a9b9d0')
    draw.text((94, 874), 'Файлы справа — первичные источники', font=font(22), fill='#a9b9d0')
    lines = []
    for line in slide['panel'].splitlines():
        lines.extend(textwrap.wrap(line, width=75, replace_whitespace=False,
                                   drop_whitespace=False) or [''])
    if len(lines) > 26:
        lines = lines[:25] + ['[… фрагмент; полный файл указан выше]']
    y = 220
    for line in lines:
        color = '#9fb5ff' if line.startswith('ИСТОЧНИК:') else '#e3e8f0'
        if 'НЕ НАЙДЕНО' in line:
            color = '#ffb790'
        draw.text((762, y), line, font=font(23, mono=True), fill=color)
        y += 27
    draw.text((64, 992), 'Не живая запись терминала. Демонстрация STUB не заменяет реальный eval.',
              font=font(24), fill='#a9b9d0')
    draw.text((1690, 992), f"{slide['number']} / 9", font=font(24), fill='#a9b9d0')
    picture.save(destination)


def run(command):
    subprocess.run([str(part) for part in command], check=True)


def render_video(args, slides, work):
    import imageio_ffmpeg
    ffmpeg = Path(args.ffmpeg) if args.ffmpeg else Path(imageio_ffmpeg.get_ffmpeg_exe())
    segments = []
    for slide in slides:
        stem = work / f"slide-{slide['number']:02d}"
        text = stem.with_suffix('.txt')
        audio = stem.with_suffix('.aiff')
        segment = stem.with_suffix('.mp4')
        text.write_text(slide['speech'], encoding='utf-8')
        run(['/usr/bin/say', '-v', args.voice, '-r', str(args.rate), '-f', text, '-o', audio])
        info = subprocess.run(['/usr/bin/afinfo', str(audio)], check=True, capture_output=True, text=True).stdout
        match = re.search(r'estimated duration:\s*([0-9.]+)\s+sec', info)
        duration = float(match.group(1)) if match else 0
        if duration <= 0:
            raise RuntimeError('macOS say produced no audio. Run video rendering with permission to access the speech service.')
        if duration > args.slide_seconds - 0.5:
            raise ValueError(f"Slide {slide['number']} narration is {duration:.1f}s; increase --rate so speech is not cut off")
        # Audio is padded to the full slide, never sped up or misrepresented.
        run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-loop', '1',
             '-framerate', '1', '-i', stem.with_suffix('.png'), '-i', audio,
             '-t', str(args.slide_seconds), '-vf', 'fps=12,format=yuv420p',
             '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'stillimage', '-crf', '23',
             '-af', 'apad', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', segment])
        segments.append(segment)
    concat = work / 'segments.txt'
    # All segment names are generated locally and contain no user-provided quotes.
    concat.write_text(''.join(f"file '{path.name}'\n" for path in segments))
    run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0',
         '-i', concat, '-c', 'copy', '-movflags', '+faststart', args.output])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--evidence-dir', type=Path)
    parser.add_argument('--benchmark-dir', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--release-report', type=Path)
    parser.add_argument('--smoke', type=Path)
    parser.add_argument('--narration', type=Path, default=Path(__file__).with_name('narration.md'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--slide-seconds', type=int, default=40)
    parser.add_argument('--voice', default='Milena')
    parser.add_argument('--rate', type=int, default=165)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--preview-only', action='store_true')
    parser.add_argument('--allow-missing', action='store_true', help='explicitly render missing-evidence labels')
    args = parser.parse_args()
    args.project = args.project.resolve()
    args.evidence_dir = (args.evidence_dir or args.project / 'evidence').resolve()
    args.benchmark_dir = (args.benchmark_dir or args.evidence_dir / 'benchmark').resolve()
    args.output = (args.output or args.project / '.submission/demo.mp4').resolve()
    if not 300 <= args.slide_seconds * 9 <= 480:
        parser.error('total duration must be 5–8 minutes')
    if args.rate < 100:
        parser.error('speech rate must be at least 100 words/minute')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    work = args.output.parent / (args.output.stem + '-assets')
    work.mkdir(parents=True, exist_ok=True)
    slides = build_slides(args)
    for slide in slides:
        render_slide(slide, work / f"slide-{slide['number']:02d}.png")
    source_hashes = {}
    for slide in slides:
        for source in slide['sources']:
            if source and Path(source).is_file():
                source_hashes[source] = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    manifest = {'disclosure': 'Rendered saved-evidence walkthrough with synthetic Milena narration; not live terminal recording or human voice',
                'duration_seconds': len(slides) * args.slide_seconds,
                'narration_sha256': hashlib.sha256(args.narration.read_bytes()).hexdigest(),
                'source_sha256': source_hashes, 'voice': args.voice, 'words_per_minute': args.rate,
                'slides': slides}
    (work / 'video-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    missing = [slide['number'] for slide in slides if slide['missing']]
    if missing and not args.allow_missing and not args.preview_only:
        parser.error(f'missing evidence on slides {missing}; inspect preview and pass actual paths')
    if not args.preview_only:
        render_video(args, slides, work)
    print(json.dumps({'output': str(args.output), 'assets': str(work),
                      'preview_only': args.preview_only, 'missing_slides': missing}, ensure_ascii=False))


if __name__ == '__main__':
    main()
