import os
import logging
import re
import subprocess
from typing import Optional
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parse import *


class NotPatchError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message
        print(message)

def split_multi_file_patch(patch: str) -> dict:
    """Split a multi-file git diff into {rel_file_path: hunk_str}. Returns {} for single-file patches without a diff --git header."""
    hunks = {}
    current_file = None
    current_lines = []
    for line in patch.splitlines(keepends=True):
        m = re.match(r'^diff --git a/(.*?) b/', line)
        if m:
            if current_file and current_lines:
                hunks[current_file] = "".join(current_lines)
            current_file = m.group(1)
            current_lines = [line]
        elif current_file:
            current_lines.append(line)
    if current_file and current_lines:
        hunks[current_file] = "".join(current_lines)
    return hunks

def format_patch_lines(patch: str) -> list[str]:
    return re.compile(r'(@@[\s\d\+\-\,]+@@)(\s+[a-zA-Z]+)').sub(r'\1\n\2', patch).splitlines()

def format_code(code_lines: list[str]) -> list[str]: #rule-based nomarlization
    for code_idx, line in enumerate(code_lines):
        concat = False
        for k in ["public, private", "protected"]: 
            if line.replace(" ", "") == k:
                concat = True
                code_lines[code_idx] = ""
                code_lines[code_idx + 1] = k + " " + code_lines[code_idx + 1].lstrip()
                break
        
        if not concat and code_idx < len(code_lines) - 1 and not ("class" in line) and \
            (not line.strip().startswith("*")) and ("//" not in line and "/*" not in line) and \
            re.search("r'[a-zA-Z0-9]$'", line):
            print("![concat]!", code_idx, code_lines[code_idx].rstrip(), "<+>", code_lines[code_idx + 1].lstrip())
            code_lines[code_idx] = line.rstrip() + " " + code_lines[code_idx + 1].lstrip()
            code_lines[code_idx + 1] = ""
    
    return code_lines

def is_a_patch(patch_lines: list[str]) -> bool:
    a = any(re.match(r'^@@\s-\d+,\d+\s\+\d+,\d+\s@@.*', line.strip()) for line in patch_lines)
    if not a:
        print("$$$$$ no @@")
        print("\n".join(patch_lines))
        return False
    b = any(re.match(r'^[-+](\s|\t)+.*$', line.strip()) for line in patch_lines)
    if not b:
        print("$$$$$ no -+")
        print("\n".join(patch_lines))
        return False
    return a and b
        
def find_a_matched_line(pidx: int, pline: str, code_lines: list[str], patch_lines: list[str], lag=-1, existing=False) -> int:
    matched = matching_lines(pline, code_lines)
    if lag >= 0:
        matched = [m for m in matched if m > lag]
    if len(matched) == 1:
        return matched[0]
    
    match_perfect = matching_with_comments(pline, matched, code_lines) # Consider comments
    if len(match_perfect) == 1:
        return match_perfect[0]

    return unique_matching(patch_lines, code_lines, pidx, resp_cur_line=pline, existing=existing)


