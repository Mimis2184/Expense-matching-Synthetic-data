import gc
import hashlib
import json
import os
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# Project root / offline execution
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# Set before importing Hugging Face libraries. Never download models.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from config import OUTPUT_DATA_DIR, PROCESSED_DATA_DIR
from src.lexical_score import lexical_score


# ============================================================
# Settings
# ============================================================

MODELS = [
    {"name": "BAAI/bge-m3", "query_prompt": None},
    {"name": "Qwen/Qwen3-Embedding-0.6B", "query_prompt": "query"},
    {"name": "Snowflake/snowflake-arctic-embed-l-v2.0", "query_prompt": "query"},
]

MAX_SEQ_LENGTH = 2048
BATCH_SIZE = 4
EVALUATION_PATH = PROCESSED_DATA_DIR / "evaluation_set.xlsx"
PROGRAM_PATH = PROCESSED_DATA_DIR / "program_expenses_clean.csv"
CACHE_DIR = OUTPUT_DATA_DIR / "embedding_cache"
OUTPUT_PATH = OUTPUT_DATA_DIR / "model_comparison.xlsx"

MODES = {"A": "all_programs", "B": "same_program"}
LEXICAL_BANDS = ["= 0", "0–0.1", ">= 0.1"]
PROGRAM_TEXT_COLUMNS = ["category", "subcategory", "expense_description"]


# ============================================================
# Texts and input validation
# ============================================================

def clean_value(value) -> str:
    return "" if pd.isna(value) else str(value).strip()


def text_key(value) -> str:
    text = clean_value(value).casefold()
    text = "".join(
        character
        for character in unicodedata.normalize("NFD", text)
        if unicodedata.category(character) != "Mn"
    )
    return " ".join(text.split())


def require_columns(df, columns, path) -> None:
    missing = sorted(set(columns) - set(df.columns))
    if missing:
        raise ValueError(f"{path.name}: missing columns {missing}")


def load_inputs():
    evaluation = pd.read_excel(EVALUATION_PATH, sheet_name="evaluation_set")
    programs = pd.read_csv(PROGRAM_PATH, encoding="utf-8-sig").reset_index(drop=True)
    require_columns(
        evaluation,
        ["eval_id", "client_expense_title", "client_expense_description",
         "program_expense_id", "program_expense_class"],
        EVALUATION_PATH,
    )
    require_columns(
        programs, PROGRAM_TEXT_COLUMNS + ["expense_class", "source_file"], PROGRAM_PATH,
    )
    if evaluation.empty or programs.empty:
        raise ValueError("Evaluation set and program expenses must not be empty.")
    if evaluation["eval_id"].isna().any() or evaluation["eval_id"].duplicated().any():
        raise ValueError("eval_id must be non-empty and unique.")

    ids = pd.to_numeric(evaluation["program_expense_id"], errors="raise")
    if ids.isna().any() or (ids % 1 != 0).any():
        raise ValueError("program_expense_id must contain integer IDs.")
    evaluation["program_expense_id"] = ids.astype(int)
    programs["program_expense_id"] = programs.index + 1
    assert evaluation["program_expense_id"].isin(programs["program_expense_id"]).all(), (
        "Evaluation set contains program_expense_id values absent from program data."
    )

    programs["_class_key"] = programs["expense_class"].map(text_key)
    programs["_source_key"] = programs["source_file"].map(clean_value)
    programs["_text_key"] = [
        tuple(text_key(row[column]) for column in PROGRAM_TEXT_COLUMNS)
        for _, row in programs.iterrows()
    ]
    if (programs["_source_key"] == "").any():
        raise ValueError("Program expenses must have a source_file for same_program mode.")

    client_texts = [
        f"Title: {clean_value(row['client_expense_title'])}. "
        f"Description: {clean_value(row['client_expense_description'])}"
        for _, row in evaluation.iterrows()
    ]
    program_texts = [
        f"Category: {clean_value(row['category'])}. "
        f"Subcategory: {clean_value(row['subcategory'])}. "
        f"Description: {clean_value(row['expense_description'])}"
        for _, row in programs.iterrows()
    ]
    return evaluation, programs, client_texts, program_texts


def lexical_band(score) -> str:
    if score == 0:
        return "= 0"
    if score < 0.1:
        return "0–0.1"
    return ">= 0.1"


