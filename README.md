
# Synthetic Data for Expense Matching

This repository is a POC (proof of concept). The question behind it is simple:
can we build a useful training and evaluation dataset for matching **client
expenses** (what a business plans to spend money on) to **program expenses**
(what a funding program is willing to cover), mostly with the help of an LLM,
and use it to fine-tune an embedding model?

A good match is not just two texts that look alike. "Website development" and
"digital marketing services" sound related, but if the program doesn't cover
web development, it's not a match. That gap between *similar* and *valid* is
what this project is about.

## Results in short

On **268 real client expenses** that no model had seen, each model ranks the
expenses of the client's own subprogram (5 to 20 options) and we check where
the correct one lands:

| Model | Recall@1 | Recall@3 | MRR |
|---|---|---|---|
| `BAAI/bge-m3` (pretrained) | 69.8% | 92.9% | 0.81 |
| `text-embedding-3-large` | 74.3% | 96.6% | 0.85 |
| bge-m3 fine-tuned, OnlineContrastiveLoss | 47.8% | 74.6% | 0.64 |
| **bge-m3 fine-tuned, MNRL with same-subprogram negatives** | **87.3%** | **99.3%** | **0.93** |

The final model puts the correct program expense first for almost 9 out of 10
real clients, and in the top 3 almost always. All numbers (real test, synthetic
test, per program, per pair type, training) are in
[`results/final_results_summary.xlsx`](results/final_results_summary.xlsx)
(numbers only, no expense texts).

## What kind of pairs we build

| Pair type | What it means |
|---|---|
| Positive | A client expense written to be covered by the program expense. |
| Easy negative | A candidate that is far from the client expense in embedding space. |
| Semi-hard negative | A candidate an embedding model thinks is close to the client expense. |
| Non-lexical positive | A match where the client and the program share no words. |

The label is always binary: match (1) or no match (0). The pair type records
**where a pair came from**, not its label: many semi-hard candidates turned out
to be valid matches when checked against the guideline (one client expense can
be covered by more than one program expense).

Whether a business is *eligible* for a program (region, activity code, size,
aid scheme) is a separate question, checked before matching. The expense class
"Μη επιλέξιμες δαπάνες" (non-eligible expenses) was removed from the synthetic
data.

## Matching guideline (v0.5)

All labels follow one written guideline (`prompts/matching_guidelines_v0.5.md`).
Each decision is MATCH or NO_MATCH with a confidence (high, medium, low), plus
the rule that applied. Key rules: the subcategory defines the expense (R8); a
required property such as "electric" or "new hire" must be stated (R9); what
the client does with the object (buy, lease, repair) doesn't change the expense
(R10); explicit exclusions win (R11); mixed or very general client expenses are
MATCH with low confidence (R12); residual expenses cover only the same theme (R13).

## How the dataset is built

**1. Positives.** We take real program expenses from 18 funding programs and
ask GPT-4o to write realistic client expenses that each program expense would
cover. A first set was checked by a GPT-4o validator; in v0.5 we also generated
positives for program expenses that had none.

**2. Candidate negatives.** For each synthetic client, the pretrained
`intfloat/multilingual-e5-large` ranks program expenses of the same class.
The nearest ones become semi-hard candidates, one outside the K95 neighbourhood
(the smallest K that contains 95% of the known positives) becomes an easy
candidate. The ranking only proposes candidates; it never decides a label.

**3. Non-lexical positives.** A word-overlap score (`src/lexical_score.py`)
marks pairs that share no words after normalisation. The 153 human examples
with score 0 were used as few-shot examples for GPT-4o to write new clients.

**4. Labels.** Every v0.5 pair (positives, negatives, non-lexical) was labelled
by Claude against guideline v0.5. Low-confidence pairs were dropped. Two older
samples (100 and 300 pairs) had been reviewed blind against their original
labels, with disagreements checked by hand.

**5. Final dataset.** `data/processed/final_pairs_v05.csv` holds **1,515 pairs**
(`src/build_final_dataset_v05.py`), split by client with a stratified group
split (no client appears in two splits), about 60/20/20 for every pair type:

| | Training | Validation | Test |
|---|---|---|---|
| Pairs | 908 | 304 | 303 |
| Match / No match | 567 / 341 | 189 / 115 | 189 / 114 |

## First fine-tuning: good on synthetic data, worse on real data

