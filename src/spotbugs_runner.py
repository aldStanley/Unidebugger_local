"""
Static analysis for the Slicer agent using SpotBugs 4.8.6.

SpotBugs detects known bug patterns (null dereference, resource leaks, wrong
comparisons, infinite loops, etc.) in compiled Java bytecode. Its output gives
the Slicer a concrete line-range anchor so the LLM is not left to guess which
50-100 line region to focus on.

For QuixBugs programs whose bugs are algorithmic (not detectable by SpotBugs),
the runner falls back to method-range extraction via the failing-test stack
trace. Either way, the Slicer receives a concrete line-range hint.

Output (returned by run_static_analysis):
  dict with keys:
    "hint": str   — formatted prompt snippet ready to prepend to Slicer's msg
    "start": int  — first suspicious line (0 if unknown)
    "end": int    — last suspicious line (0 if unknown)
"""

import os
import re
import subprocess
import logging
import tempfile
import xml.etree.ElementTree as ET
from typing import Optional


SPOTBUGS_JAR_RELPATH = os.path.join("spotbugs", "spotbugs-4.8.6", "lib", "spotbugs.jar")


def _find_spotbugs_jar(tools_dir: str) -> Optional[str]:
    jar = os.path.join(tools_dir, SPOTBUGS_JAR_RELPATH)
    return jar if os.path.isfile(jar) else None


def _class_name_for_file(info: dict) -> Optional[str]:
    """Derive the fully qualified class name from the buggy file path."""
    buggy_path = info["project_meta"].get("buggy_file_path", "")
    src_path = info["project_meta"].get("project_src_path", "")
    if not buggy_path or not src_path:
        return None
    rel = os.path.relpath(buggy_path, src_path)           # e.g. BITCOUNT.java
    return rel.replace(os.sep, ".").replace(".java", "")   # e.g. BITCOUNT


def _build_location(info: dict) -> Optional[str]:
    """Return the directory containing compiled .class files for the buggy file."""
    data_name = info["project_meta"].get("data_name", "d4j")
    if data_name == "quixbugs":
        repo = info["project_meta"].get("quixbugs_repo", "")
        if repo:
            loc = os.path.join(repo, "build", "classes", "java", "main")
            return loc if os.path.isdir(loc) else None
    # D4J: try to find compiled classes next to the source
    src_path = info["project_meta"].get("project_src_path", "")
    for candidate in ["target/classes", "build/classes", "bin"]:
        loc = os.path.join(os.path.dirname(src_path), candidate)
        if os.path.isdir(loc):
            return loc
    return None


def _run_spotbugs(jar_path: str, build_loc: str, class_name: str, output_xml: str) -> bool:
    """Run SpotBugs and write XML report. Returns True on success."""
    try:
        result = subprocess.run(
            [
                "java", "-jar", jar_path,
                "-textui",
                "-xml:withMessages",
                "-output", output_xml,
                "-onlyAnalyze", class_name,
                build_loc,
            ],
            capture_output=True,
            timeout=60,
        )
        return os.path.isfile(output_xml)
    except Exception as e:
        logging.warning(f"SpotBugs execution failed: {e}")
        return False


def _parse_spotbugs_xml(xml_path: str, source_file: str):
    """
    Parse SpotBugs XML and return (start_line, end_line, bug_types) for
    the best bug instance, or (0, 0, []) if nothing found.
    """
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return 0, 0, []

    fname = os.path.basename(source_file)
    best_start, best_end, bug_types = 0, 0, []

    for bug in tree.getroot().findall("BugInstance"):
        bug_type = bug.get("type", "")
        for sl in bug.findall(".//SourceLine"):
            if sl.get("sourcefile", "") != fname:
                continue
            start = int(sl.get("start", "0") or "0")
            end = int(sl.get("end", "0") or "0")
            if start > 0:
                bug_types.append(bug_type)
                if best_start == 0 or start < best_start:
                    best_start = start
                if end > best_end:
                    best_end = end

    return best_start, best_end, bug_types


# ── Fallback: method range from stack trace ───────────────────────────────────

