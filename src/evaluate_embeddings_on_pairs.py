import argparse
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import OUTPUT_DATA_DIR, PROCESSED_DATA_DIR
from src.compare_embedding_models import (
    CACHE_DIR,
    clean_value,
    get_embeddings,
    load_cached,
    require_columns,
)


# ============================================================
# Settings
#
# Pair classification baseline, before / after fine-tuning.
# Local models use the existing offline CPU encoder and cache.
# Azure requests are made only for texts missing from the cache.
#
# To evaluate a fine-tuned model, add a sentence_transformers
# entry with a new id and name pointing to its local directory.
# Use a new name / directory for each checkpoint to avoid reusing
# embeddings from an older checkpoint at the same path.
# ============================================================

MODELS = [
    {
        "id": "bge-m3",
        "name": "BAAI/bge-m3",
        "backend": "sentence_transformers",
        "query_prompt": None,
    },
    {
        "id": "text-embedding-3-large",
        "name": "text-embedding-3-large",
        "backend": "azure",
    },
    # Fine-tuned on final_pairs_v05 training split (Azure ML job quiet_octopus_64swzwffzy, commit 873b354).
    {
        "id": "bge-m3-ft-final-v05",
        "name": str(BASE_DIR / "models" / "bge-m3-ft-final-v05"),
        "backend": "sentence_transformers",
        "query_prompt": None,
    },
]

VALIDATION_PATH = OUTPUT_DATA_DIR / "review_100_pairs_comparison.xlsx"
TEST_PATH = OUTPUT_DATA_DIR / "review_300_pairs_comparison.xlsx"
OUTPUT_PATH = OUTPUT_DATA_DIR / "embeddings_on_pairs_results.xlsx"
FINAL_PATH = PROCESSED_DATA_DIR / "final_pairs_v05.csv"
FINAL_OUTPUT_PATH = OUTPUT_DATA_DIR / "embeddings_on_pairs_results_final_v05.xlsx"

# Fine-tuning data: no validation or test pair may appear in it.
TRAINING_PATH = PROCESSED_DATA_DIR / "training_pairs_poc.csv"

LABELS = {"Match": 1, "No match": 0}
CATEGORIES = [
    "positive", "easy_negative", "semi_hard_negative", "non_lexical_positive",
]
TEXT_COLUMNS = [
    "client_expense_title", "client_expense_description",
    "program_category", "program_subcategory", "program_expense_description",
]
THRESHOLDS = np.arange(101, dtype=np.float64) / 100

AZURE_BATCH_SIZE = 16
AZURE_MAX_RETRIES = 5
AZURE_TIMEOUT_SECONDS = 60.0


# ============================================================
# Inputs and reference labels
# ============================================================

def agreement_is_true(value) -> bool:
    # bool("False") is True: parse Excel boolean/string values explicitly.
    if pd.isna(value):
        return False
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "1", "1.0"}:
        return True
    if text in {"false", "0", "0.0", ""}:
        return False
    raise ValueError(f"Unexpected agreement value: {value!r}; expected True / False.")


def load_pairs(path, validation):
    df = pd.read_excel(path, sheet_name="review_comparison")
    columns = [
        "review_id", "source_pair_id", "pair_category", "our_label",
        "manual_review_label",
    ] + TEXT_COLUMNS
    if not validation:
        columns.append("agreement")
    require_columns(df, columns, path)

    for column in columns:
        if column != "agreement":
            df[column] = df[column].map(clean_value)

    for column in ["review_id", "source_pair_id"]:
        assert df[column].ne("").all(), f"{path.name}: empty {column}."
        assert df[column].is_unique, f"{path.name}: duplicate {column}."

    assert df["our_label"].isin(LABELS).all(), f"{path.name}: invalid our_label."
    manual = df["manual_review_label"]
    assert (manual.eq("") | manual.isin(LABELS)).all(), (
        f"{path.name}: manual_review_label must be empty, Match or No match."
    )
    assert df["pair_category"].isin(CATEGORIES).all(), (
        f"{path.name}: unknown pair_category."
    )

    # Keep IDs of the entire input, including unresolved test disagreements.
    source_ids = {
        column: set(df[column])
        for column in ["review_id", "source_pair_id"]
    }
    has_manual = manual.ne("")
    if validation:
        assert len(df) == 100, "Validation must contain exactly 100 pairs."
        keep = pd.Series(True, index=df.index)
    else:
        keep = has_manual | df["agreement"].map(agreement_is_true)

    df["label_text"] = manual.where(has_manual, df["our_label"])
    df["label_source"] = np.where(has_manual, "manual_review_label", "our_label")
    kept = df.loc[keep].copy().reset_index(drop=True)
    if kept.empty:
        raise ValueError(f"{path.name}: no labelled pairs remain.")
    assert kept["label_text"].isin(LABELS).all()
    kept["label"] = kept["label_text"].map(LABELS).astype(int)
    print(f"{path.name}: kept {len(kept)} / {len(df)}; excluded {len(df) - len(kept)}.")
    return kept, source_ids, len(df) - len(kept)


