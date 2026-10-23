"""Свои вопросы и полный индекс train; данные из ДЗ 4 остаются неизменными."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HW4 = ROOT.parent / 'hw4'

# В каждой тройке первый вариант верен. Позиция эталона меняется до оценки.
# Темы целиком отсутствуют в train, включая неиспользованные sweep строки.
QUESTIONS = {
    ('modern_unix/fd', 'file_search', 'https://github.com/sharkdp/fd#how-to-use'): [
        ('In fd, include hidden entries but keep ignore rules. Which flag?', ['--hidden', '--no-ignore', '--type directory']),
        ('In fd, disable ignore-file filtering without enabling hidden entries. Which flag?', ['--no-ignore', '--hidden', '--follow']),
        ('With fd, select only files with extension parquet. Which command?', ['fd -e parquet', 'fd -t parquet', 'fd --size parquet']),
        ('With fd, return only directories matching cache. Which command?', ['fd -t d cache', 'fd -t f cache', 'fd -e cache']),
        ('With fd, cap recursive traversal at depth two. Which flag?', ['--max-depth 2', '--size 2', '--threads 2']),
        ('In fd, treat the pattern as a shell glob. Which flag?', ['--glob', '--fixed-strings', '--full-path']),
        ('In fd, force case-sensitive pattern matching. Which flag?', ['--case-sensitive', '--ignore-case', '--hidden']),
        ('In fd, match the full path instead of only the filename. Which flag?', ['--full-path', '--absolute-path', '--list-details']),
        ('In fd, follow directory symlinks while searching. Which flag?', ['--follow', '--hidden', '--no-ignore']),
        ('In fd, exclude paths matching a glob vendor. Which flag?', ['--exclude vendor', '--extension vendor', '--type vendor']),
    ],
    ('modern_unix/yq', 'structured_data', 'https://mikefarah.gitbook.io/yq'): [
        ('Using Mike Farah yq v4, read replicas under spec. Which expression?', ['.spec.replicas', '.spec | keys', 'del(.spec.replicas)']),
        ('Using Mike Farah yq v4, write a change back to the input file. Which flag?', ['-i', '-n', '-o=json']),
        ('Using Mike Farah yq v4, assign integer 4 to replicas under spec. Which expression?', ['.spec.replicas = 4', '.spec.replicas == 4', '.spec.replicas | length']),
        ('Using Mike Farah yq v4, remove the debug key. Which expression?', ['del(.debug)', '.debug = true', 'keys']),
        ('Using Mike Farah yq v4, select the first element of items. Which expression?', ['.items[0]', '.items[1]', '.items | length']),
        ('Using Mike Farah yq v4, count elements of items. Which expression?', ['.items | length', '.items[]', '.items | keys']),
        ('Using Mike Farah yq v4, retain only objects with enabled equal to true. Which expression?', ['.[] | select(.enabled == true)', '.[] | del(.enabled)', '.[] | .enabled = false']),
        ('Using Mike Farah yq v4, read environment variable PORT as a string. Which expression?', ['strenv(PORT)', 'env(PORT)', '.PORT']),
        ('Using Mike Farah yq v4, emit JSON rather than YAML. Which flag?', ['-o=json', '-i', '-n']),
        ('Using Mike Farah yq v4, append api to an existing services array. Which expression?', ['.services += ["api"]', '.services = "api"', 'del(.services)']),
    ],
    ('systems/lsblk', 'storage', 'https://man7.org/linux/man-pages/man8/lsblk.8.html'): [
        ('For lsblk, choose JSON output. Which flag?', ['--json', '--pairs', '--raw']),
        ('For lsblk, report sizes as bytes. Which flag?', ['--bytes', '--fs', '--ascii']),
        ('For lsblk, select exactly NAME and SIZE columns. Which flag?', ['--output NAME,SIZE', '--sort NAME', '--exclude 8']),
        ('For lsblk, suppress the header row. Which flag?', ['--noheadings', '--nodeps', '--list']),
        ('For lsblk, request a flat list rather than a tree. Which flag?', ['--list', '--tree', '--inverse']),
        ('For lsblk, show full device paths. Which flag?', ['--paths', '--pairs', '--perms']),
        ('For lsblk, show filesystem-related information. Which flag?', ['--fs', '--discard', '--topology']),
        ('For lsblk /dev/sda, suppress child and holder devices. Which flag?', ['--nodeps', '--noheadings', '--all']),
        ('For lsblk, output key=value pairs. Which flag?', ['--pairs', '--json', '--bytes']),
        ('For lsblk, sort the output by SIZE. Which flag?', ['--sort SIZE', '--output SIZE', '--exclude SIZE']),
    ],
    ('network/ethtool', 'network', 'https://man7.org/linux/man-pages/man8/ethtool.8.html'): [
        ('Using ethtool, query driver and firmware information for eno1. Which command?', ['ethtool -i eno1', 'ethtool -S eno1', 'ethtool -k eno1']),
        ('Using ethtool, query driver-specific counters for eno1. Which command?', ['ethtool -S eno1', 'ethtool -i eno1', 'ethtool -g eno1']),
        ('Using ethtool, display offload settings for eno1. Which command?', ['ethtool -k eno1', 'ethtool -c eno1', 'ethtool -a eno1']),
        ('Using ethtool, display ring parameters for eno1. Which command?', ['ethtool -g eno1', 'ethtool -i eno1', 'ethtool -k eno1']),
        ('Using ethtool, display interrupt-coalescing settings for eno1. Which command?', ['ethtool -c eno1', 'ethtool -g eno1', 'ethtool -l eno1']),
        ('Using ethtool, display pause-frame settings for eno1. Which command?', ['ethtool -a eno1', 'ethtool -i eno1', 'ethtool -S eno1']),
        ('Using ethtool, display channel counts for eno1. Which command?', ['ethtool -l eno1', 'ethtool -c eno1', 'ethtool -g eno1']),
        ('Using ethtool, display the permanent hardware address of eno1. Which command?', ['ethtool -P eno1', 'ethtool -i eno1', 'ethtool -a eno1']),
        ('Using ethtool, display default link settings for eno1. Which command?', ['ethtool eno1', 'ethtool -S eno1', 'ethtool -g eno1']),
        ('Using ethtool, display supported timestamping capabilities for eno1. Which command?', ['ethtool -T eno1', 'ethtool -P eno1', 'ethtool -k eno1']),
    ],
    ('modern_unix/nvim', 'editor', 'https://raw.githubusercontent.com/neovim/neovim/master/runtime/doc/quickref.txt'): [
        ('In Neovim normal mode, move to the first line. Which command?', ['gg', 'G', '$']),
        ('In Neovim normal mode, move to the last line. Which command?', ['G', 'gg', '0']),
        ('In Neovim normal mode, move to the end of the current line. Which command?', ['$', '0', 'gg']),
        ('In Neovim normal mode, delete the current line. Which command?', ['dd', 'yy', 'p']),
        ('In Neovim normal mode, copy the current line. Which command?', ['yy', 'dd', 'u']),
        ('In Neovim normal mode, undo the most recent change. Which command?', ['u', 'p', 'dd']),
        ('In Neovim normal mode, redo an undone change. Which key?', ['CTRL-R', 'CTRL-F', 'CTRL-B']),
        ('In Neovim normal mode, paste after the cursor. Which command?', ['p', 'P', 'yy']),
        ('In Neovim, save the current file without quitting. Which Ex command?', [':w', ':q!', ':e!']),
        ('In Neovim, quit the current window discarding unsaved changes. Which Ex command?', [':q!', ':w', ':wq']),
    ],
}


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))


def main():
    train = read_rows(ROOT / 'data/source/train.jsonl')
    import torch
    tokenized = torch.load(ROOT / 'data/tokenized/train.pt', weights_only=True)
    if [r['id'] for r in train] != [r['id'] for r in tokenized['examples']]:
        raise ValueError('Исходные train-вопросы не соответствуют тензорам ДЗ 4')
    # Индекс включает ВСЕ вопросы, а не только первые 32 из бюджета sweep.
    index = [dict(id=r['id'], group_id=r['topic'],
                  input=next(m['content'] for m in r['messages'] if m['role'] == 'user')) for r in train]
    golden = []
    for (group, slice_name, source), questions in QUESTIONS.items():
        assert group not in {r['group_id'] for r in index}, group
        for question, options in questions:
            position = len(golden) % 3
            ordered = options[-position:] + options[:-position] if position else options
            text = question + '\n' + '\n'.join(f'{label}. {value}' for label, value in zip('ABC', ordered))
            golden.append(dict(id=f'cli-eval-{len(golden) + 1:02d}', group_id=group, input=text,
                               expected='ABC'[position], choices=list('ABC'), slices=[slice_name], source=source))
    behavior = [
        {**golden[0], 'id': 'behavior-format', 'type': 'format',
         'input': golden[0]['input'] + '\nUse only the requested JSON answer format.'},
        {**golden[20], 'id': 'behavior-paraphrase', 'type': 'paraphrase',
         'input': 'Which lsblk switch makes output machine-readable as JSON?\nA. --pairs\nB. --json\nC. --raw', 'expected': 'B'},
        dict(id='behavior-refusal', group_id='outside-domain/poem', type='refusal',
             input='Write a romantic poem about the moon. Choices: A, B, C, REFUSE.',
             expected='REFUSE', choices=['A', 'B', 'C', 'REFUSE'], slices=['refusal'],
             source='Authored out-of-domain request; evaluation/params.json system boundary'),
    ]
    from evaluation.contract import validate_golden, write_json
    proof = validate_golden(golden, index, behavior)
    write_rows(ROOT / 'data/eval/golden.jsonl', golden)
    write_rows(ROOT / 'data/eval/training-index.jsonl', index)
    write_rows(ROOT / 'data/eval/behavioral.jsonl', behavior)
    paths = ['params.yaml', 'src/tokenize_data.py', 'src/prompt.py', 'src/collate.py', 'src/pack.py',
             'data/train.jsonl', 'data/val.jsonl', 'data/tokenized/train.pt', 'data/tokenized/val.pt']
    hashes = {str(Path('../hw4') / p): hashlib.sha256((HW4 / p).read_bytes()).hexdigest() for p in paths}
    write_json(ROOT / 'data/provenance.json', dict(source='ДЗ 4: выход process_split/tokenize',
               source_sha256=hashes, train_index_rows=len(index), tensor_ids_match=True,
               tensor_model=tokenized['model'], validation=proof,
               review='Authored and reviewed by the coding assistant against the listed tool manuals; no human review claimed.'))
    print(json.dumps(proof, ensure_ascii=False))


if __name__ == '__main__':
    main()
