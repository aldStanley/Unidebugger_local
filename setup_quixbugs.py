#!/usr/bin/env python3
"""
One-time setup script for the QuixBugs benchmark.

Creates the checkout structure expected by UniDebugger-Local:
  benchmarks/quixbugs/
  ├── root_cause_path.json
  ├── config.json              (quixbugs_repo path for patch testing)
  └── checkouts/
      ├── BITCOUNT_1_buggy/
      │   ├── java_programs/BITCOUNT.java
      │   ├── tests/java_testcases/junit/BITCOUNT_TEST.java
      │   └── failing_tests
      └── ...

Usage:
    cd Unidebugger/
    python setup_quixbugs.py --quixbugs_repo ../../QuixBugs --output benchmarks/quixbugs
"""

import os
import sys
import json
import shutil
import subprocess
import argparse
import xml.etree.ElementTree as ET

UTILITY_CLASSES = {"Node", "WeightedEdge"}


def find_gradle() -> str:
    wrapper_dists = os.path.expanduser("~/.gradle/wrapper/dists")
    if not os.path.isdir(wrapper_dists):
        raise FileNotFoundError("~/.gradle/wrapper/dists not found. Install Gradle via https://gradle.org/install/")

    for dist_name in sorted(os.listdir(wrapper_dists), reverse=True):
        if not (dist_name.startswith("gradle-8") or dist_name.startswith("gradle-7")):
            continue
        dist_dir = os.path.join(wrapper_dists, dist_name)
        for hash_dir in os.listdir(dist_dir):
            # dist_name is e.g. "gradle-8.14.3-all" or "gradle-7.5.1-bin"
            gradle_dir_name = dist_name.replace("-all", "").replace("-bin", "")
            gradle_bin = os.path.join(dist_dir, hash_dir, gradle_dir_name, "bin", "gradle")
            if os.path.isfile(gradle_bin):
                return gradle_bin

    raise FileNotFoundError("No Gradle 7/8 found in ~/.gradle/wrapper/dists")


def run_gradle_test(gradle_bin: str, repo_dir: str, test_class: str) -> str:
    """Run tests for one program; return path to XML report regardless of exit code."""
    subprocess.run(
        [gradle_bin, "test", "--tests", test_class, "--rerun-tasks", "--continue"],
        cwd=repo_dir,
        capture_output=True,
        timeout=300,
    )
    return os.path.join(repo_dir, "build", "test-results", "test", f"TEST-{test_class}.xml")


def xml_to_failing_tests(xml_path: str, test_class: str) -> str:
    """Convert Gradle XML test report to D4J-style failing_tests format."""
    if not os.path.exists(xml_path):
        return ""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    lines = []
    for tc in root.findall("testcase"):
        failure = tc.find("failure")
        if failure is not None:
            method = tc.get("name")
            lines.append(f"--- {test_class}::{method}")
            trace = (failure.text or "").strip()
            lines.append(trace)
    return "\n".join(lines)


def discover_programs(repo_dir: str):
    """Return (program_name, src_path, test_path) for every program that has a test."""
    java_dir = os.path.join(repo_dir, "java_programs")
    test_dir = os.path.join(repo_dir, "java_testcases", "junit")
    programs = []
    for fname in sorted(os.listdir(java_dir)):
        if not fname.endswith(".java"):
            continue
        prog = fname[:-5]
        if prog in UTILITY_CLASSES:
            continue
        test_file = os.path.join(test_dir, f"{prog}_TEST.java")
        if not os.path.exists(test_file):
            continue
        programs.append((prog, os.path.join(java_dir, fname), test_file))
    return programs


def setup_quixbugs(repo_dir: str, output_dir: str):
    repo_dir = os.path.abspath(repo_dir)
    output_dir = os.path.abspath(output_dir)
    checkouts_dir = os.path.join(output_dir, "checkouts")
    os.makedirs(checkouts_dir, exist_ok=True)

    gradle_bin = find_gradle()
    print(f"Gradle: {gradle_bin}")

    programs = discover_programs(repo_dir)
    print(f"Found {len(programs)} programs\n")

    root_cause_path = {}

    # Shared utility files to copy into every checkout's java_programs/
    java_dir = os.path.join(repo_dir, "java_programs")
    shared_files = []
    for util in UTILITY_CLASSES:
        p = os.path.join(java_dir, f"{util}.java")
        if os.path.exists(p):
            shared_files.append(p)
    extra_dir = os.path.join(java_dir, "extra")

    for i, (prog, src_file, test_file) in enumerate(programs, 1):
        bug_name = f"{prog}_1"
        print(f"[{i}/{len(programs)}] {bug_name} ...", end=" ", flush=True)

        checkout = os.path.join(checkouts_dir, f"{bug_name}_buggy")
        prog_out = os.path.join(checkout, "java_programs")
        test_out = os.path.join(checkout, "tests", "java_testcases", "junit")
        os.makedirs(prog_out, exist_ok=True)
        os.makedirs(test_out, exist_ok=True)

        # Source + test
        shutil.copy2(src_file, os.path.join(prog_out, f"{prog}.java"))
        shutil.copy2(test_file, os.path.join(test_out, f"{prog}_TEST.java"))

        # Shared utility classes
        for uf in shared_files:
            shutil.copy2(uf, prog_out)
        if os.path.isdir(extra_dir):
            extra_out = os.path.join(prog_out, "extra")
            if os.path.exists(extra_out):
                shutil.rmtree(extra_out)
            shutil.copytree(extra_dir, extra_out)

        # Run tests and write failing_tests
        test_class = f"java_testcases.junit.{prog}_TEST"
        xml_path = run_gradle_test(gradle_bin, repo_dir, test_class)
        failing = xml_to_failing_tests(xml_path, test_class)
        with open(os.path.join(checkout, "failing_tests"), "w") as f:
            f.write(failing)

        fail_count = failing.count("--- ")
        print(f"{fail_count} failing test(s)")

        root_cause_path[bug_name] = f"java_programs/{prog}.java"

    # Write outputs
    with open(os.path.join(output_dir, "root_cause_path.json"), "w") as f:
        json.dump(root_cause_path, f, indent=2, sort_keys=True)

    with open(os.path.join(output_dir, "config.json"), "w") as f:
        json.dump({"quixbugs_repo": repo_dir}, f, indent=2)

    print(f"\nDone. {len(root_cause_path)} bugs → {output_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set up QuixBugs for UniDebugger-Local")
    parser.add_argument("--quixbugs_repo", required=True, help="Path to cloned QuixBugs repo")
    parser.add_argument("--output", default="benchmarks/quixbugs", help="Output benchmark directory")
    args = parser.parse_args()
    setup_quixbugs(args.quixbugs_repo, args.output)
