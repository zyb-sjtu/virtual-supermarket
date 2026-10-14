"""Validate task counts, document links and progress updates in code changes."""
import argparse
from collections import Counter
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
STATUSES = ('已完成', '部分完成', '待实施', '暂缓')
CODE_SUFFIXES = {'.py', '.js', '.html', '.css', '.svg', '.ps1', '.json', '.yml', '.yaml', '.url'}


def check(base=None):
    tasks = (ROOT / 'TASKS.md').read_text(encoding='utf-8-sig')
    rows = re.findall(r'^\| ((?:P[1-4]|R)-\d{2}) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$', tasks, re.MULTILINE)
    if not rows or len({row[0] for row in rows}) != len(rows):
        raise ValueError('任务表为空或任务 ID 重复')
    counts = Counter(row[2].strip() for row in rows)
    if set(counts) - set(STATUSES):
        raise ValueError('任务状态必须为已完成/部分完成/待实施/暂缓')
    total = re.search(r'共 (\d+) 项', tasks)
    if not total or int(total[1]) != len(rows):
        raise ValueError('任务总数与台账不一致')
    for status in STATUSES:
        value = re.search(status + r' (\d+) 项', tasks)
        if not value or int(value[1]) != counts[status]:
            raise ValueError(status + ' 数量与台账不一致')
    for label in ('更新日期：', '当前业务版本：', '## 下一轮优先级', '## 已验证证据'):
        if label not in tasks:
            raise ValueError('任务规划缺少 ' + label)
    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8-sig')
    if '## Unreleased' not in changelog:
        raise ValueError('CHANGELOG 缺少 Unreleased')
    for path in [ROOT / 'README.md', ROOT / 'TASKS.md', ROOT / 'CHANGELOG.md', ROOT / 'AGENTS.md', *sorted((ROOT / 'docs').glob('*.md'))]:
        for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', path.read_text(encoding='utf-8-sig')):
            target = target.strip('<>').split('#')[0]
            if not target or re.match(r'^[a-z]+://', target):
                continue
            if not (path.parent / unquote(target)).is_file():
                raise ValueError(f'{path.relative_to(ROOT)} 链接目标不存在：{target}')
    if base and set(base) != {'0'}:
        if not re.fullmatch(r'[0-9a-fA-F]{40,64}', base):
            raise ValueError('base 必须为 Git 提交 SHA')
        result = subprocess.run(['git', '-c', 'core.quotepath=false', 'diff', '--name-only', '-z', base, 'HEAD'],
                                cwd=ROOT, capture_output=True, text=True, encoding='utf-8', check=True)
        changed = set(result.stdout.split('\0')) - {''}
        code_changed = any(Path(path).suffix in CODE_SUFFIXES and not path.startswith('docs/') for path in changed)
        if code_changed and not {'TASKS.md', 'CHANGELOG.md'} <= changed:
            raise ValueError('运行代码/测试/配置变化必须同步提交 TASKS.md 与 CHANGELOG.md')
    print(f'Progress OK: {len(rows)} tasks; ' + ', '.join(f'{s}={counts[s]}' for s in STATUSES))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base')
    args = parser.parse_args()
    try:
        check(args.base)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + '\n')
