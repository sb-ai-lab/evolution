"""Keep the approved dark palette readable and the light theme unchanged."""

from hashlib import sha256
from pathlib import Path
import re

import pytest


TOKENS = Path(__file__).parents[1] / "src/evolution_lab/panel/static/tokens.css"


def root_properties() -> dict[str, str]:
    css = TOKENS.read_text(encoding="utf-8")
    root = re.search(r":root\s*\{([^}]*)\}", css)
    assert root is not None
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", root.group(1)))


def contrast(fg: str, bg: str) -> float:
    def luminance(color: str) -> float:
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", color)
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    light, dark = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_dark_tokens_match_spec():
    expected = {
        "--bg": "#16181c", "--bg-sunk": "#111317", "--panel": "#1b1e23",
        "--panel-2": "#1f2227", "--hover": "#23262c", "--border": "#2e333b",
        "--border-row": "#262a31", "--border-strong": "#3a404a", "--text": "#d7dade",
        "--text-2": "#b9bec6", "--text-3": "#9aa1aa",
        # One green-channel step above the spec's #7e858f; that value fails 4.5 on --panel.
        "--muted": "#7e868f", "--faint": "#666d77", "--chip": "#24282f",
        "--blue": "#7cb3e8", "--blue-bg": "#1b2c3e", "--blue-line": "#28415c",
        "--green": "#6fc49b", "--amber": "#d9b06a", "--amber-bg": "#352c19",
        "--amber-line": "#4a3f22",
    }
    actual = root_properties()
    assert {name: actual[name] for name in expected} == expected


def test_light_theme_unchanged():
    css = TOKENS.read_bytes()
    light = css[css.index(b'[data-theme="light"]{') :]
    # SHA256 of the exact light block in base commit b7668049.
    assert sha256(light).hexdigest() == "b7b91dbd6974711f5b3722fe7ff56030b37149bcd64c256fc6c79d5693e7e4e7"


@pytest.mark.parametrize("foreground", ["--text", "--text-2", "--text-3", "--muted"])
@pytest.mark.parametrize("background", ["--bg", "--panel"])
def test_answer_text_contrast(foreground, background):
    tokens = root_properties()
    assert contrast(tokens[foreground], tokens[background]) >= 4.5


@pytest.mark.parametrize("background", ["--bg", "--panel"])
def test_decorative_contrast(background):
    tokens = root_properties()
    assert contrast(tokens["--faint"], tokens[background]) >= 3.0


def test_contrast_reference_extremes():
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0)
