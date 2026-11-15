"""Local suite runner — runs the SonosController backend test suite.

The same command runs locally and in CI (the Jenkinsfile's validation Job).

Flow:
  1. install the backend (uv)
  2. wait for services, when backend/scripts/wait-for-services.py exists
  3. run backend pytest

Step 2 is the app's hook for sidecars that are slow to come up in CI (a search
engine, a message broker): the script runs under the backend's uv venv and
must exit non-zero if a service never becomes ready.

Output modes:
  simple  — progress indicators, captured output, a summary and test_results.md
            with each suite's passed/failed/skipped counts (default)
  full    — streamed output, and SUITE_RESULT markers from the JUnit XML
            reports in --junitxml-dir (for CI)

Each test run writes a JUnit XML report, in either mode: into --junitxml-dir
when given (backend.xml), into a temporary
directory otherwise. The counts come from those reports.
"""

import argparse
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from . import ALL_SUITES, REPO_ROOT, RESULTS_FILE
from .display import (
    is_full_mode,
    progress_end,
    progress_skip,
    progress_start,
    run_with_pager,
    set_full_mode,
)
from .junit import Counts, format_counts, report_counts
from .process import run, run_streamed, run_tracked

APP_NAME = "SonosController"

# The backend requires Python 3.13 (e.g. queue.ShutDown). Pin its uv venv to
# python3.13 when that interpreter is on PATH; in CI the base image's default
# python is already 3.13 (the binary may be absent by that name), so we skip it.
HAS_PYTHON313 = shutil.which("python3.13") is not None


class StepResult(NamedTuple):
    step: str
    ok: bool
    detail: str
    peak_mb: float | None
    # A test run (pytest): its counts come from its JUnit
    # report, and are None when it wrote none (a crash, a timeout).
    suite: bool = False
    counts: Counts | None = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=f"Run the {APP_NAME} backend test suite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s --suite backend
  %(prog)s --suite backend --backend-args "tests/test_foo.py -k bar"
  %(prog)s --max-failures 10
  %(prog)s --suite backend --junitxml-dir results   # keep the JUnit XML reports
  %(prog)s --output-mode full --junitxml-dir /work/results --retries 2   # as CI runs it""",
    )
    parser.add_argument(
        "--suite", default=",".join(ALL_SUITES),
        help="Comma-separated suites: backend (default: all)",
    )
    parser.add_argument("--backend-args", default="",
                        help='Extra pytest arguments (e.g., "tests/test_foo.py -k bar")')
    parser.add_argument("--max-failures", type=int, default=None,
                        help="Max failures before stopping (pytest --maxfail)")
    # CI's command line is fixed (the library's modernApp.test passes
    # --retries 2), and only Playwright retries, so the backend suite accepts
    # the flag and ignores it.
    parser.add_argument("--retries", type=int, default=None,
                        help="Playwright retry count; accepted and ignored, as the app has no frontend")
    parser.add_argument("--output-mode", choices=["simple", "full"], default="simple",
                        help="Output mode: simple (progress, a summary with each suite's "
                             "counts, test_results.md) or full (streamed output, and "
                             "SUITE_RESULT markers when --junitxml-dir is given, for CI)")
    parser.add_argument("--junitxml-dir", default=None,
                        help="Directory to keep the JUnit XML report in (backend.xml), in "
                             "either output mode (default: a temporary directory)")
    args = parser.parse_args(argv)

    args.suites = [s.strip() for s in args.suite.split(",") if s.strip()]
    for s in args.suites:
        if s not in ALL_SUITES:
            parser.error(f"Unknown suite: {s!r}. Choose from: {', '.join(ALL_SUITES)}")
    if not args.suites:
        parser.error("--suite requires at least one suite")

    args.full = args.output_mode == "full"

    # Absolute: pytest runs in backend/.
    if args.junitxml_dir:
        args.junitxml_dir = str(Path(args.junitxml_dir).resolve())
        Path(args.junitxml_dir).mkdir(parents=True, exist_ok=True)

    return args


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------

