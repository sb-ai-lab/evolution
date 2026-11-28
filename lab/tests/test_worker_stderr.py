"""Exception logging masks the full union of original secret occurrences."""
import pytest

from evolution_lab.owner import _worker_stderr as stderr


def test_exception_record_names_class_and_core_code():
    from evolution.tree.execution.config import ManifestError
    from evolution.tree.campaign import CampaignInvariantViolated
    errors = [(ManifestError("manifest_invalid", "workflow.scorer"),
               "evolution.tree.execution.config.ManifestError", "manifest_invalid"),
              (CampaignInvariantViolated("rule"), "evolution.tree.campaign.CampaignInvariantViolated", None),
              (RuntimeError("private-message"), "builtins.RuntimeError", None)]
    foreign = RuntimeError("private-message")
    foreign.code = "manifest_invalid"
    errors.append((foreign, "builtins.RuntimeError", None))
    bad_code = ManifestError("Bad Code", "workflow.scorer")
    errors.append((bad_code, "evolution.tree.execution.config.ManifestError", None))
    for error, name, code in errors:
        assert stderr.exception_record(error) == {"class": name, "code": code}


def test_exception_record_never_formats_or_raises():
    class PrivateError(Exception):
        @property
        def code(self):
            raise RuntimeError("private-code")
        def __str__(self):
            raise AssertionError("private-message")
    PrivateError.__module__ = "evolution.fake"
    assert stderr.exception_record(PrivateError()) == {"class": "unknown", "code": None}
    PrivateError.__module__ = "invalid module"
    assert stderr.exception_record(PrivateError()) == {"class": "unknown", "code": None}


@pytest.mark.parametrize("message,secrets", [
    ("abcdef", ("abc", "abcdef")),
    ("abcdef", ("abcdef", "abc")),
    ("abcd", ("abc", "bcd")),
    ("ababa", ("aba", "bab")),
    ("abXY", ("ab", "bX", "XY")),
    ("abc-abc", ("abc", "b")),
    ("яёж", ("яё", "ёж")),
])
def test_traceback_masks_nested_partial_and_repeated_secret_occurrences(capsys, message, secrets):
    stderr.write_exception(RuntimeError("left|" + message + "|right"), secrets)
    output = capsys.readouterr().err
    payload = output.split("left|", 1)[1].split("|right", 1)[0]
    assert not set(message.replace("-", "")) & set(payload)
    assert all(secret not in output for secret in secrets)
    assert "RuntimeError" in output


@pytest.mark.parametrize("secrets", [("analysis", "REDACTED"), ("hidden", "[REDACTED]"),
                                      ("#", "█", "REDACTED")])
def test_traceback_keeps_core_privacy_before_masking_and_calls_core_with_run_redactions(
    capsys, monkeypatch, secrets
):
    real_safe_record, calls = stderr.safe_record, []

    def checked(value, redactions=()):
        calls.append(redactions)
        return real_safe_record(value, redactions)

    monkeypatch.setattr(stderr, "safe_record", checked)
    stderr.write_exception(RuntimeError("<analysis>hidden-private-reasoning</analysis> " + " ".join(secrets)), secrets)
    output = capsys.readouterr().err
    assert "hidden-private-reasoning" not in output
    assert all(secret not in output for secret in secrets)
    if "analysis" in secrets:
        assert output == ""  # breaking Core's markup makes the orders disagree
    else:
        assert "RuntimeError" in output
    assert secrets in calls


@pytest.mark.parametrize("secrets", [("abc", "bcd"), ("traceback", "traceback truncated", "REDACTED"),
                                      ("RuntimeError", "Error", "#", "█")])
def test_traceback_truncation_and_marker_do_not_restore_secrets(capsys, secrets):
    stderr.write_exception(RuntimeError("é" * 65000 + "abcd " + " ".join(secrets)), secrets)
    output = capsys.readouterr().err
    assert output
    assert len(output.encode("utf-8")) <= 65536
    assert all(secret not in output for secret in secrets)


def test_traceback_before_redactions_never_formats_error(capsys):
    class PrivateError(Exception):
        def __str__(self):
            raise AssertionError("private message must not be formatted")

    stderr.write_exception(PrivateError(), None)
    assert capsys.readouterr().err == "PrivateError\n"


def test_privacy_markup_inside_secret_cannot_expose_its_prefix_or_suffix(capsys):
    secret = "private-prefix<analysis>hidden-reasoning</analysis>private-suffix"
    stderr.write_exception(RuntimeError(secret), (secret,))
    output = capsys.readouterr().err
    assert "private-prefix" not in output
    assert "private-suffix" not in output
    assert "hidden-reasoning" not in output