def patching(patch: str, raw_code_lines: list[str]) -> str: # return patched code
    patch_lines = format_patch_lines(patch)
    if not is_a_patch(patch_lines):
        raise NoCodeError(f"Not a patch!\n{patch}")
    assert isinstance(raw_code_lines, list)
    
    code_lines = format_code(raw_code_lines)
    # chg: old line -> new line
    # del: old line -> ''
    # add: old line -> old line \n added_line
    patched = [l for l in code_lines] # return patched code
    unpatched_lines = []
    to_patch_num = 0
    replace_idx, pre_patch_idx = -1, -1 #record the previous position of modification

    for pidx, pline in enumerate(patch_lines):
        if re.search(r'^[-](\s|\t)+.*$', pline): # del sth
            to_patch_num += 1
            if pre_patch_idx >= 0 and pre_patch_idx + 1 == pidx and patch_lines[pre_patch_idx][0] == '-': #del multi lines
                    if replace_idx + 1 < len(patched):
                        patched[replace_idx + 1] = ""
                        replace_idx, pre_patch_idx = replace_idx + 1, pidx
                    else:
                        logging.warning(f"Cannot patch (out of range) {pline}!")
                        unpatched_lines.append((pidx, pline))
            else:
                match_idx = find_a_matched_line(pidx, pline[1:].lstrip(), code_lines, patch_lines, lag=replace_idx, existing=True)
                if match_idx > 0:
                    patched[match_idx] = ""
                    replace_idx, pre_patch_idx = match_idx, pidx
                else:
                    logging.warning(f"Cannot patch {pline}!")
                    unpatched_lines.append((pidx, pline))
        elif re.search(r'^[+](\s|\t)+.*$', pline): # add sth
            to_patch_num += 1
            if pre_patch_idx >= 0 and pre_patch_idx + 1 == pidx: # some changes on just the last line
                if patch_lines[pre_patch_idx][0] == '-': # pre is -, cur is +, meaning cur line replaces pre line
                    patched[replace_idx] = pline[1:].rstrip()
                elif patch_lines[pre_patch_idx][0] == '+': #pre is +，cur is +，meaning using multi lines to replace a pre line
                    if isinstance(patched[replace_idx], str):
                        patched[replace_idx] += ("\n" + pline[1:].rstrip())
                    elif isinstance(patched[replace_idx], list):
                        patched[replace_idx] = patched[replace_idx][:-1] + [pline[1:].rstrip(), patched[replace_idx][-1]]
                    else:
                        raise TypeError(f"{patched[replace_idx]} in unsupported type {type(patched[replace_idx])}")
                pre_patch_idx = pidx
            else: # directly adding
                print("\n--> Patch + this", patch_lines[pidx], "\n")
                # can the previous valid line has a match?
                pre_valid = search_valid_line(patch_lines, pidx, "pre")
                if pre_valid is not None:
                    unique_idx = find_a_matched_line(pre_valid[0], pre_valid[1], code_lines, patch_lines, lag=replace_idx)
                    if unique_idx >= 0:
                        patched[unique_idx] += ("\n" + pline[1:].rstrip())
                        replace_idx, pre_patch_idx = unique_idx, pidx
                        continue
                post_valid = search_valid_line(patch_lines, pidx, "post", existing=code_lines)
                if post_valid is not None:
                    unique_idx = find_a_matched_line(post_valid[0], post_valid[1], code_lines, patch_lines, lag=replace_idx)
                    if unique_idx >= 0:
                        existing_val = patched[unique_idx] if isinstance(patched[unique_idx], list) else [patched[unique_idx]]
                        patched[unique_idx] = [pline[1:].rstrip()] + existing_val
                        replace_idx, pre_patch_idx = unique_idx, pidx
                        continue
                # Cannot find neibors
                logging.warning(f"Cannot patch! {pline}")
                unpatched_lines.append((pidx, pline))
    
    if len(unpatched_lines) == to_patch_num:
        raise NotPatchError("")
    
    if len(unpatched_lines) > 0:
        for (pidx, pline) in unpatched_lines:
            print(f"Unpatched lines: #{pidx}\t{pline}")
    
    res = ""
    for p in patched:
        if len(p) == 0: continue
        if isinstance(p, str):
            res += "\n" + p
        elif isinstance(p, list):
            res += "\n" + "\n".join(p)
        else:
            raise TypeError(f"{p} in unsupported type {type(p)}")
    return res.strip()


def testing(root_test_dir: str, container)-> int: # return the number of failing test cases
    env_vars = {'JAVA_TOOL_OPTIONS': '-Dfile.encoding=UTF8'}
    logging.info("# Compiling...")
    compile_result = container.exec_run("sh -c 'export JAVA_HOME=/usr/lib/jvm/java-8-openjdk-arm64 && defects4j compile'", 
                                        workdir=root_test_dir, environment=env_vars).output.decode('utf-8')
    if "BUILD FAILED" in compile_result:
        logging.warning(f"Compile Failed\n{compile_result}")
        return -1
    
    logging.info("# Testing...")
    signal.signal(signal.SIGALRM, timeout_handler)  
    signal.alarm(5*60)
    try:
        test_result = container.exec_run("sh -c 'export JAVA_HOME=/usr/lib/jvm/java-8-openjdk-arm64 && defects4j test'", 
                                     workdir=root_test_dir, stderr=True, stdout=True).output.decode('utf-8')
        signal.alarm(0)
    except TimeoutException:
        logging.warning("Timeout!")
        return -1
    except Exception as e:
        logging.error(f"Errors during testing: {e}")

    re_match = re.search(r'Failing tests:\s*(\d+)', test_result)
    if re_match:
        os.remove("buggy.java"); os.remove("patched.java")
        return int(re_match.group(1))
    else:
        logging.error(f"Test results\n{test_result}")
        print("@"*50)
        exit()
    