def load_inputs():
    validation, validation_ids, _ = load_pairs(VALIDATION_PATH, validation=True)
    test, test_ids, excluded = load_pairs(TEST_PATH, validation=False)
    for column in ["review_id", "source_pair_id"]:
        assert validation_ids[column].isdisjoint(test_ids[column]), (
            f"Validation and test overlap on {column}."
        )
    if validation["label"].nunique() != 2:
        raise ValueError("Validation must contain both Match and No match labels.")
    # Guard against leakage, e.g. a disagreement used for training that
    # later gets a manual_review_label and would enter the test set.
    if TRAINING_PATH.exists():
        training_ids = set(
            pd.read_csv(TRAINING_PATH, encoding="utf-8-sig")["source_pair_id"].map(clean_value)
        )
        for name, df in [("validation", validation), ("test", test)]:
            overlap = training_ids & set(df["source_pair_id"])
            if overlap:
                raise ValueError(
                    f"{len(overlap)} {name} pairs are also in {TRAINING_PATH.name}: "
                    f"{sorted(overlap)[:5]}"
                )
    else:
        print(f"Note: {TRAINING_PATH.name} not found; training overlap not checked.")
    return validation, test, excluded


def load_final_inputs():
    """
    Use stored validation/test texts without rebuilding them.
    Training rows are used only to check client leakage.
    """

    required = [
        "pair_id", "split", "source", "pair_category",
        "client_text", "program_text", "label", "label_text",
    ]
    pairs = pd.read_csv(
        FINAL_PATH,
        encoding="utf-8-sig",
        keep_default_na=False,
        usecols=lambda column: column in required,
    )
    require_columns(pairs, required, FINAL_PATH)

    assert pairs["pair_id"].map(
        lambda value: isinstance(value, str) and bool(value.strip())
    ).all(), "Final dataset contains empty or non-text pair_id values."
    assert pairs["pair_id"].is_unique, "Final dataset contains duplicate pair_id values."
    assert pairs["split"].isin(["training", "validation", "test"]).all(), (
        "Final dataset contains unknown split values."
    )

    # Do not convert training labels or include training in any metrics.
    training_clients = pairs.loc[pairs["split"].eq("training"), "client_text"]
    validation = pairs.loc[pairs["split"].eq("validation")].copy().reset_index(drop=True)
    test = pairs.loc[pairs["split"].eq("test")].copy().reset_index(drop=True)

    for name, frame in [("validation", validation), ("test", test)]:
        if frame.empty:
            raise ValueError(f"Final {name} split is empty.")
        for column in ["client_text", "program_text"]:
            assert frame[column].map(
                lambda value: isinstance(value, str) and bool(value.strip())
            ).all(), f"Final {name} contains empty or non-text {column}."

        labels = pd.to_numeric(frame["label"], errors="raise")
        assert labels.isin([0, 1]).all(), f"Final {name} labels must be 0 or 1."
        frame["label"] = labels.astype(int)
        assert frame["label_text"].isin(LABELS).all(), f"Final {name} has invalid label_text."
        assert frame["label_text"].map(LABELS).eq(frame["label"]).all(), (
            f"Final {name}: label and label_text disagree."
        )
        assert frame["pair_category"].isin(CATEGORIES).all(), (
            f"Final {name} contains unknown pair_category values."
        )

    assert set(validation["label"]) == {0, 1}, (
        "Final validation must contain both Match and No match labels."
    )

    def client_keys(texts):
        return {" ".join(clean_value(text).casefold().split()) for text in texts}

    test_keys = client_keys(test["client_text"])
    validation_keys = client_keys(validation["client_text"])
    training_keys = client_keys(training_clients)
    assert test_keys.isdisjoint(validation_keys), "Final test / validation client_text overlap."
    assert test_keys.isdisjoint(training_keys), "Final test / training client_text overlap."
    assert validation_keys.isdisjoint(training_keys), "Final validation / training client_text overlap."

    print(f"{FINAL_PATH.name}: validation {len(validation)}, test {len(test)}; test excluded 0.")
    return validation, test, 0


