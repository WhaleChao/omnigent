"""A recorded ``page`` stops filming when the test body ends, before fixtures tear down.

Runs nested pytest sessions with recording on (real browser, no live server)."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

# ``pytester`` points HOME at a temp dir, where Playwright would look for its browsers.
_REAL_HOME = Path.home()

_PLAYWRIGHT_PLUGINS = (
    "-p",
    "pytest_playwright.pytest_playwright",
    "-p",
    "pytest_base_url.plugin",
)

_LATER_FIXTURE_JOURNEY = """
import json
import os
from pathlib import Path

import pytest

REPORT = Path(os.environ["RECORDING_REPORT"])
RAW = Path(os.environ["OMNIGENT_E2E_RECORD_DIR"])
STATE = {}


@pytest.fixture
def native_session():
    yield "session"
    # Torn down before ``page``/``context``, like a fixture deleting its session.
    REPORT.write_text(json.dumps({
        "page_closed": STATE["page"].is_closed(),
        "videos": sorted(path.name for path in RAW.glob("*.webm")),
    }))


def test_journey(page, native_session):
    STATE["page"] = page
    page.set_content("<h1 style='font-size:72px'>live</h1>")
    page.wait_for_timeout(1_000)"""

# The cleanup fixture depends on ``page`` and deletes its seeded data in ``finally``.
_PAGE_DEPENDENT_CLEANUP_JOURNEY = """
import json
import os
from pathlib import Path

import pytest

REPORT = Path(os.environ["RECORDING_REPORT"])
RAW_DIR = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
RAW = Path(RAW_DIR) if RAW_DIR else None
RESULTS = ["file_name e540789f", "fileXname e540789f"]


@pytest.fixture
def search_sessions(page):
    try:
        yield RESULTS
    finally:
        # Like ``httpx.delete`` on the seeded sessions: filmed if the page still records.
        REPORT.write_text(json.dumps({
            "page_closed": page.is_closed(),
            "videos": sorted(path.name for path in RAW.glob("*.webm")) if RAW else [],
        }))
        RESULTS.clear()


def test_journey(page, search_sessions):
    page.set_content("<ul>" + "".join(f"<li>{row}</li>" for row in search_sessions) + "</ul>")
    page.wait_for_timeout(1_000)"""


def _browsers_path() -> str:
    """Playwright's browser cache as resolved from the real home, not pytester's."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return configured
    if platform.system() == "Darwin":
        return str(_REAL_HOME / "Library" / "Caches" / "ms-playwright")
    cache_home = os.environ.get("XDG_CACHE_HOME") or str(_REAL_HOME / ".cache")
    return str(Path(cache_home) / "ms-playwright")


def _configure(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    record_dir: Path | None,
) -> Path:
    root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _browsers_path())
    monkeypatch.delenv("OMNIGENT_E2E_RECORD_DIR", raising=False)
    if record_dir is not None:
        monkeypatch.setenv("OMNIGENT_E2E_RECORD_DIR", str(record_dir))
    report = tmp_path / "report.json"
    monkeypatch.setenv("RECORDING_REPORT", str(report))
    pytester.makeconftest('pytest_plugins = ["tests.e2e_ui.conftest"]')
    return report


def test_video_is_finalized_before_a_later_fixture_tears_down(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=raw)
    pytester.makepyfile(_LATER_FIXTURE_JOURNEY)

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], "page was still filming when the session fixture tore down"
    assert observed["videos"], "no video had been written when the session fixture tore down"
    assert sorted(path.name for path in raw.glob("*.webm")) == observed["videos"]


def test_video_is_finalized_before_a_page_dependent_fixture_cleans_up(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=raw)
    pytester.makepyfile(_PAGE_DEPENDENT_CLEANUP_JOURNEY)

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], "page was still filming when the fixture deleted its data"
    assert observed["videos"] == sorted(path.name for path in raw.glob("*.webm"))


def test_video_option_is_finalized_before_a_page_dependent_fixture_cleans_up(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=None)
    pytester.makepyfile(_PAGE_DEPENDENT_CLEANUP_JOURNEY)
    output = tmp_path / "pw-output"

    result = pytester.runpytest_subprocess(
        "-q", *_PLAYWRIGHT_PLUGINS, "--video", "on", "--output", str(output)
    )

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], "page was still filming when the fixture deleted its data"
    assert list(output.rglob("video.webm")), "pytest-playwright did not save its video"
