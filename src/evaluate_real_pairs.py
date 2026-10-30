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

from config import OUTPUT_DATA_DIR
from src.build_evaluation_set import (
    FEW_SHOT_SOURCE_PATH,
    HUMAN_PAIRS_DIR,
    HUMAN_PAIRS_PATTERN,
    load_program_expenses,
    text_key,
    to_int_or_none,
)
from src.compare_embedding_models import (
    CACHE_DIR,
    clean_value,
    get_embeddings,
    require_columns,
)
from src.evaluate_embeddings_on_pairs import (
    FINAL_OUTPUT_PATH,
    FINAL_PATH,
    MODELS,
    build_texts,
    get_azure_embeddings,
)


# ============================================================
# Settings
#
# Human pairs are known matches. Thresholds are reused from the
# final POC validation; no threshold is fitted on these examples.
# Retrieval candidates belong to the same program/subprogram.
# Inputs are read-only. Embeddings use the existing model caches.
# ============================================================

OUTPUT_PATH = OUTPUT_DATA_DIR / "real_pairs_evaluation.xlsx"
HUMAN_COLUMNS = [
    "Program_name", "Subprogram_ID", "Category", "Subcategory",
    "Business_Expense", "Business_Expense_description",
]
REASONS = [
    "used as few-shot", "program not in data",
    "no unique program expense", "duplicate pair",
]


# ============================================================
# Inputs and exclusions
# ============================================================

def client_key(title, description):
    return text_key(title), text_key(description)


def load_human_rows():
    paths = sorted(HUMAN_PAIRS_DIR.glob(HUMAN_PAIRS_PATTERN))
    if not paths:
        raise ValueError(f"No human pair Excels found in {HUMAN_PAIRS_DIR}")
    frames = []
    for path in paths:
        frame = pd.read_excel(path, sheet_name="Expenses", keep_default_na=False)
        require_columns(frame, HUMAN_COLUMNS, path)
        frame = frame[HUMAN_COLUMNS].copy()
        frame.insert(0, "excel_row", frame.index + 2)
        frame.insert(0, "source_excel", path.name)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def load_inputs():
    human = load_human_rows()
    programs = load_program_expenses()
    require_columns(
        programs,
        ["program_title", "subprogram_id", "category", "subcategory",
         "expense_description", "program_expense_id"],
        Path("program_expenses_clean.csv"),
    )
    few_shot = pd.read_excel(FEW_SHOT_SOURCE_PATH, keep_default_na=False)
    require_columns(
        few_shot, ["new_score", "Client Expense", "Client Expense Description"],
        FEW_SHOT_SOURCE_PATH,
    )
    few_shot = few_shot.loc[
        pd.to_numeric(few_shot["new_score"], errors="coerce").eq(0)
    ]
    few_shot_keys = {
        client_key(row["Client Expense"], row["Client Expense Description"])
        for _, row in few_shot.iterrows()
    }

    # Tuple keys also match missing subprogram IDs consistently.
    program_lookup = defaultdict(list)
    contexts = defaultdict(list)
    for index, row in programs.iterrows():
        context = (text_key(row["program_title"]), to_int_or_none(row["subprogram_id"]))
        contexts[context].append(index)
        program_lookup[context + (text_key(row["category"]), text_key(row["subcategory"]))].append(index)

    program_titles = {context[0] for context in contexts}
    kept, excluded = [], []
    seen = set()
    for _, row in human.iterrows():
        key = client_key(row["Business_Expense"], row["Business_Expense_description"])
        context = (text_key(row["Program_name"]), to_int_or_none(row["Subprogram_ID"]))
        matches = program_lookup.get(
            context + (text_key(row["Category"]), text_key(row["Subcategory"])), []
        )
        reason = ""
        if key in few_shot_keys:
            reason = "used as few-shot"
        elif context[0] not in program_titles:
            reason = "program not in data"
        elif len(matches) != 1:
            reason = "no unique program expense"
        else:
            program = programs.iloc[matches[0]]
            identity = (key, int(program["program_expense_id"]))
            if identity in seen:
                reason = "duplicate pair"
            else:
                seen.add(identity)

        if reason:
            excluded.append({**row.to_dict(), "reason": reason})
            continue

        kept.append({
            "source_excel": row["source_excel"], "excel_row": row["excel_row"],
            "program_title": clean_value(program["program_title"]),
            "subprogram_id": context[1],
            "client_expense_title": clean_value(row["Business_Expense"]),
            "client_expense_description": clean_value(row["Business_Expense_description"]),
            "program_expense_id": int(program["program_expense_id"]),
            "program_category": clean_value(program["category"]),
            "program_subcategory": clean_value(program["subcategory"]),
            "program_expense_description": clean_value(program["expense_description"]),
            "_client_key": key, "_context": context,
        })

    counts = Counter(row["reason"] for row in excluded)
    print("Human rows read:", len(human))
    for reason in REASONS:
        print(f"Removed ({reason}): {counts[reason]}")
    print(f"Real pairs remaining: {len(kept)} (expected approximately 537)")
    assert len(kept) + len(excluded) == len(human)
    if not kept:
        raise ValueError("No real pairs remain after filtering.")
    pairs = pd.DataFrame(kept)

    # All final dataset splits are checked, not just training.
    final = pd.read_csv(FINAL_PATH, encoding="utf-8-sig", keep_default_na=False)
    require_columns(final, ["client_expense_title", "client_expense_description"], FINAL_PATH)
    final_keys = {
        client_key(row["client_expense_title"], row["client_expense_description"])
        for _, row in final.iterrows()
    }
    overlap = pairs["_client_key"].isin(final_keys)
    assert not overlap.any(), (
        "Real clients also appear in final_pairs_v05.csv:\n"
        + pairs.loc[overlap, ["source_excel", "excel_row", "client_expense_title"]].to_string(index=False)
    )
    return pairs, programs, contexts, pd.DataFrame(
        excluded, columns=["source_excel", "excel_row"] + HUMAN_COLUMNS + ["reason"],
    )


