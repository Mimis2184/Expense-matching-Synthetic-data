import argparse
import io
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import PROCESSED_DATA_DIR
from src.build_evaluation_set import load_program_expenses, text_key, to_int_or_none
from src.compare_embedding_models import CACHE_DIR, clean_value, get_embeddings, require_columns
from src.evaluate_embeddings_on_pairs import MODELS, build_texts


# ============================================================
# Settings
#
# Purpose: hard negatives for (client, correct expense, negative)
# triplets in MultipleNegativesRankingLoss fine-tuning on the
# synthetic training split. Motivated by real-pair errors between
# expenses of the same subprogram.
#
# Propose hard negatives for training Match pairs using the
# original bge-m3. Candidates are not labelled: review them before
# training. Known Match texts from every split are excluded.
# Local CPU embeddings only; no API calls or model downloads.
# ============================================================

INPUT_PATH = PROCESSED_DATA_DIR / "final_pairs_v05.csv"
OUTPUT_PATH = PROCESSED_DATA_DIR / "subprogram_negative_candidates_for_synthetic_mnrl_training.csv"
EXCEL_PATH = OUTPUT_PATH.with_suffix(".xlsx")
TOP_K = 3
PROGRAM_COLUMNS = ["program_category", "program_subcategory", "program_expense_description"]
OUTPUT_COLUMNS = [
    "candidate_id", "pair_id", "candidate_rank",
    "client_expense_title", "client_expense_description", "client_text",
    "program_title", "subprogram_id", "correct_program_expense_id",
    "correct_category", "correct_subcategory", "correct_description", "correct_score",
    "candidate_program_expense_id", "candidate_category", "candidate_subcategory",
    "candidate_description", "candidate_program_text", "candidate_score",
    "decision", "confidence", "rule", "flag_review",
]


# ============================================================
# Inputs and catalog resolution
# ============================================================

def expense_key(category, subcategory, description):
    return text_key(category), text_key(subcategory), text_key(description)


def load_inputs():
    pairs = pd.read_csv(INPUT_PATH, encoding="utf-8-sig", keep_default_na=False)
    require_columns(
        pairs, ["pair_id", "split", "label", "client_text",
                "client_expense_title", "client_expense_description"] + PROGRAM_COLUMNS,
        INPUT_PATH,
    )
    labels = pd.to_numeric(pairs["label"], errors="raise")
    assert labels.isin([0, 1]).all(), "Labels must be 0 or 1."
    training = pairs.loc[pairs["split"].eq("training") & labels.eq(1)].copy()
    assert training["pair_id"].map(clean_value).ne("").all()
    assert training["pair_id"].is_unique, "Duplicate training pair_id values."
    assert training["client_text"].map(clean_value).ne("").all(), "Empty client_text."

    programs = load_program_expenses()
    require_columns(
        programs, ["program_title", "subprogram_id", "category", "subcategory",
                   "expense_description", "program_expense_id"],
        Path("program_expenses_clean.csv"),
    )
    catalog_keys = []
    text_lookup = defaultdict(list)
    contexts = defaultdict(list)
    for index, row in programs.iterrows():
        key = expense_key(row["category"], row["subcategory"], row["expense_description"])
        catalog_keys.append(key)
        text_lookup[key].append(index)
        context = (text_key(row["program_title"]), to_int_or_none(row["subprogram_id"]))
        contexts[context].append(index)

    # Build exactly the same document format as training/evaluation.
    text_frame = pd.DataFrame({
        "client_expense_title": "", "client_expense_description": "",
        "program_category": programs["category"].map(clean_value),
        "program_subcategory": programs["subcategory"].map(clean_value),
        "program_expense_description": programs["expense_description"].map(clean_value),
    })
    _, program_texts = build_texts(text_frame)
    return pairs, labels, training, programs, catalog_keys, text_lookup, contexts, program_texts


def resolve_correct(row, programs, text_lookup):
    value = clean_value(row.get("program_expense_id", ""))
    if value:
        number = pd.to_numeric(value, errors="raise")
        if not np.isfinite(number) or number % 1 != 0 or not 1 <= number <= len(programs):
            raise ValueError(f"Invalid program_expense_id for {row['pair_id']}: {value!r}")
        return int(number) - 1, "id"
    key = expense_key(*(row[column] for column in PROGRAM_COLUMNS))
    matches = text_lookup.get(key, [])
    # Catalog order is ascending program_expense_id.
    return (matches[0], "text") if matches else (None, "no catalog match")


def prepare_candidates(pairs, labels, training, programs, catalog_keys, text_lookup, contexts):
    known_matches = defaultdict(set)
    for _, row in pairs.loc[labels.eq(1)].iterrows():
        client = text_key(row["client_text"])
        known_matches[client].add(expense_key(*(row[column] for column in PROGRAM_COLUMNS)))
        index, _ = resolve_correct(row, programs, text_lookup)
        if index is not None:
            known_matches[client].add(catalog_keys[index])

    prepared = []
    counts = Counter()
    unresolved = []
    for _, row in training.iterrows():
        correct_index, method = resolve_correct(row, programs, text_lookup)
        counts[method] += 1
        if correct_index is None:
            unresolved.append(row["pair_id"])
            continue
        correct = programs.iloc[correct_index]
        context = (text_key(correct["program_title"]), to_int_or_none(correct["subprogram_id"]))
        client = text_key(row["client_text"])
        candidates = [
            index for index in contexts[context]
            if index != correct_index
            and catalog_keys[index] != catalog_keys[correct_index]
            and catalog_keys[index] not in known_matches[client]
        ]
        if len(candidates) < TOP_K:
            counts["fewer than three"] += 1
        prepared.append({
            "row": row, "correct_index": correct_index, "candidates": candidates,
        })

    print("Training Match pairs read:", len(training))
    print("Catalog matches by ID:", counts["id"])
    print("Catalog matches by text:", counts["text"])
    print("No catalog match:", counts["no catalog match"])
    for pair_id in unresolved:
        print(f"  {pair_id}: no catalog match")
    print("Resolved Match pairs with fewer than 3 candidates:", counts["fewer than three"])
    assert counts["id"] + counts["text"] + counts["no catalog match"] == len(training)
    return prepared


