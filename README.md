
# Synthetic Data for Expense Matching

This repository is a POC(proof of concept). The question behind it is simple: can we
build a useful training and evaluation dataset for matching **client expenses**
(what a business plans to spend money on) to **program expenses** (what a
funding program is willing to cover), mostly with the help of an LLM?

A good match is not just two texts that look alike. "Website development" and
"digital marketing services" sound related, but if the program doesn't cover
web development, it's not a match. That gap between *similar* and *valid* is
what this project is about.

## What kind of pairs we build

| Pair type | What it means |
|---|---|
| Positive | The client expense is genuinely covered by the program expense. |
| Easy negative | Not a match, and the two are clearly far apart. |
| Hard (semi-hard) negative | Not a match, even though an embedding model thinks they are close. |
| Non-lexical positive | A valid match where the client and the program share no words. |
| Multiple valid matches | One client expense can be covered by more than one program expense. |

The label is always binary: match (1) or no match (0). "Easy", "hard" and
"non-lexical" describe how difficult an example is, not extra classes.

Whether a business is *eligible* for a program (region, activity code, size)
is a separate question, checked before matching. The expense class
"Μη επιλέξιμες δαπάνες" (non-eligible expenses) was removed from the synthetic
data, because matching a description of something non-fundable doesn't make it
a positive.

## How the dataset is built

**1. Positives.** We take real program expenses from 18 funding programs and
ask GPT-4o to write realistic client expenses that each program expense would
cover. A second, independent GPT-4o prompt (the validator) then checks every
pair. Only pairs it accepts are kept: 426 positives.

**2. Ranking with embeddings.** For each client, the pretrained
`intfloat/multilingual-e5-large` model ranks all program expenses of the same
class by cosine distance. This ranking only tells us *which* pairs are worth
checking; it never decides a label.

**3. Neighbourhood size (K95).** For every class we find the smallest K that
contains 95% of the known positives. It's an empirical guide, not a guarantee.

**4. Easy negatives.** We look for candidates outside K95 and let the validator
judge them. The first candidate it rejects becomes the negative for that
client: 243 easy negatives. Candidates whose text is identical to the known
positive are skipped, since the validator would always accept them.

**5. Semi-hard negatives.** A pilot looks at candidates just slightly further
away than the known positive. These are the cases an embedding model is most
likely to get wrong: 31 semi-hard negatives so far.

**6. Non-lexical positives.** We define "non-lexical" with a simple word-overlap
score (`src/lexical_score.py`): client title and description against program
category and subcategory, after lowercasing, removing accents and stopwords,
handling Greeklish, and matching word variants by their common beginning. A
pair with a score of 0, meaning no shared words, counts as non-lexical. From the
human examples we found 153 such pairs and used them as few-shot examples for
GPT-4o to write new ones. A pilot produced 120 pairs, 89 of which really share
no words with the program.

## Evaluation and model choice

The human-labelled examples (eight Excel files, one per program) are the
ground truth. We link each of them to the exact program expense it refers to,
remove anything used as a few-shot example, and keep **535 pairs** as the
evaluation set (`src/build_evaluation_set.py`).

On that set we compared three open multilingual embedding models without any
fine-tuning (`src/compare_embedding_models.py`): `BAAI/bge-m3`,
`Qwen/Qwen3-Embedding-0.6B` and `Snowflake/snowflake-arctic-embed-l-v2.0`.
Each model ranks the program expenses for every client, and we check where the
correct one lands.

- Ranking only within the client's own program (closer to how the platform
  works), all three find the right expense first about 83–86% of the time.
- Ranking across all programs is much harder (around 26% at the top spot), partly
  because different programs describe very similar expenses.
- On the pairs with the least word overlap, `bge-m3` did best, and it was also
  the fastest. **`bge-m3` is the model we'll fine-tune.**

E5 was left out of the comparison: it truncates input at 512 tokens, which cuts
off about 37% of the program descriptions. That's the one that we used to make the
synthetic data .

## Human review

LLM labels are not ground truth. To measure how reliable they are, we prepared
an annotation file (`src/build_annotation_sample.py`) with 700 shuffled pairs:
380 positives, 210 easy negatives, 31 semi-hard negatives and 79 non-lexical
positives. Reviewers see our label and only write something when they disagree.

## Where things stand

Done:
- Data preparation and synthetic positive generation with validation
- Embedding ranking, K95 analysis, easy-negative mining
- Semi-hard-negative pilot
- Lexical score and non-lexical few-shot pilot
- Human evaluation set and pre-fine-tuning model comparison
- Annotation file for human review

Next:
- Collect the human annotations and measure agreement with our labels
- Decide on full runs for non-lexical and semi-hard negatives
- Assemble the training set and fine-tune `bge-m3`
- Compare the fine-tuned model with the pretrained one and with the
  `text-embedding-3-large` baseline

Nothing here claims a finished, fine-tuned model yet.

## Things to keep in mind

- Synthetic examples carry the style and assumptions of the model that wrote them.
- The validator can be wrong or unsure, which is why we're doing human review.
- Similarity scores are not probabilities of a valid match.
- A known positive is rarely the only valid match for a client.
- Variants of the same example must stay together when splitting data, to
  avoid leaking information between training and evaluation.

## Built with

Python, pandas, NumPy, sentence-transformers (E5, bge-m3, Qwen3, Snowflake
Arctic), Azure OpenAI (GPT-4o) for generation and validation, and Excel for
review.