def prepare_pairs(evaluation, programs):
    pairs = []
    for client_index, row in evaluation.iterrows():
        correct_index = int(row["program_expense_id"]) - 1
        correct = programs.iloc[correct_index]
        assert text_key(row["program_expense_class"]) == correct["_class_key"], (
            f"eval_id={row['eval_id']}: program class differs from current program data."
        )
        score = lexical_score(
            row["client_expense_title"], row["client_expense_description"],
            correct["category"], correct["subcategory"],
        )
        class_mask = programs["_class_key"] == correct["_class_key"]
        for mode, mode_name in MODES.items():
            mask = class_mask.copy()
            if mode_name == "same_program":
                mask &= programs["_source_key"] == correct["_source_key"]
            candidates = np.flatnonzero(mask.to_numpy())
            assert correct_index in candidates, (
                f"eval_id={row['eval_id']}, mode={mode}: correct expense not in candidates."
            )
            relevant = np.array([
                programs.iloc[index]["_text_key"] == correct["_text_key"]
                for index in candidates
            ], dtype=bool)
            pairs.append({
                "client_index": client_index,
                "eval_id": row["eval_id"],
                "mode": mode,
                "candidate_mode": mode_name,
                "program_expense_class": clean_value(correct["expense_class"]),
                "lexical_score": score,
                "lexical_band": lexical_band(score),
                "correct_index": correct_index,
                "candidates": candidates,
                "relevant": relevant,
            })
    return pairs


# ============================================================
# Embedding cache
# ============================================================

