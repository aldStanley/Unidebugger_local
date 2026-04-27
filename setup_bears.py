#!/usr/bin/env python3
"""
setup_bears.py — Set up Bears benchmark for UniDebugger-Local.

Creates the checkout structure expected by the pipeline:
  benchmarks/bears/
  ├── root_cause_path.json       bug_name → relative path of buggy file
  └── checkouts/
      └── Bears_<N>_buggy/      full Maven project at the buggy commit
          ├── src/
          ├── pom.xml
          ├── bears.json
          └── failing_tests      D4J-format failing test output

Bears branch naming: org-project-buggyBuildId-fixerBuildId  (e.g. traccar-traccar-188473748-188474474)
Metadata file on each branch: bears.json
Only "failing_passing" type bugs are used (tests fail on buggy, pass on fix).

Usage:
    # Use the pre-cloned Bears repo and set up the default curated 10 bugs:
    python setup_bears.py --output benchmarks/bears

    # Specify a different Bears clone location:
    python setup_bears.py --bears_repo ~/bears-benchmark --output benchmarks/bears

    # Set up specific bug IDs (numeric Bears IDs, e.g. 98 102 157):
    python setup_bears.py --bugs 98 102 --output benchmarks/bears

Requirements:
    - git on PATH
    - mvn (Maven) on PATH  (only needed if --run-tests is specified)
    - Java configured for your shell
"""

import os
import sys
import json
import shutil
import subprocess
import argparse
import tempfile
import xml.etree.ElementTree as ET

BEARS_REPO_URL = "https://github.com/bears-bugs/bears-benchmark"

# Curated list: (bugId_int, branch_name, relative_buggy_file)
# All are failing_passing type with a single changed file — good for L3 evaluation.
CURATED_BUGS = [
    (98,  "traccar-traccar-188473748-188474474",
          "src/org/traccar/protocol/GoSafeProtocolDecoder.java"),
    (102, "traccar-traccar-201008628-201013389",
          "src/org/traccar/protocol/TeltonikaProtocolDecoder.java"),
    (157, "CorfuDB-CorfuDB-330246430-330267605",
          "runtime/src/main/java/org/corfudb/util/NodeLocator.java"),
    (209, "DmitriiSerikov-money-transfer-service-446104441-446106577",
          "src/main/java/com/github/example/holder/impl/LockHolderImpl.java"),
    (193, "Activiti-activiti-cloud-app-service-459060444-459062447",
          "activiti-cloud-app-service/src/main/java/org/activiti/cloud/app/model/Application.java"),
    (195, "apache-incubator-dubbo-450828157-450884276",
          "dubbo-config/dubbo-config-api/src/main/java/com/alibaba/dubbo/config/AbstractInterfaceConfig.java"),
    (201, "brettwooldridge-HikariCP-446097106-446106182",
          "src/main/java/com/zaxxer/hikari/pool/PoolBase.java"),
    (226, "milaboratory-milib-444855015-444858326",
          "src/main/java/com/milaboratory/core/alignment/kaligner2/KAligner2.java"),
    (233, "pippo-java-pippo-446336779-446341967",
          "pippo-session-parent/pippo-session/src/main/java/ro/pippo/session/EncryptedSessionDataTranscoder.java"),
    (251, "webfirmframework-wff-453188520-453188718",
          "wffweb/src/main/java/com/webfirmframework/wffweb/tag/html/AbstractHtml.java"),
]

# Build lookup maps
_CURATED_BY_ID    = {bug_id: (branch, src) for bug_id, branch, src in CURATED_BUGS}
_CURATED_BY_BRANCH = {branch: (bug_id, src) for bug_id, branch, src in CURATED_BUGS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, cwd=None, check=True, capture=False, timeout=600):
    kwargs = dict(cwd=cwd, timeout=timeout)
    if capture:
        kwargs["capture_output"] = True
    return subprocess.run(cmd, **kwargs, check=check)


def git(args, cwd, **kwargs):
    return run(["git"] + args, cwd=cwd, **kwargs)