def load_thresholds():
    summary = pd.read_excel(FINAL_OUTPUT_PATH, sheet_name="summary")
    require_columns(summary, ["model", "threshold"], FINAL_OUTPUT_PATH)
    thresholds = {}
    for model in MODELS:
        rows = summary.loc[summary["model"].map(clean_value).eq(model["id"])]
        if len(rows) != 1:
            raise ValueError(f"Expected exactly one threshold for {model['id']}.")
        threshold = float(rows.iloc[0]["threshold"])
        if not np.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError(f"Invalid threshold for {model['id']}: {threshold}")
        thresholds[model["id"]] = threshold
    return thresholds


# ============================================================
# Texts, candidates and known correct alternatives
# ============================================================

def prepare_candidates(pairs, programs, contexts):
    client_texts, correct_texts = build_texts(pairs)
    pairs["client_text"] = client_texts
    pairs["program_text"] = correct_texts
    catalog = pd.DataFrame({
        "client_expense_title": "", "client_expense_description": "",
        "program_category": programs["category"].map(clean_value),
        "program_subcategory": programs["subcategory"].map(clean_value),
        "program_expense_description": programs["expense_description"].map(clean_value),
    })
    _, catalog_texts = build_texts(catalog)
    candidates = {}
    for context in dict.fromkeys(pairs["_context"]):
        indices = contexts[context]
        texts = [catalog_texts[index] for index in indices]
        assert len(texts) == len(set(texts)), (
            f"Duplicate program expense texts within program/subprogram {context}."
        )
        candidates[context] = indices

    correct_ids = defaultdict(set)
    for _, row in pairs.iterrows():
        correct_ids[(row["_client_key"], row["_context"])].add(row["program_expense_id"])
        assert row["program_expense_id"] - 1 in candidates[row["_context"]], (
            f"Correct expense absent from candidates: {row['program_expense_id']}"
        )
    pairs["candidate_count"] = pairs["_context"].map(lambda key: len(candidates[key]))
    unique_clients = list(dict.fromkeys(client_texts))
    unique_programs = list(dict.fromkeys(
        catalog_texts[index]
        for indices in candidates.values() for index in indices
    ))
    return candidates, correct_ids, catalog_texts, unique_clients, unique_programs


# ============================================================
# Embeddings and metrics
# ============================================================

def encode_model(model, clients, programs):
    if model["backend"] == "sentence_transformers":
        client_values, program_values = get_embeddings(model, clients, programs)
    elif model["backend"] == "azure":
        texts = list(dict.fromkeys(clients + programs))
        # The shared function prints cache misses before making any API call.
        values = get_azure_embeddings(model, texts)
        indices = {text: index for index, text in enumerate(texts)}
        client_values = values[[indices[text] for text in clients]]
        program_values = values[[indices[text] for text in programs]]
    else:
        raise ValueError(f"Unsupported backend: {model['backend']}")
    for values, count in [(client_values, len(clients)), (program_values, len(programs))]:
        assert values.ndim == 2 and values.shape[0] == count and values.shape[1] > 0
        assert np.isfinite(values).all(), "Non-finite embeddings."
        assert np.allclose(np.linalg.norm(values, axis=1), 1, atol=1e-4), (
            "Embeddings must be normalized."
        )
    assert client_values.shape[1] == program_values.shape[1]
    return client_values, program_values


