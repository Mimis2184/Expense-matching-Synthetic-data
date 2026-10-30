import json
import sys
from pathlib import Path

import pandas as pd


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import OUTPUT_DATA_DIR


# ============================================================
# Results summary
#
# Numbers only, for the repository: no client or program texts.
# Reads the evaluation workbooks and the MNRL training summary;
# writes results/final_results_summary.xlsx.
# ============================================================

SYNTHETIC_PATH = OUTPUT_DATA_DIR / "embeddings_on_pairs_results_final_v05.xlsx"
REAL_TEST_PATH = OUTPUT_DATA_DIR / "real_pairs_evaluation_real_test.xlsx"
MNRL_SUMMARY_PATH = BASE_DIR / "models" / "bge-m3-ft-mnrl-subprogram" / "training_summary.json"
OUTPUT_PATH = BASE_DIR / "results" / "final_results_summary.xlsx"

MODEL_LABELS = {
    "bge-m3": "bge-m3 (pretrained)",
    "text-embedding-3-large": "text-embedding-3-large",
    "bge-m3-ft-final-v05": "bge-m3 fine-tuned (OnlineContrastiveLoss)",
    "bge-m3-ft-mnrl-subprogram": "bge-m3 fine-tuned (MNRL, same-subprogram negatives)",
}


def label_models(frame):
    frame = frame.loc[frame["model"].isin(MODEL_LABELS)].copy()
    frame["model"] = frame["model"].map(MODEL_LABELS)
    return frame


def real_test_tables():
    summary = pd.read_excel(REAL_TEST_PATH, sheet_name="summary")
    summary = label_models(summary)[[
        "model", "pairs", "Recall@1", "Recall@3", "MRR", "threshold", "above_threshold_rate",
    ]]
    pairs = pd.read_excel(REAL_TEST_PATH, sheet_name="pairs")
    program = pairs["source_excel"].str.replace("Παραδείγματα_αντιστοίχισης_δαπανών_", "").str.replace(".xlsx", "")
    rows = []
    for name, group in pairs.groupby(program):
        row = {"program": name, "pairs": len(group)}
        for model_id, label in MODEL_LABELS.items():
            row[label] = float((group[f"{model_id}_rank"] == 1).mean())
        rows.append(row)
    return summary, pd.DataFrame(rows)


def synthetic_tables():
    summary = label_models(pd.read_excel(SYNTHETIC_PATH, sheet_name="summary"))[[
        "model", "threshold", "pair_count", "TP", "FP", "TN", "FN",
        "precision", "recall", "F1", "accuracy", "roc_auc",
    ]]
    pairs = pd.read_excel(SYNTHETIC_PATH, sheet_name="test_pairs")
    rows = []
    for (category, label), group in pairs.groupby(["pair_category", "label"]):
        row = {"pair_category": category, "label": "Match" if label == 1 else "No match", "pairs": len(group)}
        for model_id, model_label in MODEL_LABELS.items():
            row[model_label] = float((group[f"{model_id}_prediction"] == group["label"]).mean())
        rows.append(row)
    return summary, pd.DataFrame(rows)


def training_table():
    summary = json.loads(MNRL_SUMMARY_PATH.read_text(encoding="utf-8"))
    rows = [{"stage": "before training", **summary["real_validation_before"],
             **summary["synthetic_validation_before"]}]
    rows.append({"stage": f"best checkpoint (epoch {summary['best_epoch']:g})",
                 **summary["real_validation_after_best_checkpoint"],
                 **summary["synthetic_validation_after_best_checkpoint"]})
    keep = ["stage", "real_validation_recall@1", "real_validation_recall@3", "real_validation_mrr",
            "validation_cosine_ap", "validation_cosine_f1"]
    return pd.DataFrame(rows)[keep]


def main():
    real_summary, real_programs = real_test_tables()
    synthetic_summary, synthetic_categories = synthetic_tables()
    training = training_table()
    for frame in [real_summary, real_programs, synthetic_summary, synthetic_categories, training]:
        text = frame.astype(str).apply(" ".join, axis=1).str.cat(sep=" ")
        assert "Title:" not in text and "Description:" not in text, "Summary must not contain texts."
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        real_summary.round(4).to_excel(writer, sheet_name="real_test", index=False)
        real_programs.round(4).to_excel(writer, sheet_name="real_test_per_program", index=False)
        synthetic_summary.round(4).to_excel(writer, sheet_name="synthetic_test", index=False)
        synthetic_categories.round(4).to_excel(writer, sheet_name="synthetic_per_category", index=False)
        training.round(4).to_excel(writer, sheet_name="mnrl_training", index=False)
    print(real_summary.round(4).to_string(index=False))
    print("Saved:", OUTPUT_PATH)


if __name__ == "__main__":
    main()