def clone_bears(dest: str):
    print(f"Cloning Bears benchmark → {dest} (this may take a few minutes)...")
    run(["git", "clone", "--no-checkout", "--filter=blob:none", BEARS_REPO_URL, dest])
    print("Clone done.")


def extract_project(bears_repo: str, branch: str, dest_dir: str):
    """Extract the project tree at origin/<branch> into dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)
    archive = git(["archive", f"origin/{branch}"], cwd=bears_repo, capture=True)
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
        tf.write(archive.stdout)
        tar_path = tf.name
    run(["tar", "-x", "-C", dest_dir, "-f", tar_path])
    os.unlink(tar_path)


def read_bears_json(bears_repo: str, branch: str) -> dict:
    result = git(
        ["show", f"origin/{branch}:bears.json"],
        cwd=bears_repo, capture=True, check=False,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def get_failing_details(meta: dict) -> list[dict]:
    """Return list of {testClass, testMethod, failureName, detail} dicts."""
    return meta.get("tests", {}).get("failureDetails", [])


def build_failing_tests(details: list[dict]) -> str:
    """Convert Bears failureDetails to D4J-style failing_tests text."""
    lines = []
    for det in details:
        cls    = det.get("testClass", "UnknownClass")
        method = det.get("testMethod", "unknownMethod")
        err    = det.get("failureName", "")
        detail = det.get("detail", "")
        lines.append(f"--- {cls}::{method}")
        if detail:
            lines.append(f"{err}: {detail}")
        else:
            lines.append(err)
        simple = cls.split(".")[-1]
        lines.append(f"\tat {cls}.{method}({simple}.java:1)")
    return "\n".join(lines)


def run_maven_tests(checkout_dir: str, test_classes: list[str]) -> list[str]:
    """Run mvn test and return paths to generated surefire XML files."""
    if not os.path.exists(os.path.join(checkout_dir, "pom.xml")):
        return []
    selector = "+".join(test_classes) if test_classes else ""
    cmd = ["mvn", "test", "--no-transfer-progress", "-fae", "-q"]
    if selector:
        cmd += [f"-Dtest={selector}", "-DfailIfNoTests=false"]
    run(cmd, cwd=checkout_dir, check=False, timeout=300)
    xmls = []
    for root_dir, _dirs, files in os.walk(checkout_dir):
        if "surefire-reports" in root_dir:
            for f in files:
                if f.startswith("TEST-") and f.endswith(".xml"):
                    xmls.append(os.path.join(root_dir, f))
    return xmls


def surefire_xml_to_failing_tests(xml_paths: list[str], wanted_classes: set[str]) -> str:
    lines = []
    for xml_path in xml_paths:
        try:
            tree = ET.parse(xml_path)
        except ET.ParseError:
            continue
        root = tree.getroot()
        suite_cls = root.get("name", "")
        if wanted_classes and suite_cls not in wanted_classes:
            continue
        for tc in root.findall("testcase"):
            node = tc.find("failure") or tc.find("error")
            if node is None:
                continue
            method = tc.get("name", "unknown")
            classname = tc.get("classname", suite_cls)
            lines.append(f"--- {classname}::{method}")
            lines.append((node.text or "").strip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def setup_bears(bears_repo: str, output_dir: str, bug_ids: list[int], run_tests: bool):
    bears_repo = os.path.abspath(bears_repo)
    output_dir = os.path.abspath(output_dir)
    checkouts_dir = os.path.join(output_dir, "checkouts")
    os.makedirs(checkouts_dir, exist_ok=True)

    rcp_path = os.path.join(output_dir, "root_cause_path.json")
    root_cause_path = {}
    if os.path.exists(rcp_path):
        with open(rcp_path) as f:
            root_cause_path = json.load(f)

    selected = [(bid, _CURATED_BY_ID[bid][0], _CURATED_BY_ID[bid][1])
                for bid in bug_ids if bid in _CURATED_BY_ID]
    if not selected:
        print("No valid bug IDs in curated list. Available:", sorted(_CURATED_BY_ID.keys()))
        sys.exit(1)

    print(f"Setting up {len(selected)} Bears bugs → {output_dir}\n")

    for i, (bug_id, branch, buggy_file) in enumerate(selected, 1):
        bug_name    = f"Bears_{bug_id}"
        checkout_dir = os.path.join(checkouts_dir, f"{bug_name}_buggy")
        print(f"[{i}/{len(selected)}] {bug_name} (branch: {branch})")

        if bug_name in root_cause_path and os.path.exists(checkout_dir):
            print("  already done, skipping")
            continue

        # Read metadata
        meta = read_bears_json(bears_repo, branch)
        if not meta:
            print(f"  SKIP — cannot read bears.json from origin/{branch}")
            continue

        details = get_failing_details(meta)
        if not details:
            print(f"  SKIP — no failureDetails in bears.json")
            continue

        # Extract project
        if os.path.exists(checkout_dir):
            shutil.rmtree(checkout_dir)
        try:
            extract_project(bears_repo, branch, checkout_dir)
        except subprocess.CalledProcessError as e:
            print(f"  SKIP — git archive failed: {e}")
            continue

        # Verify buggy file exists
        full_buggy = os.path.join(checkout_dir, buggy_file)
        if not os.path.exists(full_buggy):
            print(f"  WARN — buggy file not found: {buggy_file}")

        failing_text = ""

        if run_tests:
            print("  running mvn test...", end=" ", flush=True)
            test_classes = list({d["testClass"] for d in details if "testClass" in d})
            xmls = run_maven_tests(checkout_dir, test_classes)
            if xmls:
                failing_text = surefire_xml_to_failing_tests(xmls, set(test_classes))
                print(f"{failing_text.count('--- ')} failing test(s) from surefire XML")
            else:
                print("no surefire XML generated")

        if not failing_text.strip():
            failing_text = build_failing_tests(details)
            print(f"  failing_tests written from metadata ({failing_text.count('--- ')} test(s))")
        else:
            print(f"  failing_tests written from surefire XML")

        with open(os.path.join(checkout_dir, "failing_tests"), "w") as f:
            f.write(failing_text)

        root_cause_path[bug_name] = buggy_file
        with open(rcp_path, "w") as f:
            json.dump(root_cause_path, f, indent=2, sort_keys=True)

        print(f"  buggy file: {buggy_file}")

    print(f"\nDone. {len(root_cause_path)} bugs in {output_dir}")
    if root_cause_path:
        print(f"Run: cd src && python pipeline.py --model_name gpt-4o --data_name bears --level 3 --limit {len(root_cause_path)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set up Bears benchmark for UniDebugger-Local")
    parser.add_argument(
        "--bears_repo", default=None,
        help="Path to a cloned bears-benchmark repo. Clones automatically if omitted.",
    )
    parser.add_argument(
        "--output", default="benchmarks/bears",
        help="Output benchmark directory (default: benchmarks/bears)",
    )
    parser.add_argument(
        "--bugs", nargs="+", type=int, default=None,
        help=f"Bug IDs to set up (default: all {len(CURATED_BUGS)} curated). "
             f"Available: {sorted(_CURATED_BY_ID.keys())}",
    )
    parser.add_argument(
        "--run-tests", action="store_true",
        help="Run 'mvn test' for each bug to get real surefire stack traces "
             "(slower but more accurate failing_tests output).",
    )
    args = parser.parse_args()

    # Resolve bears repo
    bears_repo = args.bears_repo
    if bears_repo is None:
        bears_repo = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tools", "bears-benchmark"
        )
        if not os.path.exists(os.path.join(bears_repo, ".git")):
            clone_bears(bears_repo)
    else:
        bears_repo = os.path.abspath(bears_repo)

    print("Fetching remote branches...")
    git(["fetch", "--all", "-q"], cwd=bears_repo)

    bug_ids = args.bugs if args.bugs else sorted(_CURATED_BY_ID.keys())
    setup_bears(
        bears_repo=bears_repo,
        output_dir=args.output,
        bug_ids=bug_ids,
        run_tests=args.run_tests,
    )