def _run_cmd(cmd, *, cwd, timeout=120, env=None):
    """Run a test command, choosing streamed or tracked based on output mode.

    Returns ``(ok, detail, peak_mb)`` — in full mode *detail* is always
    empty and *peak_mb* is always None.
    """
    if is_full_mode():
        ok = run_streamed(cmd, cwd=cwd, timeout=timeout, env=env)
        return ok, "", None
    ok, detail, peak_mb = run_tracked(cmd, cwd=cwd, timeout=timeout, env=env)
    return ok, detail, peak_mb


def _install_cmd(cmd, *, cwd, timeout=120, env=None):
    """Run an install/build command (no memory tracking).

    Returns ``(ok, detail)`` — in full mode *detail* is always empty.
    """
    if is_full_mode():
        ok = run_streamed(cmd, cwd=cwd, timeout=timeout, env=env)
        return ok, ""
    return run(cmd, cwd=cwd, timeout=timeout, env=env)


# ---------------------------------------------------------------------------
# Suite orchestration
# ---------------------------------------------------------------------------

def run_tests(args, report_dir):
    """Run test steps for selected suites. Returns (exit_code, results).

    results is a list of StepResult; the test runs write their JUnit XML
    reports into report_dir.
    """
    backend = REPO_ROOT / "backend"
    results = []
    exit_code = 0
    extra_args = {
        "backend": shlex.split(args.backend_args) if args.backend_args else [],
    }

    # --- Backend install ---
    backend_installed = False
    col = progress_start("Installing backend dependencies")
    if backend.is_dir():
        cmds = [["uv", "sync", "--locked"] + (["--python", "python3.13"] if HAS_PYTHON313 else [])]
        ok, detail = True, ""
        for cmd in cmds:
            ok, detail = _install_cmd(cmd, cwd=backend, timeout=600)
            if not ok:
                break
        progress_end(ok, col)
        results.append(StepResult("backend install", ok, detail, None))
        backend_installed = ok
    else:
        progress_end(False, col)
        results.append(StepResult("backend install", False, f"Directory not found: {backend}", None))

    # --- Service readiness (app hook) ---
    wait_script = backend / "scripts" / "wait-for-services.py"
    if backend_installed and wait_script.is_file():
        col = progress_start("Waiting for services")
        ok, detail = _install_cmd(
            ["uv", "run", "python", str(wait_script)], cwd=backend, timeout=300
        )
        progress_end(ok, col)
        results.append(StepResult("services", ok, detail, None))
        if not ok:
            exit_code = 1

    # --- Backend tests ---
    if "backend" in args.suites:
        col = progress_start("Running backend tests")
        if not backend_installed:
            progress_skip(col)
            results.append(StepResult("backend pytest", False, "Skipped (install failed)", None))
        else:
            report = report_dir / "backend.xml"
            report.unlink(missing_ok=True)
            pytest_cmd = ["uv", "run", "pytest", "-v", "--tb=short", f"--junitxml={report}"]
            if args.max_failures is not None:
                pytest_cmd.append(f"--maxfail={args.max_failures}")
            pytest_cmd.extend(extra_args["backend"])
            ok, detail, peak_mb = _run_cmd(pytest_cmd, cwd=backend, timeout=1800)
            progress_end(ok, col)
            results.append(StepResult("backend pytest", ok, detail, peak_mb, suite=True,
                                      counts=report_counts(report)))
            if not ok:
                exit_code = 1

    return exit_code, results


# ---------------------------------------------------------------------------
# JUnit XML → SUITE_RESULT markers
# ---------------------------------------------------------------------------