def evaluate_model(model, threshold, pairs, candidates, correct_ids,
                   catalog_texts, clients, programs):
    client_values, program_values = encode_model(model, clients, programs)
    client_lookup = {text: index for index, text in enumerate(clients)}
    program_lookup = {text: index for index, text in enumerate(programs)}
    similarities, ranks, top_texts = [], [], []
    for _, row in pairs.iterrows():
        indices = np.asarray(candidates[row["_context"]], dtype=int)
        vectors = program_values[[program_lookup[catalog_texts[index]] for index in indices]]
        scores = vectors @ client_values[client_lookup[row["client_text"]]]
        correct_position = int(np.flatnonzero(indices == row["program_expense_id"] - 1)[0])
        correct_score = float(scores[correct_position])
        known_correct = correct_ids[(row["_client_key"], row["_context"])]
        incorrect = np.asarray([int(index) + 1 not in known_correct for index in indices])
        # Strictly greater scores only: ties do not worsen the correct rank.
        rank = 1 + int(np.count_nonzero((scores > correct_score) & incorrect))
        similarities.append(correct_score)
        ranks.append(rank)
        # Candidate order is catalog order: ties use the lower expense ID.
        top_texts.append(catalog_texts[int(indices[int(np.argmax(scores))])])

    similarities = np.asarray(similarities)
    ranks = np.asarray(ranks)
    prefix = model["id"]
    pairs[f"{prefix}_similarity"] = similarities
    pairs[f"{prefix}_above_threshold"] = np.where(similarities >= threshold, "ναι", "όχι")
    pairs[f"{prefix}_rank"] = ranks
    pairs[f"{prefix}_top_1_program_text"] = top_texts
    return {
        "model": prefix, "threshold": threshold, "pairs": len(pairs),
        "mean_similarity": float(similarities.mean()),
        "median_similarity": float(np.median(similarities)),
        "min_similarity": float(similarities.min()),
        "max_similarity": float(similarities.max()),
        "above_threshold_rate": float((similarities >= threshold).mean()),
        "Recall@1": float((ranks <= 1).mean()),
        "Recall@3": float((ranks <= 3).mean()),
        "MRR": float((1.0 / ranks).mean()),
    }


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Evaluate embeddings on held-out real human pairs.")
    parser.add_argument("--overwrite", action="store_true", help="Replace the existing output workbook.")
    args = parser.parse_args()
    if OUTPUT_PATH.exists() and not args.overwrite:
        raise FileExistsError(f"Output exists: {OUTPUT_PATH}. Use --overwrite to replace it.")
    if not MODELS or len({model["id"] for model in MODELS}) != len(MODELS):
        raise ValueError("MODELS must contain non-empty, distinct model entries.")

    pairs, programs, contexts, excluded = load_inputs()
    thresholds = load_thresholds()
    candidates, correct_ids, catalog_texts, clients, program_texts = prepare_candidates(
        pairs, programs, contexts,
    )
    print(f"Unique client texts: {len(clients)}; unique candidate texts: {len(program_texts)}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    for model in MODELS:
        print("=" * 70)
        print(model["id"])
        summary_rows.append(evaluate_model(
            model, thresholds[model["id"]], pairs, candidates, correct_ids,
            catalog_texts, clients, program_texts,
        ))

    # Uniform random ranking reference, as defined per original correct pair.
    counts = pairs["candidate_count"].to_numpy(dtype=int)
    harmonic = np.cumsum(1.0 / np.arange(1, int(counts.max()) + 1))
    summary_rows.append({
        "model": "random", "pairs": len(pairs),
        "Recall@1": float((1.0 / counts).mean()),
        "Recall@3": float((np.minimum(3, counts) / counts).mean()),
        "MRR": float((harmonic[counts - 1] / counts).mean()),
    })
    summary = pd.DataFrame(summary_rows)
    output_pairs = pairs.drop(columns=["_client_key", "_context"])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False)
        output_pairs.to_excel(writer, sheet_name="pairs", index=False)
        excluded.to_excel(writer, sheet_name="excluded", index=False)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("wb" if args.overwrite else "xb") as handle:
        handle.write(buffer.getvalue())
    print("=" * 70)
    print("REAL PAIRS EVALUATION (rates and retrieval metrics: 0–1)")
    print(summary.round(4).to_string(index=False))
    print("All evaluated pairs are known matches; above-threshold rate is positive recall.")
    print("Random reference uses the requested single-target uniform-ranking formula.")
    print("Saved:", OUTPUT_PATH)


if __name__ == "__main__":
    main()
