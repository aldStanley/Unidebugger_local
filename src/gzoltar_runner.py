"""
Fault localization for UniDebugger-Local.

Primary method: spectrum-based FL derived from test failure stack traces.
  - Parses failing_tests to find which program lines appear in stack traces.
  - Applies Ochiai-like frequency scoring across all failing tests.
  - Zero external dependencies; works for both QuixBugs and D4J.

GZoltar hook: full GZoltar 1.7.3 execution is also attempted when the tools/
  gzoltar/ directory is present. Requires Java 8 or 11 JVM (GZoltar 1.7.3
  does not support Java 20 class files).

Output format (returned by run_fault_localization):
  list of (line_number: int, score: float) sorted by score descending,
  ready to prepend to the Locator agent's prompt.
"""

import os
import re
import subprocess
import logging
import tempfile
from collections import defaultdict
from typing import List, Tuple


# ── Stack-trace-based SBFL ────────────────────────────────────────────────────

def _parse_stack_trace_lines(failing_tests_content: str, program_class_prefix: str) -> List[Tuple[int, float]]:
    """
    Parse GZoltar-format failing_tests and extract line numbers that appear
    in the stack traces of failing tests, scoring by Ochiai approximation.

    program_class_prefix: e.g. "java_programs.BITCOUNT" for QuixBugs,
                          or "org.apache.commons.lang" for D4J.
    """
    line_fail_count: dict = defaultdict(int)
    line_pass_count: dict = defaultdict(int)
    total_fail = 0
    total_pass = 0

    current_is_fail = False
    in_trace = False

    for raw_line in failing_tests_content.splitlines():
        line = raw_line.strip()
        if line.startswith("--- "):
            # New test entry; failing_tests only lists failing tests
            current_is_fail = True
            in_trace = True
            total_fail += 1
            continue

        if in_trace and line.startswith("at ") and program_class_prefix in line:
            m = re.search(r':(\d+)\)$', line)
            if m:
                lineno = int(m.group(1))
                if current_is_fail:
                    line_fail_count[lineno] += 1
                else:
                    line_pass_count[lineno] += 1

    if total_fail == 0:
        return []

    results = []
    for lineno in set(line_fail_count) | set(line_pass_count):
        ef = line_fail_count[lineno]
        ep = line_pass_count[lineno]
        nf = total_fail - ef
        denom = (total_fail * (ef + ep)) ** 0.5
        score = ef / denom if denom > 0 else 0.0
        results.append((lineno, round(score, 4)))

    return sorted(results, key=lambda x: x[1], reverse=True)


def _get_program_class_prefix(info: dict) -> str:
    """Derive the Java class prefix for the buggy program from project_meta."""
    data_name = info["project_meta"].get("data_name", "d4j")
    if data_name == "quixbugs":
        prog = info["project_meta"]["project_name"]
        return f"java_programs.{prog}"
    # D4J: derive from buggy_file_path
    buggy_path = info["project_meta"].get("buggy_file_path", "")
    # e.g. .../src/main/java/org/apache/commons/lang/StringUtils.java
    # → org.apache.commons.lang.StringUtils
    for marker in ["src/main/java/", "src/", "source/"]:
        if marker in buggy_path:
            rel = buggy_path[buggy_path.index(marker) + len(marker):]
            return rel.replace("/", ".").replace(".java", "")
    return info["project_meta"].get("project_name", "")


# ── GZoltar full execution (Java 8/11 only) ───────────────────────────────────

def _find_gzoltar_jars(tools_dir: str):
    lib = os.path.join(tools_dir, "gzoltar", "lib")
    agent = os.path.join(lib, "gzoltaragent.jar")
    cli = os.path.join(lib, "gzoltarcli.jar")
    if os.path.isfile(agent) and os.path.isfile(cli):
        return agent, cli
    return None, None


def _build_quixbugs_classpath(quixbugs_repo: str, cli_jar: str) -> str:
    gradle_cache = os.path.expanduser("~/.gradle/caches/modules-2/files-2.1")
    jars = [cli_jar]
    for group, artifact, version in [
        ("junit", "junit", "4.12"),
        ("org.hamcrest", "hamcrest-core", "1.3"),
    ]:
        base = os.path.join(gradle_cache, group, artifact, version)
        if os.path.isdir(base):
            for root, _, files in os.walk(base):
                for f in files:
                    if f.endswith(".jar") and "sources" not in f and "javadoc" not in f:
                        jars.append(os.path.join(root, f))
                        break
    jars.append(os.path.join(quixbugs_repo, "build", "classes", "java", "main"))
    jars.append(os.path.join(quixbugs_repo, "build", "classes", "java", "test"))
    return ":".join(jars)


