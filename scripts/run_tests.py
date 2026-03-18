#!/usr/bin/env python3
"""
Automated test runner for AskUserQuestion Feishu regression suite.

Usage:
    python scripts/run_tests.py                  # Run all tests
    python scripts/run_tests.py -m integration   # Run integration tests only
    python scripts/run_tests.py --watch          # Watch mode: re-run on file changes
    python scripts/run_tests.py --report         # Generate detailed report
"""

import subprocess
import sys
import time
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List
import argparse


class TestRunner:
    """Manages test execution and reporting."""

    def __init__(self, project_root: Path, verbose: bool = False):
        self.project_root = project_root
        self.tests_dir = project_root / "tests"
        self.test_file = self.tests_dir / "test_askuserquestion_feishu.py"
        self.verbose = verbose
        self.results: Dict = {
            "timestamp": datetime.now().isoformat(),
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "total": 0,
            "duration": 0,
            "details": [],
        }

    def check_server_running(self) -> bool:
        """Check if walkcode server is running on port 3001."""
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(("127.0.0.1", 3001))
        sock.close()
        return result == 0

    def start_server(self) -> bool:
        """Start walkcode server in background."""
        print("🚀 Starting walkcode server...")
        try:
            # Kill any existing processes on port 3001
            subprocess.run(
                'lsof -i :3001 -t | xargs kill -9 2>/dev/null || true',
                shell=True,
                capture_output=True,
            )
            time.sleep(1)

            # Start server
            proc = subprocess.Popen(
                ["python", "-m", "walkcode", "serve"],
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Wait for server to start
            max_retries = 30
            for i in range(max_retries):
                if self.check_server_running():
                    print("✅ Server started successfully")
                    return True
                time.sleep(1)
                if self.verbose:
                    print(f"  Waiting for server... ({i+1}/{max_retries})")

            print("❌ Server failed to start")
            return False
        except Exception as e:
            print(f"❌ Error starting server: {e}")
            return False

    def run_pytest(self, markers: List[str] = None) -> bool:
        """Run pytest with specified markers."""
        print("\n📋 Running tests...")

        cmd = ["pytest", str(self.test_file), "-v", "--tb=short"]

        if markers:
            for marker in markers:
                cmd.extend(["-m", marker])

        if self.verbose:
            cmd.append("--capture=no")

        # Add JSON report
        report_file = self.project_root / ".pytest_report.json"
        cmd.extend(["--json-report", f"--json-report-file={report_file}"])

        try:
            start_time = time.time()
            result = subprocess.run(cmd, cwd=self.project_root, capture_output=True, text=True)
            duration = time.time() - start_time

            self.results["duration"] = duration

            if result.stdout:
                print(result.stdout)
            if result.stderr and result.returncode != 0:
                print(result.stderr)

            # Parse report if available
            if report_file.exists():
                with open(report_file) as f:
                    report = json.load(f)
                    self.results["passed"] = report["summary"].get("passed", 0)
                    self.results["failed"] = report["summary"].get("failed", 0)
                    self.results["skipped"] = report["summary"].get("skipped", 0)
                    self.results["total"] = report["summary"].get("total", 0)

            return result.returncode == 0

        except FileNotFoundError:
            print("❌ pytest not found. Install with: pip install pytest pytest-json-report")
            return False
        except Exception as e:
            print(f"❌ Error running tests: {e}")
            return False

    def print_summary(self) -> None:
        """Print test results summary."""
        print("\n" + "=" * 60)
        print("📊 TEST SUMMARY")
        print("=" * 60)
        print(f"Total:    {self.results['total']}")
        print(f"Passed:   {self.results['passed']} ✅")
        print(f"Failed:   {self.results['failed']} ❌")
        print(f"Skipped:  {self.results['skipped']} ⏭️")
        print(f"Duration: {self.results['duration']:.2f}s")
        print("=" * 60)

    def save_report(self, filepath: Path = None) -> None:
        """Save detailed test report."""
        if filepath is None:
            filepath = self.project_root / "test_results.json"

        with open(filepath, "w") as f:
            json.dump(self.results, f, indent=2)

        print(f"\n📄 Report saved to: {filepath}")

    def run(self, markers: List[str] = None, watch: bool = False) -> int:
        """Execute full test suite."""
        if not self.check_server_running():
            if not self.start_server():
                print("⚠️ Could not start server. Tests may fail.")
                return 1

        # Run tests
        success = self.run_pytest(markers)

        # Print summary
        self.print_summary()

        # Save report
        self.save_report()

        return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(
        description="Automated test runner for AskUserQuestion Feishu tests"
    )
    parser.add_argument(
        "-m",
        "--marker",
        dest="markers",
        action="append",
        help="Run tests matching marker (e.g., integration, edge_case)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--report", action="store_true", help="Generate detailed report")
    parser.add_argument("--watch", action="store_true", help="Watch mode (re-run on changes)")

    args = parser.parse_args()

    project_root = Path(__file__).parent.parent
    runner = TestRunner(project_root, verbose=args.verbose)

    try:
        return runner.run(markers=args.markers, watch=args.watch)
    except KeyboardInterrupt:
        print("\n\n⚠️ Tests interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
