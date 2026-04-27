"""
Generate benchmarks/d4j/root_cause_path.json from local defects4j metadata.
Run from the Unidebugger/ directory:
    python generate_root_cause.py --d4j_path ../defects4j --projects Lang Math Chart
"""
import os
import csv
import json
import argparse

D4J_PROJECTS = [
    "Chart", "Cli", "Closure", "Codec", "Collections", "Compress",
    "Csv", "Gson", "JacksonCore", "JacksonDatabind", "JacksonXml",
    "Jsoup", "JxPath", "Lang", "Math", "Mockito", "Time",
]

def get_src_dir(d4j_path, project, buggy_commit):
    layout_path = os.path.join(d4j_path, "framework/projects", project, "dir-layout.csv")
    if not os.path.exists(layout_path):
        return "src/main/java"
    with open(layout_path) as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) >= 2 and parts[0].strip() == buggy_commit:
                return parts[1].strip()
    return "src/main/java"

def class_to_path(src_dir, fqcn):
    return os.path.join(src_dir, fqcn.replace(".", "/") + ".java")

def build_root_cause(d4j_path, projects):
    result = {}
    for project in projects:
        proj_dir = os.path.join(d4j_path, "framework/projects", project)
        active_bugs = os.path.join(proj_dir, "active-bugs.csv")
        modified_dir = os.path.join(proj_dir, "modified_classes")

        if not os.path.exists(active_bugs) or not os.path.exists(modified_dir):
            print(f"Skipping {project}: missing metadata")
            continue

        with open(active_bugs) as f:
            reader = csv.DictReader(f)
            for row in reader:
                bug_id = row["bug.id"].strip()
                buggy_commit = row["revision.id.buggy"].strip()

                mod_file = os.path.join(modified_dir, f"{bug_id}.src")
                if not os.path.exists(mod_file):
                    continue

                with open(mod_file) as mf:
                    classes = [l.strip() for l in mf.read().splitlines() if l.strip()]
                if not classes:
                    continue

                src_dir = get_src_dir(d4j_path, project, buggy_commit)
                bug_name = f"{project}_{bug_id}"
                result[bug_name] = class_to_path(src_dir, classes[0])

        print(f"{project}: {sum(1 for k in result if k.startswith(project))} bugs")

    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--d4j_path", default="../defects4j", help="Path to defects4j repo")
    parser.add_argument("--projects", nargs="+", default=D4J_PROJECTS, help="Projects to include")
    parser.add_argument("--out", default="benchmarks/d4j/root_cause_path.json")
    args = parser.parse_args()

    d4j_path = os.path.abspath(args.d4j_path)
    result = build_root_cause(d4j_path, args.projects)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)
    print(f"\nWrote {len(result)} bugs to {args.out}")
