# Synthetic Data for Expense Matching

A proof-of-concept pipeline for generating and validating training pairs between **client expenses** and **funding-program expenses**.

The goal is to build a dataset for adapting and evaluating expense-matching models. A correct match must satisfy the relevant business constraints; textual or semantic similarity alone is insufficient.

## Task

Given a client expense and a program expense, determine whether they form a valid business match within the applicable eligibility context.

The pipeline distinguishes between:

| Pair or case type | Description |
|---|---|
| Positive | A valid match between a client expense and a program expense. |
| Easy negative | An invalid match selected from semantically more distant candidates. |
| Hard negative | An invalid match despite strong semantic similarity. |
| Non-lexical positive | A valid match expressed using substantially different wording. |
| Multiple valid matches | A client expense correctly matches more than one program expense. |

The learning target is binary. Difficulty and non-lexical status describe examples rather than additional target classes.

Funding eligibility is handled separately from similarity: matching the description of a non-fundable expense does not make that expense eligible for funding.

## Pipeline

### 1. Positive pair generation

Program expenses serve as anchors for generating synthetic client expenses in the same expense class.

Each proposed client–anchor pair is checked by a validator. Generation intent alone does not establish a positive label.

An accepted anchor becomes a known positive for that client. Other program expenses may also be valid matches.

### 2. Semantic ranking

The pipeline uses the pretrained `intfloat/multilingual-e5-large` embedding model to rank program expenses within the same expense class.

- **Client input:** title and description, encoded as a query.
- **Program input:** category, subcategory and description, encoded as a passage.

Cosine distance is calculated as:

```text
distance = 1 - cosine_similarity
```

Smaller distances indicate greater embedding similarity. These scores guide candidate selection; they do not determine business-valid labels.

### 3. Class-specific neighbourhood calibration

For each client, the pipeline records the rank of its known positive anchor.

These ranks are aggregated by expense class to calculate coverage thresholds such as **K95**: the smallest K that includes at least 95% of the observed known positives in that class.

K95 measures empirical coverage of known anchors. It does not guarantee coverage of every valid match.

### 4. Easy-negative mining

Candidates are selected outside the class-specific K95 neighbourhood.

When no outside region exists, a fallback examines candidates from the most distant towards closer ones. The validator checks candidates, and the first confirmed invalid match is retained for that client.

Being outside K95 does not automatically make a candidate negative.

### 5. Semi-hard-negative mining

A pilot explores semantically close candidates using their distance relative to the known positive.

Candidate selection excludes known positive pairs, previously retained easy-negative pairs and non-fundable cases according to the mining rules.

A limited validation budget per client helps control API usage.

### 6. Validation and review

LLM validation checks candidate matches against the supplied business information. Cached verdicts and checkpointing support reuse and incremental processing where implemented.

LLM-validated labels are distinguished from human-reviewed labels. Manual review is required to assess label quality, particularly for ambiguous and difficult cases.

## Current Status

Implemented components include:

- Data preparation.
- Synthetic positive generation and validation.
- Same-class embedding ranking.
- Known-positive rank and coverage analysis.
- Easy-negative mining.
- A semi-hard-negative pilot.
- Validation caching and intermediate output generation.

Work in progress:

- Manual review of the semi-hard-negative pilot.
- Selection of a methodology for non-lexical positive mining and generation.
- Explicit handling of additional valid matches.
- Preparation of training and independent evaluation datasets.

The E5 model currently supports candidate selection. This repository does not claim completed fine-tuning or demonstrated downstream performance improvements.

## Design Principles

- Expense category is a hard constraint.
- Required expense fields are used together when assessing a match.
- Same-class membership alone does not establish validity.
- Low lexical overlap alone does not establish a non-lexical positive.
- Low embedding similarity does not establish a negative.
- A client may have multiple valid program matches.
- Generated variants and related examples must be handled carefully when creating training and evaluation splits.

## Technologies

- Python
- pandas and NumPy
- Multilingual E5 embeddings
- Azure OpenAI for generation and validation
- CSV and Excel outputs for analysis and review

## Limitations

- Synthetic examples may reflect the language and assumptions of the generation model.
- LLM validation can produce incorrect or uncertain labels.
- Similarity scores are not calibrated probabilities of business validity.
- Known-positive anchors do not provide an exhaustive list of valid matches.
- Coverage thresholds depend on the available examples and candidate pool.
- Non-lexical generation and downstream model evaluation remain ongoing work.