def build_texts(df):
    clients = [
        f"Title: {row['client_expense_title']}. "
        f"Description: {row['client_expense_description']}"
        for _, row in df.iterrows()
    ]
    programs = [
        f"Category: {row['program_category']}. "
        f"Subcategory: {row['program_subcategory']}. "
        f"Description: {row['program_expense_description']}"
        for _, row in df.iterrows()
    ]
    return clients, programs


# ============================================================
# Azure embeddings and cache
# ============================================================

def azure_settings():
    from dotenv import dotenv_values

    # python-dotenv handles spaces around '=' and quoted values.
    # Environment variables take precedence; credentials are never logged.
    values = dotenv_values(BASE_DIR / ".env")
    names = {
        "endpoint": "AZURE_OPENAI_EMB_ENDPOINT",
        "api_key": "AZURE_OPENAI_EMB_API_KEY",
        "api_version": "AZURE_OPENAI_EMB_API_VERSION",
        "deployment": "AZURE_OPENAI_EMB_API_DEPLOYMENT",
    }
    settings = {
        key: (os.environ.get(name, values.get(name)) or "").strip()
        for key, name in names.items()
    }
    # A fully cached run does not need the API key.
    missing = [names[key] for key in ["endpoint", "api_version", "deployment"]
               if not settings[key]]
    if missing:
        raise ValueError(f"Missing Azure settings: {', '.join(missing)}")
    return settings


