"""Kernel-specific filesystem inspection must carry an explicit platform mark."""
import ast
import os
from pathlib import Path

import pytest


_KERNEL_PREFIX = '/' + 'proc/'


def _linux_only(mark):
    return (isinstance(mark, ast.Call) and isinstance(mark.func, ast.Name)
            and mark.func.id == 'linux_only')


def _module_linux_only(module):
    for node in module.body:
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == 'pytestmark' for target in node.targets
        ):
            continue
        values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else (node.value,)
        if any(_linux_only(value) for value in values):
            return True
    return False


def test_no_unmarked_proc_reads():
    failures = []
    for path in sorted(Path(__file__).parent.glob('*.py')):
        if path.name == 'platforms.py':
            continue
        source = path.read_text()
        module = ast.parse(source)
        if _module_linux_only(module):
            continue
        ranges = [(min(node.lineno, *(mark.lineno for mark in node.decorator_list)), node.end_lineno)
                  for node in ast.walk(module)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and any(_linux_only(mark) for mark in node.decorator_list)]
        for number, line in enumerate(source.splitlines(), start=1):
            if _KERNEL_PREFIX in line and not any(start <= number <= end for start, end in ranges):
                failures.append(f'{path.name}:{number}: add an explicit linux_only mark or use a portable check')
    assert failures == []


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_fd_path_resolves_open_object(tmp_path, kind):
    from platforms import fd_path
    path = tmp_path / kind
    if kind == 'directory':
        path.mkdir()
    else:
        path.write_text('descriptor path')
    fd = os.open(path, os.O_RDONLY)
    try:
        assert fd_path(fd) == os.path.realpath(path)
    finally:
        os.close(fd)
