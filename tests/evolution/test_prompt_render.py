from __future__ import annotations

import pytest

from evolution import prompts


@pytest.fixture(autouse=True)
def _isolated_prompts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "_PROMPTS_DIR", tmp_path)
    prompts.clear_cache()
    yield
    prompts.clear_cache()


def _write(tmp_path, rel_name, text):
    path = tmp_path / f"{rel_name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_load_prompt_basic_and_cache(tmp_path):
    _write(tmp_path, "greet", "hello {name}")

    assert prompts.load_prompt("greet") == "hello {name}"

    (tmp_path / "greet.md").write_text("changed on disk", encoding="utf-8")
    assert prompts.load_prompt("greet") == "hello {name}"


def test_load_prompt_resolves_include(tmp_path):
    _write(tmp_path, "_blocks/leak_rules", "never leak secrets")
    _write(tmp_path, "main", "rules:\n{{include:_blocks/leak_rules}}\ndone")

    assert prompts.load_prompt("main") == "rules:\nnever leak secrets\ndone"


def test_include_rejects_non_block_paths(tmp_path):
    _write(tmp_path, "other", "should not be includable")
    _write(tmp_path, "main", "{{include:other}}")

    with pytest.raises(ValueError, match="_blocks"):
        prompts.load_prompt("main")


def test_include_rejects_traversal(tmp_path):
    _write(tmp_path, "main", "{{include:_blocks/../other}}")

    with pytest.raises(ValueError, match="_blocks"):
        prompts.load_prompt("main")


def test_nested_include_rejected(tmp_path):
    _write(tmp_path, "_blocks/inner", "x")
    _write(tmp_path, "_blocks/outer", "{{include:_blocks/inner}}")
    _write(tmp_path, "main", "{{include:_blocks/outer}}")

    with pytest.raises(ValueError):
        prompts.load_prompt("main")


def test_missing_file_error_names_path(tmp_path):
    with pytest.raises(FileNotFoundError) as exc_info:
        prompts.load_prompt("nope")

    assert str(tmp_path / "nope.md") == str(exc_info.value)


def test_render_missing_kwarg_raises(tmp_path):
    _write(tmp_path, "p", "hello {name}")

    with pytest.raises(ValueError, match="name"):
        prompts.render("p")


def test_render_extra_kwarg_raises(tmp_path):
    _write(tmp_path, "p", "hello {name}")

    with pytest.raises(ValueError, match="extra"):
        prompts.render("p", name="example", unused="x")


def test_render_json_example_untouched(tmp_path):
    _write(tmp_path, "p", 'do {action}: {"edits": [{"op": "append"}]}')

    out = prompts.render("p", action="run")

    assert out == 'do run: {"edits": [{"op": "append"}]}'


def test_render_repeated_placeholder(tmp_path):
    _write(tmp_path, "p", "{name} said hi to {name}")

    assert prompts.render("p", name="example") == "example said hi to example"