def azure_cache_path(model, settings, text):
    # One file per unique text: changed labels / test membership do not
    # invalidate embeddings already paid for. Do not hash the API key.
    payload = {
        "backend": "azure", "model": model["name"],
        "endpoint": settings["endpoint"].rstrip("/"),
        "deployment": settings["deployment"],
        "api_version": settings["api_version"],
        "text": text, "dimensions": "default",
        "normalize_embeddings": True, "cache_version": 1,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return CACHE_DIR / f"azure_pairs_{digest}.npy"


def get_azure_embeddings(model, texts):
    settings = azure_settings()
    paths = [azure_cache_path(model, settings, text) for text in texts]
    cached = [load_cached(path, 1) for path in paths]
    missing = [index for index, value in enumerate(cached) if value is None]
    print(f"Azure cache: {len(texts) - len(missing)} hits; {len(missing)} missing texts.")

    if missing:
        if not settings["api_key"]:
            raise ValueError("AZURE_OPENAI_EMB_API_KEY is required for cache misses.")
        from openai import AzureOpenAI

        # SDK retries transient connection/timeout errors, 408/409/429
        # and server errors, with backoff. Permanent 4xx errors fail fast.
        with AzureOpenAI(
            azure_endpoint=settings["endpoint"],
            api_key=settings["api_key"],
            api_version=settings["api_version"],
            max_retries=AZURE_MAX_RETRIES,
            timeout=AZURE_TIMEOUT_SECONDS,
        ) as client:
            for start in range(0, len(missing), AZURE_BATCH_SIZE):
                indices = missing[start:start + AZURE_BATCH_SIZE]
                # Azure requires the deployment name in the model field.
                # Never silently truncate long inputs: API input-limit errors
                # must be resolved before interpreting the evaluation.
                response = client.embeddings.create(
                    model=settings["deployment"],
                    input=[texts[index] for index in indices],
                    encoding_format="float",
                )
                items = sorted(response.data, key=lambda item: item.index)
                assert [item.index for item in items] == list(range(len(indices))), (
                    "Azure returned missing or duplicate embedding indices."
                )
                values = np.asarray([item.embedding for item in items], dtype=np.float64)
                assert values.ndim == 2 and values.shape[1] > 0
                assert np.isfinite(values).all(), "Non-finite Azure embeddings."
                norms = np.linalg.norm(values, axis=1, keepdims=True)
                assert np.isfinite(norms).all() and (norms > 0).all()
                values = values / norms
                for index, value in zip(indices, values):
                    row = value.reshape(1, -1)
                    # Cache files are only created, never overwritten.
                    # Reuse an entry if another process has just created it.
                    if paths[index].exists():
                        cached[index] = load_cached(paths[index], 1)
                    else:
                        buffer = io.BytesIO()
                        np.save(buffer, row, allow_pickle=False)
                        with paths[index].open("xb") as handle:
                            handle.write(buffer.getvalue())
                        cached[index] = row
                print(f"Azure encoded {min(start + len(indices), len(missing))}/{len(missing)} texts.")

    assert all(value is not None for value in cached), "Missing Azure embeddings."
    rows = [value for value in cached if value is not None]
    assert len({value.shape[1] for value in rows}) == 1, "Azure dimensions differ."
    return np.concatenate(rows, axis=0)


def encode_pairs(model, client_texts, program_texts):
    # Deduplicate texts before encoding, then restore the original pair order.
    # Encoding both splits together is only an efficiency measure: no labels
    # enter the encoder and no parameters are fitted on the test set.
    if model["backend"] == "sentence_transformers":
        unique_clients = list(dict.fromkeys(client_texts))
        unique_programs = list(dict.fromkeys(program_texts))
        clients, programs = get_embeddings(model, unique_clients, unique_programs)
        client_indices = pd.Index(unique_clients).get_indexer(client_texts)
        program_indices = pd.Index(unique_programs).get_indexer(program_texts)
        client_values = clients[client_indices]
        program_values = programs[program_indices]
    elif model["backend"] == "azure":
        texts = list(dict.fromkeys(client_texts + program_texts))
        values = get_azure_embeddings(model, texts)
        index = pd.Index(texts)
        client_values = values[index.get_indexer(client_texts)]
        program_values = values[index.get_indexer(program_texts)]
    else:
        raise ValueError(f"Unknown embedding backend: {model['backend']}")

    assert client_values.ndim == program_values.ndim == 2
    assert client_values.shape == program_values.shape
    assert client_values.shape[0] == len(client_texts)
    assert client_values.shape[1] > 0, "Empty embedding dimensions."
    for values in [client_values, program_values]:
        assert np.isfinite(values).all(), "Embeddings must be finite."
        assert np.allclose(np.linalg.norm(values, axis=1), 1.0, atol=1e-4), (
            "Embeddings must be normalized."
        )
    # Accumulate in float64; clip only floating-point overflow beyond [-1, 1].
    similarities = np.einsum(
        "ij,ij->i", client_values.astype(np.float64), program_values.astype(np.float64),
    )
    assert np.isfinite(similarities).all()
    return np.clip(similarities, -1.0, 1.0)


# ============================================================
# Threshold selection and metrics
# ============================================================

def classification_metrics(labels, predictions):
    tp = int(((labels == 1) & (predictions == 1)).sum())
    fp = int(((labels == 0) & (predictions == 1)).sum())
    tn = int(((labels == 0) & (predictions == 0)).sum())
    fn = int(((labels == 1) & (predictions == 0)).sum())
    return {
        "pair_count": len(labels), "TP": tp, "FP": fp, "TN": tn, "FN": fn,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "F1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
        "accuracy": (tp + tn) / len(labels) if len(labels) else np.nan,
    }


def select_threshold(labels, similarities):
    rows = []
    best_threshold = 0.0
    best_metrics = None
    for threshold in THRESHOLDS:
        metrics = classification_metrics(labels, (similarities >= threshold).astype(int))
        rows.append({"threshold": float(threshold), **metrics})
        # Compare F1 fractions with integer arithmetic to resolve exact ties.
        # Ascending thresholds + >= selects the largest tied threshold.
        denominator = 2 * metrics["TP"] + metrics["FP"] + metrics["FN"]
        if best_metrics is None:
            choose = True
        else:
            best_denominator = (
                2 * best_metrics["TP"] + best_metrics["FP"] + best_metrics["FN"]
            )
            choose = (
                metrics["TP"] * max(best_denominator, 1)
                >= best_metrics["TP"] * max(denominator, 1)
            )
        if choose:
            best_threshold = float(threshold)
            best_metrics = metrics
    return best_threshold, pd.DataFrame(rows)


def evaluation_metrics(labels, similarities, threshold):
    metrics = classification_metrics(labels, (similarities >= threshold).astype(int))
    both_classes = len(np.unique(labels)) == 2
    # AUC needs both classes. Leave it blank in Excel for one-class groups.
    metrics["roc_auc"] = (
        float(roc_auc_score(labels, similarities)) if both_classes else np.nan
    )
    metrics["roc_auc_defined"] = both_classes
    # In a one-class group precision and F1 add nothing beyond recall /
    # accuracy: with only Match, precision is 1 whenever any Match is
    # predicted (0 by convention when none is) and F1 depends only on
    # recall; with only No match, precision, recall and F1 are always 0.
    # Recall is kept only when the group has Match pairs.
    # Use accuracy, FP and FN for such groups.
    if not both_classes:
        metrics["precision"] = np.nan
        metrics["F1"] = np.nan
        if not (labels == 1).any():
            metrics["recall"] = np.nan
    return metrics


# ============================================================
# Main
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate embedding pair classification with a validation-only threshold."
    )
    parser.add_argument(
        "--dataset", choices=["zestama", "final_v05"], default="zestama",
        help="Dataset to evaluate (default: zestama).",
    )
    parser.add_argument(
        "--models", nargs="+", choices=[model["id"] for model in MODELS],
        default=None, help="Model IDs to evaluate (default: all configured models).",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Allow replacing the results Excel; input files are never changed.",
    )
    args = parser.parse_args()
    output_path = FINAL_OUTPUT_PATH if args.dataset == "final_v05" else OUTPUT_PATH
    if output_path.exists() and not args.overwrite:
        raise FileExistsError(f"{output_path} already exists. Use --overwrite to replace it.")
    if not MODELS or len({model["id"] for model in MODELS}) != len(MODELS):
        raise ValueError("MODELS must contain at least one model, with unique IDs.")
    selected = [model for model in MODELS if args.models is None or model["id"] in args.models]

    if args.dataset == "final_v05":
        validation, test, test_excluded = load_final_inputs()
        test_columns = ["pair_id", "source", "pair_category", "label", "label_text"]
    else:
        validation, test, test_excluded = load_inputs()
        test_columns = [
            "review_id", "source_pair_id", "pair_category", "label", "label_text", "label_source",
        ]
    # Label changes do not alter cached text embeddings.
    all_pairs = pd.concat([validation, test], ignore_index=True)
    if args.dataset == "final_v05":
        clients = all_pairs["client_text"].tolist()
        programs = all_pairs["program_text"].tolist()
    else:
        clients, programs = build_texts(all_pairs)
    validation_labels = validation["label"].to_numpy(dtype=int)
    test_labels = test["label"].to_numpy(dtype=int)
    test_pairs = test[test_columns].copy()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    category_rows = []
    source_rows = []
    curves = []

    for model in selected:
        print("=" * 70)
        print(model["id"])
        started = time.perf_counter()
        similarities = encode_pairs(model, clients, programs)
        validation_similarities = similarities[:len(validation)]
        test_similarities = similarities[len(validation):]
        threshold, curve = select_threshold(validation_labels, validation_similarities)
        curve.insert(0, "model", model["id"])
        curves.append(curve)
        metrics = evaluation_metrics(test_labels, test_similarities, threshold)
        best_row = curve.loc[curve["threshold"] == threshold].iloc[0]
        summary_rows.append({
            "model": model["id"], "model_name": model["name"], "threshold": threshold,
            "validation_pairs": len(validation), "validation_F1": best_row["F1"],
            "test_excluded": test_excluded, **metrics,
            "elapsed_seconds": time.perf_counter() - started,
        })
        for category in CATEGORIES:
            mask = test["pair_category"].eq(category).to_numpy()
            category_rows.append({
                "model": model["id"], "pair_category": category, "threshold": threshold,
                **evaluation_metrics(test_labels[mask], test_similarities[mask], threshold),
            })
        if args.dataset == "final_v05":
            for source in sorted(test["source"].unique()):
                mask = test["source"].eq(source).to_numpy()
                source_rows.append({
                    "model": model["id"], "source": source, "threshold": threshold,
                    **evaluation_metrics(test_labels[mask], test_similarities[mask], threshold),
                })
        test_pairs[f"{model['id']}_similarity"] = test_similarities
        test_pairs[f"{model['id']}_prediction"] = (test_similarities >= threshold).astype(int)
        print(
            f"Threshold={threshold:.2f} | Test n={len(test)} | "
            f"Precision={metrics['precision']:.4f} Recall={metrics['recall']:.4f} "
            f"F1={metrics['F1']:.4f} Accuracy={metrics['accuracy']:.4f} "
            f"ROC-AUC={metrics['roc_auc']:.4f}"
        )

    # Prepare the entire workbook before touching the output path. Exclusive
    # creation also protects against another process creating it mid-run.
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="summary", index=False)
        pd.DataFrame(category_rows).to_excel(writer, sheet_name="per_category", index=False)
        if args.dataset == "final_v05":
            pd.DataFrame(source_rows).to_excel(writer, sheet_name="per_source", index=False)
        pd.concat(curves, ignore_index=True).to_excel(writer, sheet_name="threshold_curve", index=False)
        test_pairs.to_excel(writer, sheet_name="test_pairs", index=False)
    with output_path.open("wb" if args.overwrite else "xb") as handle:
        handle.write(buffer.getvalue())
    print("=" * 70)
    print("EMBEDDINGS ON PAIRS (metrics: 0–1; prediction 1 = Match)")
    print(pd.DataFrame(summary_rows).round(4).to_string(index=False))
    print("Undefined ROC-AUC values are blank in Excel; zero-denominator precision/recall/F1 = 0.")
    print("Saved:", output_path)


if __name__ == "__main__":
    main()