def cache_path(settings, group, texts):
    # Include encoding settings as well as the ordered texts to avoid stale caches.
    payload = {
        "model": settings["name"], "group": group, "texts": texts,
        "query_prompt": settings["query_prompt"] if group == "clients" else None,
        "max_seq_length": MAX_SEQ_LENGTH, "normalize_embeddings": True,
        "cache_version": 1,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    model_name = settings["name"].replace("/", "--")
    return CACHE_DIR / f"{model_name}_{group}_{digest}.npy"


def load_cached(path, count):
    if not path.exists():
        return None
    embeddings = np.load(path, allow_pickle=False)
    if (
        embeddings.ndim != 2 or embeddings.shape[0] != count
        or embeddings.shape[1] == 0 or not np.isfinite(embeddings).all()
    ):
        raise ValueError(f"Invalid embedding cache: {path}")
    if not np.allclose(np.linalg.norm(embeddings, axis=1), 1.0, atol=1e-4):
        raise ValueError(f"Cache embeddings are not normalized: {path}")
    return embeddings


def get_embeddings(settings, client_texts, program_texts):
    texts_by_group = {"clients": client_texts, "programs": program_texts}
    paths = {
        group: cache_path(settings, group, texts)
        for group, texts in texts_by_group.items()
    }
    embeddings = {
        group: load_cached(paths[group], len(texts))
        for group, texts in texts_by_group.items()
    }
    missing = [group for group, values in embeddings.items() if values is None]
    if missing:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(
            settings["name"], device="cpu", local_files_only=True,
        )
        model.max_seq_length = MAX_SEQ_LENGTH
        # Ensure document encoding never inherits a model's default query prompt.
        model.default_prompt_name = None
        query_prompt = settings["query_prompt"]
        if query_prompt is not None and query_prompt not in model.prompts:
            raise ValueError(f"{settings['name']}: missing prompt {query_prompt!r}")
        try:
            for group in missing:
                active_prompt_name = (
                    query_prompt if group == "clients" else None
                )
                values = model.encode(
                    texts_by_group[group], batch_size=BATCH_SIZE,
                    normalize_embeddings=True, convert_to_numpy=True,
                    show_progress_bar=True,
                    prompt_name=active_prompt_name,
                    prompt=None if active_prompt_name is not None else "",
                )
                if not np.isfinite(values).all():
                    raise ValueError(f"{settings['name']}: non-finite {group} embeddings")
                # Atomic replacement prevents partially written cache files.
                temporary = paths[group].with_suffix(".npy.tmp")
                with temporary.open("wb") as handle:
                    np.save(handle, values, allow_pickle=False)
                temporary.replace(paths[group])
                embeddings[group] = values
        finally:
            del model
            gc.collect()
    else:
        print("Using cached client and program embeddings.")
    client_embeddings = embeddings["clients"]
    program_embeddings = embeddings["programs"]
    if client_embeddings is None or program_embeddings is None:
        raise ValueError(f"{settings['name']}: missing embeddings")
    if client_embeddings.shape[1] != program_embeddings.shape[1]:
        raise ValueError(f"{settings['name']}: embedding dimensions differ")
    return client_embeddings, program_embeddings


# ============================================================
# Ranking and metrics
# ============================================================

def evaluate_model(settings, pairs, client_embeddings, program_embeddings):
    rows = []
    for pair in pairs:
        candidates = pair["candidates"]
        similarities = program_embeddings[candidates] @ client_embeddings[pair["client_index"]]
        # Stable ties: lower program_expense_id first (candidates are in row order).
        order = np.argsort(-similarities, kind="stable")
        rank = int(np.flatnonzero(pair["relevant"][order])[0]) + 1
        best_correct = order[rank - 1]
        rows.append({
            "eval_id": pair["eval_id"], "model": settings["name"],
            "mode": pair["mode"], "candidate_mode": pair["candidate_mode"],
            "program_expense_class": pair["program_expense_class"],
            "lexical_score": pair["lexical_score"], "lexical_band": pair["lexical_band"],
            "correct_rank": rank, "candidate_count": len(candidates),
            "correct_similarity": float(similarities[best_correct]),
            "best_correct_program_expense_id": int(candidates[best_correct]) + 1,
            "original_correct_program_expense_id": pair["correct_index"] + 1,
            "original_correct_similarity": float(
                similarities[np.flatnonzero(candidates == pair["correct_index"])[0]]
            ),
            "top_1_program_expense_id": int(candidates[order[0]]) + 1,
        })
    return rows


def aggregate_metrics(per_pair, group_columns):
    rows = []
    for keys, group in per_pair.groupby(group_columns, sort=False, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        ranks = group["correct_rank"]
        rows.append({
            **dict(zip(group_columns, keys)),
            "pair_count": len(group),
            "top_1_accuracy": float((ranks <= 1).mean()),
            "top_5_accuracy": float((ranks <= 5).mean()),
            "top_10_accuracy": float((ranks <= 10).mean()),
            "MRR": float((1.0 / ranks).mean()),
            "mean_rank": float(ranks.mean()),
            "mean_candidate_count": float(group["candidate_count"].mean()),
        })
    return pd.DataFrame(rows)


# ============================================================
# Main
# ============================================================

def main() -> None:
    if not MODELS:
        raise ValueError("Configure at least one embedding model in MODELS.")
    evaluation, programs, client_texts, program_texts = load_inputs()
    pairs = prepare_pairs(evaluation, programs)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    durations = {}
    for settings in MODELS:
        print("=" * 70)
        print(settings["name"])
        started = time.perf_counter()
        client_embeddings, program_embeddings = get_embeddings(
            settings, client_texts, program_texts,
        )
        results.extend(evaluate_model(settings, pairs, client_embeddings, program_embeddings))
        durations[settings["name"]] = time.perf_counter() - started
        print(f"Elapsed: {durations[settings['name']]:.2f} seconds")
        del client_embeddings, program_embeddings
        gc.collect()

    per_pair = pd.DataFrame(results)
    groups = ["model", "mode", "candidate_mode"]
    summary = aggregate_metrics(per_pair, groups)
    summary["elapsed_seconds"] = summary["model"].map(durations)
    per_class = aggregate_metrics(per_pair, groups + ["program_expense_class"])
    per_band = aggregate_metrics(per_pair, groups + ["lexical_band"])
    per_band["lexical_band"] = pd.Categorical(
        per_band["lexical_band"], categories=LEXICAL_BANDS, ordered=True,
    )
    per_band = per_band.sort_values(["model", "mode", "lexical_band"])

    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False)
        per_class.to_excel(writer, sheet_name="per_class", index=False)
        per_band.to_excel(writer, sheet_name="per_lexical_band", index=False)
        per_pair.to_excel(writer, sheet_name="per_pair", index=False)

    print("=" * 70)
    print("MODEL COMPARISON (accuracy and MRR: 0–1)")
    print(summary.round(4).to_string(index=False))
    print("Saved:", OUTPUT_PATH)


if __name__ == "__main__":
    main()
