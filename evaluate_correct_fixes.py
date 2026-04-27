"""
Correct Fix Evaluator for UniDebugger-Local (QuixBugs)

For each plausible fix, parses the generated patch to extract the new lines,
applies them to the original buggy file by content-matching (bypassing the
// buggy line annotations the Locator inserts), and compares the result
against the QuixBugs ground-truth correct version.

A fix is "correct" if the patched file is semantically identical to the
correct version (modulo package declaration and pipeline annotations).
"""

import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))

HASH_TO_LEVEL = {
    "96d5c603": "L1",
    "47423f8c": "L2",
    "ac680dac": "L3",
}

# single-line annotations added by the pipeline (not in original source)
# block-comment annotations inserted by the pipeline: /* missing code:[...] ... */
_BLOCK_ANNOTATION = re.compile(r"/\*\s*missing code:.*?\*/", re.DOTALL)
# any trailing // comment (never semantic in Java; pipeline adds many variants)
_TRAILING_COMMENT = re.compile(r"\s*//.*$")


def is_pipeline_artifact(line: str) -> bool:
    """Return True if the line is purely a /* missing code:... */ annotation."""
    return bool(re.fullmatch(r"/\*\s*missing code:.*?\*/\s*", line.strip(), re.DOTALL))


def clean(line: str) -> str:
    """Strip block annotations, trailing // comments, and trailing whitespace."""
    line = _BLOCK_ANNOTATION.sub("", line)   # remove /* missing code:...*/ blocks
    line = _TRAILING_COMMENT.sub("", line)   # remove any trailing // comment
    return line.rstrip()


def normalize_line(line: str) -> str:
    """Normalize a single line: clean annotations, collapse all internal whitespace."""
    return "".join(clean(line).split())


def normalize_file(source: str) -> str:
    """
    Normalize a Java source for comparison.
    - Drop package declaration (differs between buggy and correct packages)
    - Drop import statements (correct files add cross-package imports not in buggy)
    - Clean trailing // comments and /* missing code:...*/ annotations
    - Collapse all internal whitespace so 'a % b' == 'a%b'
    - Drop blank lines to ignore formatting differences
    """
    out = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("package ") or stripped.startswith("import "):
            continue
        normed = normalize_line(line)
        if normed:  # skip blank lines
            out.append(normed)
    return "\n".join(out)


