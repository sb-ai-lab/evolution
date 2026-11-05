"""Documents ``EvoTaskLayout.run_solution``'s workspace ``solve.sh`` contract.

When a workspace-local ``solve.sh`` is present it is honored (``bash -c`` of its
body with ``python3`` rewritten to the role interpreter, cwd=workspace) and wins
over ``_solve.py``; otherwise ``python_exe _solve.py`` runs. A missing
``_solve.py`` with no ``solve.sh`` is a loud failure, not a silent no-run. This
contract crosses the layout abstraction boundary, so it gets its own test rather
than riding on the equivalence smoke.
"""

from __future__ import annotations

import sys
from pathlib import Path

from evolution.eval.layouts.evo import EvoTaskLayout


def test_solve_sh_is_honored_and_wins_over_solve_py(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "solve.sh").write_text("touch ran_via_solve_sh.txt\n", encoding="utf-8")
    (ws / "_solve.py").write_text("raise SystemExit('solve.py should not run')\n", encoding="utf-8")

    res = EvoTaskLayout().run_solution(ws, sys.executable, timeout=30)

    assert res.ok, res.stderr or res.error
    assert (ws / "ran_via_solve_sh.txt").is_file()
    assert "solve.py should not run" not in (res.stderr or "")


def test_solve_sh_runs_with_workspace_cwd(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "solve.sh").write_text("pwd > cwd.txt\n", encoding="utf-8")

    res = EvoTaskLayout().run_solution(ws, sys.executable, timeout=30)

    assert res.ok, res.stderr or res.error
    recorded = (ws / "cwd.txt").read_text(encoding="utf-8").strip()
    assert Path(recorded).resolve() == ws.resolve()


def test_solve_sh_python3_token_is_patched_to_python_exe(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "solve.sh").write_text(
        "python3 -c \"import sys; open('exe.txt','w').write(sys.executable)\"\n",
        encoding="utf-8",
    )

    res = EvoTaskLayout().run_solution(ws, sys.executable, timeout=30)

    assert res.ok, res.stderr or res.error
    assert (ws / "exe.txt").read_text(encoding="utf-8").strip() == sys.executable


def test_falls_back_to_solve_py_when_no_solve_sh(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "_solve.py").write_text("open('ran_via_solve_py.txt', 'w').close()\n", encoding="utf-8")

    res = EvoTaskLayout().run_solution(ws, sys.executable, timeout=30)

    assert res.ok, res.stderr or res.error
    assert (ws / "ran_via_solve_py.txt").is_file()


def test_missing_solve_py_without_solve_sh_is_loud_failure(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()

    res = EvoTaskLayout().run_solution(ws, sys.executable, timeout=30)

    assert not res.ok
    assert res.error is not None
    assert "missing" in res.error