def _run_gzoltar(info: dict, tools_dir: str) -> List[Tuple[int, float]]:
    """Full GZoltar execution. Only works with Java 8/11 JVM."""
    quixbugs_repo = info["project_meta"].get("quixbugs_repo", "")
    if not quixbugs_repo or info["project_meta"].get("data_name") != "quixbugs":
        return []

    agent_jar, cli_jar = _find_gzoltar_jars(tools_dir)
    if not agent_jar:
        return []

    prog = info["project_meta"]["project_name"]
    test_class = f"java_testcases.junit.{prog}_TEST"
    build_location = os.path.join(quixbugs_repo, "build", "classes", "java", "main")

    # Gather test methods from the test file
    test_file = os.path.join(
        info["project_meta"]["checkout_dir"],
        f"{info['project_meta']['bug_name']}_buggy",
        "tests", "java_testcases", "junit", f"{prog}_TEST.java"
    )
    test_methods = []
    if os.path.isfile(test_file):
        with open(test_file) as f:
            for line in f:
                m = re.search(r'public void (test_\w+)', line)
                if m:
                    test_methods.append(f"{test_class}#{m.group(1)}")
    if not test_methods:
        return []

    with tempfile.TemporaryDirectory() as tmpdir:
        methods_file = os.path.join(tmpdir, "test_methods.txt")
        with open(methods_file, "w") as f:
            f.write("\n".join(test_methods))

        ser_file = os.path.join(tmpdir, "gzoltar.ser")
        classpath = _build_quixbugs_classpath(quixbugs_repo, cli_jar)

        # Phase 1: instrument + run
        try:
            subprocess.run(
                [
                    "java",
                    f"-javaagent:{agent_jar}=destfile={ser_file},buildlocation={build_location}",
                    "-cp", classpath,
                    "com.gzoltar.cli.Main", "runTestMethods",
                    "--testMethods", methods_file,
                ],
                capture_output=True, timeout=120,
            )
        except Exception as e:
            logging.warning(f"GZoltar runTestMethods failed: {e}")
            return []

        if not os.path.isfile(ser_file):
            return []

        # Phase 2: generate ranking
        try:
            subprocess.run(
                [
                    "java", "-jar", cli_jar,
                    "faultLocalizationReport",
                    "--buildLocation", build_location,
                    "--dataFile", ser_file,
                    "--outputDirectory", tmpdir,
                    "--formula", "OCHIAI",
                    "--granularity", "LINE",
                    "--formatter", "TXT",
                ],
                capture_output=True, timeout=60,
            )
        except Exception as e:
            logging.warning(f"GZoltar faultLocalizationReport failed: {e}")
            return []

        # Parse ranking
        ranking_file = os.path.join(tmpdir, "sfl", "txt", "ochiai.ranking.csv")
        if not os.path.isfile(ranking_file):
            return []

        results = []
        with open(ranking_file) as f:
            next(f)  # skip header
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 2:
                    continue
                # name format: pkg/Class#method(sig):lineNo:File.java:lineNo
                name, score_str = parts[0], parts[-1]
                m = re.search(r':(\d+)$', name)
                if m:
                    results.append((int(m.group(1)), float(score_str)))
        return sorted(results, key=lambda x: x[1], reverse=True)


# ── Public API ────────────────────────────────────────────────────────────────

def run_fault_localization(info: dict, tools_dir: str = None) -> List[Tuple[int, float]]:
    """
    Return a ranked list of (line_number, score) for the buggy file.
    Tries GZoltar first; falls back to stack-trace-based SBFL.
    """
    if tools_dir is None:
        tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools")

    # Try GZoltar (only works with Java 8/11)
    try:
        results = _run_gzoltar(info, tools_dir)
        if results:
            logging.info(f"GZoltar produced {len(results)} suspicious lines")
            return results
    except Exception as e:
        logging.debug(f"GZoltar skipped: {e}")

    # Fallback: stack-trace SBFL
    failing_tests_path = os.path.join(
        info["project_meta"]["checkout_dir"],
        f"{info['project_meta']['bug_name']}_buggy",
        "failing_tests",
    )
    failing_content = ""
    if os.path.isfile(failing_tests_path):
        with open(failing_tests_path) as f:
            failing_content = f.read()
    elif "failing_test_cases" in info:
        failing_content = info["failing_test_cases"]

    prefix = _get_program_class_prefix(info)
    results = _parse_stack_trace_lines(failing_content, prefix)
    logging.info(f"Stack-trace SBFL: {len(results)} suspicious lines for prefix '{prefix}'")
    return results


def format_sbfl_hint(results: List[Tuple[int, float]], top_k: int = 5) -> str:
    """Format SBFL results as a prompt hint for the Locator agent."""
    if not results:
        return ""
    top = results[:top_k]
    lines = ["Suspicious lines (fault localization ranking):"]
    for lineno, score in top:
        lines.append(f"  Line {lineno}: suspiciousness score {score:.4f}")
    return "\n".join(lines)
