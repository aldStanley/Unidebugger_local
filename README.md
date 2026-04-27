# UniDebugger-Local

A fully local variant of the [UniDebugger](https://arxiv.org/abs/2404.17153) automated program repair framework. Replaces all external paid services with free, offline alternatives while preserving the hierarchical L1→L2→L3 repair architecture.

## Changes from Original

| Component | Original | This Implementation |
|-----------|----------|-------------------|
| Helper agent | Tavily Search API | Tree-sitter local RAG |
| Slicer agent | SonarQube CE | SpotBugs 4.8.6 |
| Locator agent | GZoltar (full) | Stack-trace SBFL (Ochiai) |
| Benchmark | Defects4J | QuixBugs (Java) |

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

### QuixBugs benchmark

Clone the QuixBugs repository alongside this repo:
```bash
git clone https://github.com/jkoppel/QuixBugs.git ../QuixBugs
```

## Setup

```bash
python setup_quixbugs.py --quixbugs_repo ../QuixBugs --output benchmarks/quixbugs
```

## Running

```bash
# Level 1 (Locator + Fixer)
python src/pipeline.py --data_name quixbugs --level 1

# Level 2 (+ Slicer + Summarizer + FixerPro)
python src/pipeline.py --data_name quixbugs --level 2

# Level 3 (+ Helper + RepoFocus)
python src/pipeline.py --data_name quixbugs --level 3
```

## Evaluation

```bash
# Plausible fix rate (automated)
# Results are written to res/quixbugs/records/<hash>/plausible.txt

# Correct fix rate (compare against QuixBugs ground truth)
python evaluate_correct_fixes.py               # all levels
python evaluate_correct_fixes.py ac680dac      # specific hash
python evaluate_correct_fixes.py ac680dac -v   # with side-by-side diff
```

## Results (QuixBugs, gpt-4o)

| Level | Plausible | Fix Rate | Cost |
|-------|-----------|----------|------|
| L1    | 30 / 40   | 75.0%    | $0.87 |
| L2    | 34 / 40   | 85.0%    | $2.29 |
| L3    | 36 / 40   | 90.0%    | $2.75 |