The first fine-tuning used `OnlineContrastiveLoss` on the 908 training pairs
(3 epochs, best epoch by synthetic validation AP), as an Azure ML job on a
serverless T4 GPU (`azureml/finetune_job.yml`). On the synthetic test set
(`src/evaluate_embeddings_on_pairs.py`, threshold chosen on validation only)
it looked very good: F1 from 0.777 to 0.879 and ROC-AUC from 0.690 to 0.912.

The synthetic test only tells us how well the model learned *our* data. To
check real behaviour we used the human example files (eight Excel files, one
per program) with real client expenses and their correct program expense
(`src/evaluate_real_pairs.py`). After removing the 153 few-shot examples and
programs missing from our catalogue, **537 real pairs** remain. There, the
first fine-tuned model was **worse** than the pretrained one (Recall@1 47%
vs 68%). It was pulled towards a few generic expenses ("Λοιπός εξοπλισμός",
"Κτίρια, εγκαταστάσεις", "Πνευματική ιδιοκτησία"). Likely reasons: the loss
taught "match or not" rather than "this one above the others"; our negatives
came mostly from *other* programs, not from the same subprogram where the real
confusions happen; and some program expenses only ever appeared as matches.

## Second fine-tuning: ranking with reviewed hard negatives

- **Real split.** The 537 real pairs are split by client into `real_validation`
  (269) and `real_test` (268) (`src/split_real_pairs.py`). Real pairs are never
  used for training.
- **Hard negatives.** For every training match, the pretrained bge-m3 ranks the
  other expenses of the same subprogram and keeps the three closest
  (`src/build_subprogram_negative_candidates_for_synthetic_mnrl_training.py`).
  Claude reviewed them against guideline v0.5 (726 checks), and for each pair
  we keep the closest one judged NO_MATCH with high confidence.
- **Triplets.** 556 of 567 training matches got a (client, correct expense,
  hard negative) triplet (`src/build_mnrl_triplets.py`). In 188 of them, the
  pretrained bge-m3 would have ranked the negative above the correct expense.
- **Training.** `MultipleNegativesRankingLoss` with these triplets, 3 epochs;
  the best epoch is chosen by **Recall@1 on real_validation**
  (`src/finetune_bge_m3_zestama.py --dataset mnrl_triplets`,
  `azureml/finetune_mnrl_job.yml`). The best was epoch 1 (real_validation
  Recall@1 0.669 -> 0.889); later epochs kept improving the synthetic metrics
  but slightly lowered the real ranking.

## Final results

**Real test** (268 unseen real pairs, ranking within the subprogram): see
*Results in short* above. The MNRL model is better than or equal to both
pretrained models in every program (it ties with `text-embedding-3-large` on
one of the six), and its remaining errors are spread across many expenses
instead of a few generic ones.

**Synthetic test** (303 pairs, match or not, threshold chosen on validation):

| Model | Precision | Recall | F1 | ROC-AUC |
|---|---|---|---|---|
| `BAAI/bge-m3` (pretrained) | 0.693 | 0.884 | 0.777 | 0.690 |
| `text-embedding-3-large` | 0.714 | 0.857 | 0.779 | 0.690 |
| bge-m3 fine-tuned, OnlineContrastiveLoss | **0.837** | **0.926** | **0.879** | **0.912** |
| bge-m3 fine-tuned, MNRL | 0.790 | 0.878 | 0.832 | 0.847 |

The MNRL model is the only one that clearly improves both the real ranking and
the synthetic match/no-match task over the pretrained models. The first
fine-tuned model is still better on the synthetic test, especially on
semi-hard non-matches (19/38 vs 9/38), but that did not carry over to real
expenses.

## Things to keep in mind

- Client expenses in training are synthetic; they carry the style of the model
  that wrote them.
- Most labels come from Claude applying the guideline, not from domain experts.
- The real example files list one correct expense per client, while the
  guideline allows several; Recall@1 can count a defensible answer as wrong.
- 268 real test pairs give an uncertainty of a few points on Recall@1; the gap
  to the other models is much larger than that.
- The real test covers new clients, not new funding programs.
- Similarity scores are not probabilities of a valid match.

## Built with

Python, pandas, NumPy, scikit-learn, sentence-transformers (E5, bge-m3),
Azure OpenAI (GPT-4o for generation, `text-embedding-3-large` as a baseline),
Claude (labelling against the guideline), Azure Machine Learning (serverless
GPU jobs) and Excel for review.