def _method_range_from_trace(info: dict):
    """
    Extract the line range of the method that appears in the failing stack trace.
    Uses the buggy source file to find the method's start and end braces.
    """
    # Prefer the raw failing_tests file (has full stack traces with line numbers)
    failing_tests_path = os.path.join(
        info["project_meta"]["checkout_dir"],
        f"{info['project_meta']['bug_name']}_buggy",
        "failing_tests",
    )
    if os.path.isfile(failing_tests_path):
        with open(failing_tests_path) as f:
            failing = f.read()
    else:
        failing = info.get("failing_test_cases", "")
    buggy_code = info.get("raw_code", "") or info.get("buggy_code", "")
    if not failing or not buggy_code:
        return 0, 0

    # Find lowest line number in the buggy program from the stack trace
    data_name = info["project_meta"].get("data_name", "d4j")
    if data_name == "quixbugs":
        prog = info["project_meta"]["project_name"]
        prefix = f"java_programs.{prog}"
    else:
        prefix = info["project_meta"].get("project_name", "")

    trace_lines = []
    for line in failing.splitlines():
        if prefix in line and line.strip().startswith("at "):
            m = re.search(r':(\d+)\)', line)
            if m:
                trace_lines.append(int(m.group(1)))

    if not trace_lines:
        return 0, 0

    anchor = min(trace_lines)  # first executed line in the program
    code_lines = buggy_code.splitlines()

    # Walk backward from anchor to find method start (line with '{')
    start = max(0, anchor - 1)  # convert to 0-indexed
    while start > 0 and "{" not in code_lines[start]:
        start -= 1
    # Walk backward further to include the method signature
    sig_start = start
    while sig_start > 0 and not re.search(r'\b(public|private|protected|static)\b', code_lines[sig_start]):
        sig_start -= 1

    # Walk forward from anchor to find method end (closing brace)
    depth = 0
    end = start
    for i in range(start, len(code_lines)):
        depth += code_lines[i].count("{") - code_lines[i].count("}")
        if depth <= 0 and i > start:
            end = i
            break

    return sig_start + 1, end + 1  # 1-indexed


# ── Public API ────────────────────────────────────────────────────────────────

def run_static_analysis(info: dict, tools_dir: str = None) -> dict:
    """
    Run SpotBugs on the buggy file's compiled class; fall back to method-range
    extraction from the failing stack trace.

    Returns:
        {
          "hint": str,    # ready to prepend to Slicer prompt
          "start": int,   # 1-indexed start line (0 = unknown)
          "end": int,     # 1-indexed end line   (0 = unknown)
        }
    """
    if tools_dir is None:
        tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools")

    start, end, bug_types = 0, 0, []
    source = "stack-trace"

    jar = _find_spotbugs_jar(tools_dir)
    build_loc = _build_location(info)
    class_name = _class_name_for_file(info)

    if jar and build_loc and class_name:
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
            xml_path = tmp.name
        try:
            if _run_spotbugs(jar, build_loc, class_name, xml_path):
                buggy_path = info["project_meta"].get("buggy_file_path", "")
                s, e, types = _parse_spotbugs_xml(xml_path, buggy_path)
                if s > 0:
                    start, end, bug_types, source = s, e, types, "SpotBugs"
                    logging.info(f"SpotBugs found {len(types)} bug(s): {types}")
        finally:
            try:
                os.unlink(xml_path)
            except OSError:
                pass

    # Fallback: method range from stack trace
    if start == 0:
        start, end = _method_range_from_trace(info)
        source = "stack-trace method extraction"
        logging.info(f"SpotBugs fallback: method range lines {start}-{end}")

    # Build hint text
    hint_lines = []
    if bug_types:
        hint_lines.append(f"SpotBugs detected: {', '.join(set(bug_types))} (lines {start}–{end})")
    if start > 0:
        hint_lines.append(f"Suspected region: lines {start}–{end} [{source}]")

    return {
        "hint": "\n".join(hint_lines),
        "start": start,
        "end": end,
    }