def _find_gradle() -> str:
    wrapper_dists = os.path.expanduser("~/.gradle/wrapper/dists")
    if not os.path.isdir(wrapper_dists):
        raise FileNotFoundError("~/.gradle/wrapper/dists not found")
    for dist_name in sorted(os.listdir(wrapper_dists), reverse=True):
        if not (dist_name.startswith("gradle-8") or dist_name.startswith("gradle-7")):
            continue
        for hash_dir in os.listdir(os.path.join(wrapper_dists, dist_name)):
            gradle_dir = dist_name.replace("-all", "").replace("-bin", "")
            gradle_bin = os.path.join(wrapper_dists, dist_name, hash_dir, gradle_dir, "bin", "gradle")
            if os.path.isfile(gradle_bin):
                return gradle_bin
    raise FileNotFoundError("No Gradle 7/8 found in ~/.gradle/wrapper/dists")


def patching_and_testing_quixbugs(patch: str, project_meta: dict) -> bool:
    quixbugs_repo = project_meta["quixbugs_repo"]
    program_name = project_meta["project_name"]

    test_class = f"java_testcases.junit.{program_name}_TEST"
    try:
        gradle_bin = _find_gradle()
    except FileNotFoundError as e:
        logging.error(str(e))
        return None

    # Resolve file(s) to patch
    hunks = split_multi_file_patch(patch)
    if not hunks:
        # Single-file patch without diff --git header — use the primary program file
        hunks = {f"java_programs/{program_name}.java": patch}

    originals = {}
    try:
        for rel_path, hunk in hunks.items():
            # Resolve to an absolute path in the quixbugs repo
            candidate = os.path.join(quixbugs_repo, rel_path)
            if not os.path.exists(candidate):
                candidate = os.path.join(quixbugs_repo, "java_programs", os.path.basename(rel_path))
            if not os.path.exists(candidate):
                logging.warning(f"Cannot find file for hunk: {rel_path}")
                continue
            with open(candidate, encoding="utf-8") as f:
                originals[candidate] = f.read()
            try:
                patched_code = patching(hunk, originals[candidate].splitlines())
            except (NoCodeError, NotPatchError) as e:
                logging.warning(f"Cannot patch {rel_path}: {e}")
                continue
            with open(candidate, "w", encoding="utf-8") as f:
                f.write(patched_code)

        result = subprocess.run(
            [gradle_bin, "test", "--tests", test_class, "--rerun-tasks"],
            cwd=quixbugs_repo,
            capture_output=True,
            timeout=180,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logging.warning(f"Gradle test timed out for {program_name}")
        return None
    finally:
        for path, original in originals.items():
            with open(path, "w", encoding="utf-8") as f:
                f.write(original)


def patching_and_testing_bears(patch: str, project_meta: dict) -> bool:
    """Apply patch, run mvn test on the Bears checkout, restore original."""
    import xml.etree.ElementTree as ET

    checkout_dir = os.path.join(
        project_meta["checkout_dir"],
        f"{project_meta['bug_name']}_buggy",
    )

    # Derive failing test class names from failing_tests file
    failing_tests_path = os.path.join(checkout_dir, "failing_tests")
    test_classes = []
    if os.path.exists(failing_tests_path):
        with open(failing_tests_path) as f:
            for line in f:
                if line.startswith("--- "):
                    cls = line.replace("--- ", "").split("::")[0].strip()
                    if cls and cls not in test_classes:
                        test_classes.append(cls)

    cmd = ["mvn", "test", "--no-transfer-progress", "-fae", "-q"]
    if test_classes:
        cmd += [f"-Dtest={'+'.join(test_classes)}", "-DfailIfNoTests=false"]

    # Resolve file(s) to patch
    hunks = split_multi_file_patch(patch)
    if not hunks:
        hunks = {os.path.relpath(project_meta["buggy_file_path"], checkout_dir): patch}

    originals = {}
    try:
        for rel_path, hunk in hunks.items():
            candidate = os.path.join(checkout_dir, rel_path)
            if not os.path.exists(candidate):
                candidate = project_meta["buggy_file_path"]
            if not os.path.exists(candidate):
                logging.warning(f"Cannot find file for hunk: {rel_path}")
                continue
            with open(candidate, encoding="utf-8") as f:
                originals[candidate] = f.read()
            try:
                patched_code = patching(hunk, originals[candidate].splitlines())
            except (NoCodeError, NotPatchError) as e:
                logging.warning(f"Cannot patch {rel_path}: {e}")
                continue
            with open(candidate, "w", encoding="utf-8") as f:
                f.write(patched_code)

        result = subprocess.run(cmd, cwd=checkout_dir, capture_output=True, timeout=600)
        if result.returncode != 0:
            return False
        for root_dir, _, files in os.walk(os.path.join(checkout_dir, "target", "surefire-reports")):
            for fname in files:
                if not (fname.startswith("TEST-") and fname.endswith(".xml")):
                    continue
                try:
                    tree = ET.parse(os.path.join(root_dir, fname))
                    if tree.getroot().get("failures", "0") != "0" or tree.getroot().get("errors", "0") != "0":
                        return False
                except ET.ParseError:
                    pass
        return True
    except subprocess.TimeoutExpired:
        logging.warning(f"Maven test timed out for {project_meta['bug_name']}")
        return None
    finally:
        for path, original in originals.items():
            with open(path, "w", encoding="utf-8") as f:
                f.write(original)


def _find_java11() -> Optional[str]:
    """Return a JAVA_HOME path for Java 11 if one is installed, else None."""
    try:
        r = subprocess.run(
            ["/usr/libexec/java_home", "-v", "11"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.decode("utf-8").strip()
    except FileNotFoundError:
        pass
    candidates = [
        "/opt/homebrew/opt/openjdk@11",
        "/usr/local/opt/openjdk@11",
        "/usr/lib/jvm/java-11-openjdk-arm64",
        "/usr/lib/jvm/java-11-openjdk-amd64",
    ]
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


def _find_d4j_bin() -> str:
    """Return the defects4j binary path, checking tools/defects4j first then PATH."""
    import shutil
    local = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "defects4j", "framework", "bin", "defects4j",
    )
    if os.path.exists(local):
        return local
    found = shutil.which("defects4j")
    return found if found else "defects4j"


def patching_and_testing_d4j(patch: str, project_meta: dict) -> bool:
    """Apply patch, run defects4j compile+test locally (no Docker), restore original."""
    buggy_file = project_meta["buggy_file_path"]
    checkout_dir = os.path.join(
        project_meta["checkout_dir"],
        f"{project_meta['bug_name']}_buggy",
    )

    with open(buggy_file, encoding="utf-8") as f:
        original = f.read()

    try:
        patched_code = patching(patch, original.splitlines())
    except (NoCodeError, NotPatchError) as e:
        logging.warning(f"Cannot patch: {e}")
        return None

    env = os.environ.copy()
    java11 = _find_java11()
    if java11:
        env["JAVA_HOME"] = java11
        env["PATH"] = os.path.join(java11, "bin") + os.pathsep + env.get("PATH", "")
    perl5_lib = os.path.join(os.path.expanduser("~"), "perl5", "lib", "perl5")
    if os.path.isdir(perl5_lib):
        existing = env.get("PERL5LIB", "")
        env["PERL5LIB"] = perl5_lib + (os.pathsep + existing if existing else "")
    d4j_bin = _find_d4j_bin()

    try:
        with open(buggy_file, "w", encoding="utf-8") as f:
            f.write(patched_code)

        compile_res = subprocess.run(
            [d4j_bin, "compile"], cwd=checkout_dir,
            capture_output=True, env=env, timeout=120,
        )
        compile_out = compile_res.stdout.decode("utf-8", errors="replace")
        if compile_res.returncode != 0 or "BUILD FAILED" in compile_out:
            logging.warning(f"Compile failed for {project_meta['bug_name']}")
            return False

        test_res = subprocess.run(
            [d4j_bin, "test"], cwd=checkout_dir,
            capture_output=True, env=env, timeout=300,
        )

        test_out = test_res.stdout.decode("utf-8", errors="replace")
        m = re.search(r"Failing tests:\s*(\d+)", test_out)
        if m:
            return int(m.group(1)) == 0
        logging.error(f"Cannot parse test output:\n{test_out}")
        return None
    except subprocess.TimeoutExpired:
        logging.warning(f"Test timed out for {project_meta['bug_name']}")
        return None
    finally:
        with open(buggy_file, "w", encoding="utf-8") as f:
            f.write(original)


def patching_and_testing(patch: str, project_meta: dict, container_id='7bdc33a65712') -> bool: # Pass testing?
    if project_meta.get("data_name") == "quixbugs":
        return patching_and_testing_quixbugs(patch, project_meta)
    if project_meta.get("data_name") == "bears":
        return patching_and_testing_bears(patch, project_meta)
    if project_meta.get("data_name") == "d4j":
        return patching_and_testing_d4j(patch, project_meta)

    import docker
    client = docker.from_env()
    container = client.containers.get(container_id)
    
    
    main_dir = "/defects4j"
    bug_name = project_meta['project_name'] + "_" + str(project_meta['buggy_number'])
    project_dir = os.path.join(project_meta['checkout_dir'], f"{bug_name}_buggy")

    logging.info("# Checking out...")
    container.exec_run(f"rm -rf {project_dir}", workdir=main_dir)
    container.exec_run(f"defects4j checkout -p {project_meta['project_name']} -v {project_meta['buggy_number']}b -w {project_dir}", workdir=main_dir)
    
    code_undecode = container.exec_run(f"cat {project_meta['buggy_file_path']}", workdir=main_dir).output # buggy code
    try:
        buggy_code = code_undecode.decode('utf-8')
    except UnicodeDecodeError:
        buggy_code = code_undecode.decode('latin-1')
    with open(f"buggy.java", "w") as wf:
        wf.write(buggy_code)

    logging.info("# Getting patched code...")
    try:
        patched_code = patching(patch, buggy_code.splitlines())
    except (NoCodeError, NotPatchError) as e:
        logging.warning(f"Cannot patch this patch because {e}")
        return None
    with open(f"patched.java", "w") as wf:
        wf.write(patched_code)
    
    logging.info("# Patching back...") # copy patched code to container
    subprocess.run(["docker", "cp", f"patched.java", f"{container_id}:"+os.path.join(main_dir, project_meta['buggy_file_path'])], check=True)
    
    return testing(os.path.join(main_dir, project_dir), container) == 0

if __name__ == "__main__":
    patch = '''
    @@ -941,7 +941,7 @@
-   head
+   add_0 // missing code
+   add_1 
+   add_2
+   add_3
    post neibor, degree == 1
    post neibor, degree == 2
    post neibor, degree == 3
    post neibor, degree == 4
    post neibor, degree == 5
    end
    '''
    raw_code = '''
    head
    post neibor, degree == 1
    post neibor, degree == 2
    post neibor, degree == 3
    post neibor, degree == 4
    post neibor, degree == 5
    end
    ==================
    head
    post neibor, degree == 1
    post neibor, degree == 2
    post neibor, degree == 3
    post neibor, degree == 4
    end
    ==================
    head
    pre neibor, degree == 3
    pre neibor, degree == 2
    pre neibor, degree == 1
    my buggy code // three
    post neibor, degree == 1
    post neibor, degree == 2
    post neibor, degree == 3
    end
    ==================
    head
    pre neibor, degree == 2
    pre neibor, degree == 1
    my buggy code // four
    post neibor, degree == 1
    post neibor, degree == 2
    end
    ==================
    '''

    patched = patching(patch, raw_code_lines=raw_code.splitlines())
    print("@@@@@@@@@@@")
    print(patched)


