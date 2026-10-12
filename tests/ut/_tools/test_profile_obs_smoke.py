# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

from urllib.error import HTTPError

import pytest

from tools.profile_obs_smoke import check, report_failure


def test_wrapped_error_reports_metadata_without_secrets(capsys):
    inner = RuntimeError("secret request headers and signed URL")
    inner.response = {
        "Error": {"Code": "AccessDenied", "Message": "secret server message"},
        "ResponseMetadata": {"HTTPStatusCode": 403, "HTTPHeaders": {"Authorization": "secret"}},
    }
    inner.operation_name = "PutObject"
    outer = RuntimeError("secret wrapper message")
    outer.__context__ = inner

    report_failure("archive upload", outer)

    output = capsys.readouterr().out
    assert "AccessDenied" in output
    assert "403" in output
    assert "PutObject" in output
    assert "secret" not in output


def test_http_error_hides_url_and_headers(capsys):
    error = HTTPError("https://example.com/?secret=signature", 403, "secret", {"secret": "key"}, None)
    report_failure("anonymous download", error)
    output = capsys.readouterr().out
    assert "403" in output
    assert "secret" not in output
    assert "https://" not in output


def test_check_reports_success(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with check("upload"):
        pass
    assert "START: upload" in capsys.readouterr().out
    assert "PASS: upload" in summary.read_text()


def test_check_reports_failure_and_reraises(capsys):
    with pytest.raises(ValueError, match="secret"), check("cleanup"):
        raise ValueError("secret")
    output = capsys.readouterr().out
    assert '"stage": "cleanup"' in output
    assert "ValueError" in output
    assert "secret" not in output
    assert "PASS" not in output