# ============================================================
# Local embeddings and ranking
# ============================================================

def rank_candidates(prepared, programs, program_texts):
    active = [item for item in prepared if item["candidates"]]
    if not active:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    models = [model for model in MODELS if model["id"] == "bge-m3"]
    if len(models) != 1 or models[0]["backend"] != "sentence_transformers":
        raise ValueError("MODELS must contain exactly one local bge-m3 entry.")

    clients = list(dict.fromkeys(clean_value(item["row"]["client_text"]) for item in active))
    needed_indices = sorted({
        index for item in active for index in [item["correct_index"]] + item["candidates"]
    })
    documents = list(dict.fromkeys(program_texts[index] for index in needed_indices))
    client_lookup = {text: index for index, text in enumerate(clients)}
    document_lookup = {text: index for index, text in enumerate(documents)}
    print(f"Unique texts to encode: {len(clients)} clients, {len(documents)} program expenses.")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    client_values, document_values = get_embeddings(models[0], clients, documents)
    for values, count in [(client_values, len(clients)), (document_values, len(documents))]:
        assert values.ndim == 2 and values.shape[0] == count and values.shape[1] > 0
        assert np.isfinite(values).all(), "Non-finite embeddings."
        assert np.allclose(np.linalg.norm(values, axis=1), 1, atol=1e-4)
    assert client_values.shape[1] == document_values.shape[1]

    rows = []
    for item in active:
        row = item["row"]
        correct_index = item["correct_index"]
        correct = programs.iloc[correct_index]
        client_text = clean_value(row["client_text"])
        vector = client_values[client_lookup[client_text]]
        correct_score = float(document_values[document_lookup[program_texts[correct_index]]] @ vector)
        indices = item["candidates"]
        scores = document_values[[document_lookup[program_texts[index]] for index in indices]] @ vector
        # Stable ties: lower program_expense_id first (catalog order).
        order = np.argsort(-scores, kind="stable")[:TOP_K]
        for rank, position in enumerate(order, start=1):
            candidate_index = indices[int(position)]
            candidate = programs.iloc[candidate_index]
            rows.append({
                "candidate_id": f"H{len(rows) + 1:04d}",
                "pair_id": row["pair_id"], "candidate_rank": rank,
                "client_expense_title": clean_value(row["client_expense_title"]),
                "client_expense_description": clean_value(row["client_expense_description"]),
                "client_text": client_text,
                "program_title": clean_value(correct["program_title"]),
                "subprogram_id": to_int_or_none(correct["subprogram_id"]),
                "correct_program_expense_id": int(correct["program_expense_id"]),
                "correct_category": clean_value(correct["category"]),
                "correct_subcategory": clean_value(correct["subcategory"]),
                "correct_description": clean_value(correct["expense_description"]),
                "correct_score": correct_score,
                "candidate_program_expense_id": int(candidate["program_expense_id"]),
                "candidate_category": clean_value(candidate["category"]),
                "candidate_subcategory": clean_value(candidate["subcategory"]),
                "candidate_description": clean_value(candidate["expense_description"]),
                "candidate_program_text": program_texts[candidate_index],
                "candidate_score": float(scores[int(position)]),
                "decision": "", "confidence": "", "rule": "", "flag_review": "",
            })
    result = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    assert result["candidate_id"].is_unique
    assert not result.duplicated(["pair_id", "candidate_program_expense_id"]).any()
    assert result["candidate_rank"].between(1, TOP_K).all()
    return result


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description=(
        "Mine same-subprogram hard-negative candidates with the original bge-m3 for synthetic "
        "training Match pairs, to be reviewed against guideline v0.5 and used as triplet "
        "negatives for MultipleNegativesRankingLoss."
    ))
    parser.add_argument("--dry-run", action="store_true", help="Compute and print without writing output files.")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing candidate outputs.")
    args = parser.parse_args()
    for path in [OUTPUT_PATH, EXCEL_PATH]:
        if path.exists() and not args.overwrite and not args.dry_run:
            raise FileExistsError(f"Output exists: {path}. Use --overwrite to replace it.")

    pairs, labels, training, programs, keys, lookup, contexts, texts = load_inputs()
    prepared = prepare_candidates(pairs, labels, training, programs, keys, lookup, contexts)
    result = rank_candidates(prepared, programs, texts)
    print("=" * 70)
    print("SUBPROGRAM HARD-NEGATIVE CANDIDATES FOR SYNTHETIC MNRL TRAINING")
    print("Total candidate rows:", len(result))
    print("Candidates require review; no negative labels have been assigned.")
    if args.dry_run:
        print("Dry run complete: no output CSV or Excel written (embedding cache may be populated).")
        return

    csv_bytes = result.to_csv(index=False).encode("utf-8-sig")
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        result.to_excel(writer, sheet_name="candidates", index=False)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    for path, contents in [(OUTPUT_PATH, csv_bytes), (EXCEL_PATH, excel_buffer.getvalue())]:
        with path.open("wb" if args.overwrite else "xb") as handle:
            handle.write(contents)
        print("Saved:", path)


if __name__ == "__main__":
    main()