def _emit_suite_results(junitxml_dir):
    """Parse JUnit XML files and emit SUITE_RESULT markers for CI."""
    xml_dir = Path(junitxml_dir)
    if not xml_dir.is_dir():
        return

    for xml_file in sorted(xml_dir.glob("*.xml")):
        counts = report_counts(xml_file) or Counts(0, 0, 0)
        print(f"===SUITE_RESULT:{xml_file.stem}:{counts.passed}:{counts.failed}:"
              f"{counts.skipped}===", flush=True)


# ---------------------------------------------------------------------------
# Output formatting (simple mode)
# ---------------------------------------------------------------------------

def _extract_pytest_failures(detail):
    return [line.strip() for line in detail.splitlines() if line.strip().startswith("FAILED ")]


def _format_mem(peak_mb):
    if peak_mb is not None and peak_mb > 0:
        return f" [{peak_mb:,.0f} MB peak]"
    return ""


def _format_suite_counts(steps):
    """A line per test run with its passed/failed/skipped counts, under a header."""
    return [f"  {s.step}: {format_counts(s.counts)}" for s in steps if s.suite]


def format_summary(all_results):
    lines = []
    any_failure = False

    for app_name, steps in all_results:
        failures = [s for s in steps if not s.ok]
        if failures:
            any_failure = True
            lines.append(f"\n{app_name}: FAILURES")
            for s in failures:
                mem = _format_mem(s.peak_mb)
                if "Skipped" in s.detail:
                    lines.append(f"  {s.step}: {s.detail}")
                elif s.step == "backend pytest":
                    extracted = _extract_pytest_failures(s.detail)
                    for line in extracted:
                        lines.append(f"  {line}")
                    if not extracted:
                        lines.append(f"  {s.step}: failed{mem} (see test_results.md)")
                    elif mem:
                        lines.append(f"  {mem.strip()}")
                else:
                    lines.append(f"  {s.step}: FAILED (see test_results.md)")
        else:
            mem_parts = [f"{s.step}{_format_mem(s.peak_mb)}" for s in steps]
            lines.append(f"\n{app_name}: all passed ({', '.join(mem_parts)})")

        suite_counts = _format_suite_counts(steps)
        if suite_counts:
            lines.append(f"\n{app_name}: test counts")
            lines.extend(suite_counts)

    if not any_failure:
        lines.append("\nAll steps passed.")

    return "\n".join(lines) + "\n"


def format_app_detailed(app_name, steps):
    lines = [f"## {app_name}\n"]
    for s in steps:
        status = "PASS" if s.ok else "FAIL"
        counts = f" ({format_counts(s.counts)})" if s.suite else ""
        lines.append(f"### {s.step}: {status}{counts}{_format_mem(s.peak_mb)}\n")
        if not s.ok and s.detail and "Skipped" not in s.detail:
            lines.append("```")
            lines.extend(s.detail.splitlines())
            lines.append("```\n")
        elif not s.ok:
            lines.append(f"{s.detail}\n")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def main(argv=None):
    args = parse_args(argv)
    set_full_mode(args.full)

    # The test runs always write JUnit XML reports, for the counts; without
    # --junitxml-dir they go to a directory that lives as long as the run.
    with tempfile.TemporaryDirectory(prefix="run-suite-") as tmp:
        exit_code, steps = run_tests(args, Path(args.junitxml_dir or tmp))

    if is_full_mode():
        # Full mode: emit SUITE_RESULT markers from the kept JUnit XML
        if args.junitxml_dir:
            col = progress_start("Exporting test results")
            _emit_suite_results(args.junitxml_dir)
            progress_end(True, col)
    else:
        # Simple mode: write test_results.md and print the summary, with counts
        RESULTS_FILE.write_text("# Test Results\n\n")
        with RESULTS_FILE.open("a") as f:
            f.write(format_app_detailed(APP_NAME, steps))
        print(f"\nDetailed results written to {RESULTS_FILE}", file=sys.stderr)
        print(format_summary([(APP_NAME, steps)]))

    sys.exit(exit_code)


def cli():
    run_with_pager(main)
