# UniDebugger-Local

A fully local variant of the [UniDebugger](https://arxiv.org/abs/2404.17153) automated program repair framework. Replaces all external paid services with free, offline alternatives while preserving the hierarchical L1→L2→L3 repair architecture.

For implementation details, evaluation methodology, and results, see **[Final_Report.pdf](Final_Report.pdf)**.

## Component Functionalities

1) **Helper** *(L3)*: Produces a step-by-step debugging guide grounded in the buggy repo's own symbols. Indexes every `.java` file with Tree-sitter, retrieves the top-5 relevant symbols via TF-IDF/Jaccard overlap, and outputs a guide plus `path/to/File.java:linenum` references for follow-up retrieval.
2) **RepoFocus** *(L3)*: Identifies 2–6 bug-relevant source files by reasoning over the directory tree, imported packages, failing tests, and an optional coverage report. Test files are filtered from the output.
3) **Summarizer** *(L2+)*: Extracts method-signature skeletons from Java source via `javalang` AST (class names, constructor and method signatures — no bodies), then asks the LLM to produce one-line descriptions per function in `<ClassName>~<FunctionName>~<params>~<return type>~<description>` format. Falls back to full source if parsing fails.
4) **Slicer** *(L2+)*: Extracts a 50–100 line suspicious code window anchored by a SpotBugs static analysis hint, or by method-range extraction from the stack trace when SpotBugs finds no patterns. Uses the Summarizer output, Helper guide, failing tests, and coverage as context.
5) **Locator** *(L1+)*: Marks faulty lines with `// buggy line` or `// missing code:[...]` annotations. Receives GZoltar Ochiai SBFL rankings prepended to its prompt and can request full method bodies or coverage data on demand via `get_method_body` and `failing_coverage` function-calling tools.
6) **Fixer** *(L1+)*: Generates a git-diff patch for the Locator-annotated code. Automatically appends bodies of methods called on flagged lines (post-Locator enrichment) and can fetch additional method bodies via `get_method_body`. Produces one diff hunk per file for cross-file bugs.
7) **FixerPro** *(L2+)*: Re-attempts patching when the Fixer's candidate fails plausibility testing. Receives the candidate patch with a plausible/not-plausible label alongside the full agent context, and generates a revised patch without tool access.

## Requirements

### Python dependencies

```bash
pip install -r requirements.txt
```

### Tool downloads (required before running)

These are not included in the repo due to size. Download and extract to the paths shown.

#### SpotBugs 4.8.6
```bash
curl -L https://github.com/spotbugs/spotbugs/releases/download/4.8.6/spotbugs-4.8.6.zip -o spotbugs-4.8.6.zip
unzip spotbugs-4.8.6.zip -d tools/spotbugs/
```
Expected location: `tools/spotbugs/spotbugs-4.8.6/`

#### GZoltar 1.7.3
```bash
curl -L https://github.com/GZoltar/gzoltar/releases/download/v1.7.3/gzoltar-1.7.3.202203230348.zip -o gzoltar.zip
unzip gzoltar.zip -d tools/gzoltar/
```
Expected location: `tools/gzoltar/gzoltar-1.7.3.202203230348/`

### Benchmarks

**QuixBugs** — clone alongside this repo:
```bash
git clone https://github.com/jkoppel/QuixBugs.git ../QuixBugs
```

**Defects4J** — requires Java 11, Perl (`String::Interpolate`), and Subversion (for Chart bugs):
```bash
python setup_defects4j.py
```

## Setup

```bash
# QuixBugs
python setup_quixbugs.py --quixbugs_repo ../QuixBugs --output benchmarks/quixbugs

# Defects4J (checks out and compiles all 100 bugs)
python setup_d4j_evaluation.py
```

## Running

```bash
# QuixBugs
python src/pipeline.py --data_name quixbugs --level 1   # L1: Locator + Fixer
python src/pipeline.py --data_name quixbugs --level 2   # L2: + Slicer + Summarizer + FixerPro
python src/pipeline.py --data_name quixbugs --level 3   # L3: + Helper + RepoFocus

# Defects4J (runs L1→L2→L3 in one pass, escalating only on failure)
python src/pipeline.py --data_name d4j
```

## Evaluation

```bash
# Plausibility is checked automatically after each run.
# Results are written to res/<benchmark>/records/<hash>/plausible.txt

# GPT single-call baseline (Defects4J)
python baseline_gpt.py
```