def parse_patch_hunks(patch_path: str):
    """
    Parse a unified diff and return a list of (removed_lines, added_lines) per hunk.
    Lines are cleaned of pipeline annotations.
    """
    hunks = []
    removed, added = [], []
    in_hunk = False

    with open(patch_path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("@@"):
                if in_hunk:
                    hunks.append((removed, added))
                removed, added = [], []
                in_hunk = True
            elif in_hunk:
                if line.startswith("-"):
                    content = line[1:]
                    # skip lines that are purely pipeline artifacts not in source
                    if not is_pipeline_artifact(content):
                        removed.append(clean(content))
                elif line.startswith("+"):
                    content = line[1:]
                    if not is_pipeline_artifact(content):
                        added.append(clean(content))

    if in_hunk:
        hunks.append((removed, added))
    return hunks


def apply_patch_by_content(patch_path: str, original_source: str) -> str | None:
    """
    Apply a patch to the original source by content-matching rather than
    line numbers. This is necessary because the Locator annotates the source
    with '// buggy line' before the Fixer generates its patch, so the patch
    context never matches the original file.

    Strategy per hunk:
      1. Clean removed lines (strip annotations)
      2. Find the first occurrence of those lines in the original source
      3. Replace with the added lines
    Returns the patched source, or None if any hunk fails to match.
    """
    lines = original_source.splitlines()
    hunks = parse_patch_hunks(patch_path)
    if not hunks:
        return None

    for removed, added in hunks:
        if not removed:
            # pure insertion — skip for now (very rare in QuixBugs)
            continue

        # find the block of removed lines in the current file
        needle = [clean(l) for l in removed]
        match_idx = None
        for i in range(len(lines) - len(needle) + 1):
            window = [clean(l) for l in lines[i:i + len(needle)]]
            if window == needle:
                match_idx = i
                break

        if match_idx is None:
            return None  # hunk did not match

        # preserve the indentation of the first matched line
        original_indent = len(lines[match_idx]) - len(lines[match_idx].lstrip())
        patch_indent    = len(added[0]) - len(added[0].lstrip()) if added else 0
        indent_delta    = original_indent - patch_indent

        new_lines = []
        for al in added:
            if indent_delta > 0:
                new_lines.append(" " * indent_delta + al)
            else:
                new_lines.append(al[max(0, -indent_delta):])

        lines = lines[:match_idx] + new_lines + lines[match_idx + len(removed):]

    return "\n".join(lines)


def ground_truth_change(buggy_path: str, correct_path: str):
    """Return (removed_lines, added_lines) from the ground-truth diff."""
    with open(buggy_path)   as f: buggy   = f.read().splitlines()
    with open(correct_path) as f: correct = f.read().splitlines()
    removed, added = [], []
    for b, c in zip(buggy, correct):
        bn, cn = normalize_line(b), normalize_line(c)
        if bn.startswith("package") or cn.startswith("package"):
            continue
        if bn != cn:
            removed.append(b.strip())
            added.append(c.strip())
    return removed, added


def evaluate(hash_id: str, quixbugs_repo: str, verbose: bool = False):
    level       = HASH_TO_LEVEL.get(hash_id, hash_id)
    records_dir = os.path.join(BASE, "res", "quixbugs", "records", hash_id)
    resp_dir    = os.path.join(BASE, "res", "quixbugs", "resp",    hash_id)
    checkouts   = os.path.join(BASE, "benchmarks", "quixbugs", "checkouts")
    correct_dir = os.path.join(quixbugs_repo, "correct_java_programs")

    plausible_file = os.path.join(records_dir, "plausible.txt")
    if not os.path.exists(plausible_file):
        print(f"No plausible.txt found for hash {hash_id}")
        return

    with open(plausible_file) as f:
        plausible_bugs = [l.strip() for l in f if l.strip()]

    correct        = []
    plausible_only = []
    patch_failed   = []

    for bug in plausible_bugs:
        prog         = bug.rsplit("_", 1)[0]
        checkout_dir = os.path.join(checkouts, f"{bug}_buggy")
        buggy_path   = os.path.join(checkout_dir, "java_programs", f"{prog}.java")
        correct_path = os.path.join(correct_dir, f"{prog}.java")

        if not os.path.exists(correct_path):
            patch_failed.append((bug, "no ground-truth file"))
            continue
        if not os.path.exists(buggy_path):
            patch_failed.append((bug, "no buggy source file"))
            continue

        with open(buggy_path)   as f: buggy_source   = f.read()
        with open(correct_path) as f: correct_source = f.read()

        # try fixer patch first, fall back to fixerpro
        patched_source = None
        for agent in ("fixer", "fixerpro"):
            patch_path = os.path.join(resp_dir, agent, "aim", f"{bug}.patch")
            if not os.path.exists(patch_path):
                continue
            result = apply_patch_by_content(patch_path, buggy_source)
            if result is not None:
                patched_source = result
                break

        if patched_source is None:
            patch_failed.append((bug, "hunk did not match original source"))
            continue

        if normalize_file(patched_source) == normalize_file(correct_source):
            correct.append(bug)
        else:
            plausible_only.append(bug)

    # ── Report ────────────────────────────────────────────────────────────────
    total_correct = len(correct)

    print(f"\n{'='*60}")
    print(f"Correct Fix Evaluation  —  {level} (hash: {hash_id})")
    print(f"{'='*60}")
    print(f"  Plausible fixes : {len(plausible_bugs)} / 40")
    print(f"  Correct fixes   : {total_correct} / 40  "
          f"({100*total_correct/40:.1f}%)")
    print(f"  Plausible-only  : {len(plausible_only)}")
    if patch_failed:
        print(f"  Patch errors    : {len(patch_failed)}")

    if correct:
        print(f"\n  CORRECT ({len(correct)}):")
        for b in sorted(correct):
            print(f"    + {b}")

    if plausible_only:
        print(f"\n  PLAUSIBLE-ONLY ({len(plausible_only)}):")
        for b in sorted(plausible_only):
            print(f"    ~ {b}")
            if verbose:
                prog = b.rsplit("_", 1)[0]
                buggy_path   = os.path.join(checkouts, f"{b}_buggy",
                                            "java_programs", f"{prog}.java")
                correct_path = os.path.join(correct_dir, f"{prog}.java")
                gt_rem, gt_add = ground_truth_change(buggy_path, correct_path)
                # show generated patch added lines
                gen_add = []
                for agent in ("fixer", "fixerpro"):
                    pp = os.path.join(resp_dir, agent, "aim", f"{b}.patch")
                    if os.path.exists(pp):
                        for _, a in parse_patch_hunks(pp):
                            gen_add.extend(a)
                        break
                print(f"      ground truth : {' | '.join(gt_add) or '(none)'}")
                print(f"      generated    : {' | '.join(l.strip() for l in gen_add) or '(none)'}")

    if patch_failed:
        print(f"\n  PATCH ERRORS ({len(patch_failed)}):")
        for b, reason in patch_failed:
            print(f"    ! {b}  ({reason})")

    print()
    return {"correct": correct, "plausible_only": plausible_only,
            "patch_failed": patch_failed}


if __name__ == "__main__":
    quixbugs_repo = os.path.normpath(os.path.join(BASE, "..", "QuixBugs"))
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    hashes = args if args else list(HASH_TO_LEVEL.keys())
    for h in hashes:
        evaluate(h, quixbugs_repo, verbose=verbose)
