import argparse
import csv
import hashlib
import json
import math
import os
import sys
import shutil
import tempfile
import time
from collections import defaultdict
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# Project root / offline execution
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(
    0,
    str(BASE_DIR),
)

# Stage the model in the Azure ML machine's cache or pass its
# local directory. Training never downloads models or calls APIs.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from config import PROCESSED_DATA_DIR
from src.build_evaluation_set import clean_value, load_program_expenses, text_key, to_int_or_none
from src.evaluate_embeddings_on_pairs import (
    VALIDATION_PATH,
    build_texts,
    load_pairs,
)


# ============================================================
# BGE-M3 fine-tuning (zestama / final_v05 / mnrl_triplets)
#
# Training texts are used exactly as stored in the CSV.
# Zestama validation uses the baseline loader and text formatter.
# Final v0.5 uses the stored training / validation texts as-is.
# Validation is only an evaluator, never a training dataset.
# Test rows are never used for training or checkpoint selection.
#
# Best checkpoint: validation cosine average precision, or real
# validation Recall@1 for reviewed MNRL triplets.
# The evaluator's F1 uses its own validation-optimal threshold;
# it is not the baseline's fixed-threshold test F1.
#
# One T4: fp16, no bf16, gradient checkpointing by default.
# No API call. Existing input data files are not modified.
# ============================================================

TRAINING_PATH = PROCESSED_DATA_DIR / "training_pairs_poc.csv"
FINAL_PATH = PROCESSED_DATA_DIR / "final_pairs_v05.csv"
TRIPLETS_PATH = PROCESSED_DATA_DIR / "mnrl_triplets_training.csv"
REAL_PAIRS_PATH = PROCESSED_DATA_DIR / "real_pairs_split.csv"
DEFAULT_OUTPUT_DIR = BASE_DIR / "models" / "bge-m3-ft-zestama"
DEFAULT_FINAL_OUTPUT_DIR = BASE_DIR / "models" / "bge-m3-ft-final-v05"
DEFAULT_MNRL_OUTPUT_DIR = BASE_DIR / "models" / "bge-m3-ft-mnrl-subprogram"
MODEL_NAME = "BAAI/bge-m3"
EVALUATOR_NAME = "validation"
AP_METRIC = f"{EVALUATOR_NAME}_cosine_ap"
F1_METRIC = f"{EVALUATOR_NAME}_cosine_f1"
REAL_METRIC = "real_validation_recall@1"
REAL_METRICS = [REAL_METRIC, "real_validation_recall@3", "real_validation_mrr"]


# ============================================================
# Inputs and safety checks
# ============================================================

def load_mnrl_training_and_validation():
    """Preserve triplet texts and use only synthetic validation for metrics."""

    training = pd.read_csv(TRIPLETS_PATH, encoding="utf-8-sig", keep_default_na=False)
    required = ["pair_id", "anchor", "positive", "negative"]
    missing = sorted(set(required) - set(training.columns))
    if missing:
        raise ValueError(f"{TRIPLETS_PATH.name}: missing columns {missing}")
    training = training[required].copy()
    assert not training.empty, "No MNRL training triplets."
    for column in required:
        assert training[column].map(
            lambda value: isinstance(value, str) and bool(value.strip())
        ).all(), f"Triplets contain empty or non-text {column}."
    assert training["pair_id"].str.strip().is_unique, "Duplicate triplet pair_id."
    assert training["negative"].map(text_key).ne(training["positive"].map(text_key)).all(), (
        "Triplet negatives must differ from positives."
    )

    # Test identities are used only for leakage checks, never evaluation.
    required = ["pair_id", "split", "client_text", "program_text", "label"]
    synthetic = pd.read_csv(
        FINAL_PATH, encoding="utf-8-sig", keep_default_na=False,
        usecols=lambda column: column in required,
    )
    missing = sorted(set(required) - set(synthetic.columns))
    if missing:
        raise ValueError(f"{FINAL_PATH.name}: missing columns {missing}")
    held_out_clients = set(synthetic.loc[
        synthetic["split"].isin(["validation", "test"]), "client_text",
    ].map(text_key))
    assert set(training["anchor"].map(text_key)).isdisjoint(held_out_clients), (
        "Triplet anchors overlap with synthetic validation/test clients."
    )
    validation = synthetic.loc[synthetic["split"].eq("validation")].copy().reset_index(drop=True)
    assert not validation.empty, "Synthetic validation is empty."
    assert validation["split"].eq("validation").all()
    assert validation["pair_id"].is_unique
    for column in ["client_text", "program_text"]:
        assert validation[column].map(
            lambda value: isinstance(value, str) and bool(value.strip())
        ).all(), f"Synthetic validation contains empty or non-text {column}."
    validation["label"] = pd.to_numeric(validation["label"], errors="raise")
    assert set(validation["label"]) == {0, 1}, "Synthetic validation must have labels 0 and 1."
    validation["label"] = validation["label"].astype(int)
    return training, validation, validation["client_text"].tolist(), validation["program_text"].tolist()


def load_real_validation_pairs(training):
    """Keep real_validation rows only; test client identities only check leakage."""

    required = [
        "split", "program_title", "subprogram_id", "program_expense_id",
        "client_expense_title", "client_expense_description",
    ]
    rows = []
    held_out_clients = set()
    with REAL_PAIRS_PATH.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(set(required) - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"{REAL_PAIRS_PATH.name}: missing columns {missing}")
        for row in reader:
            assert row["split"] in {"real_validation", "real_test"}, "Unknown real split."
            title = clean_value(row["client_expense_title"])
            description = clean_value(row["client_expense_description"])
            held_out_clients.add(text_key(f"Title: {title}. Description: {description}"))
            if row["split"] == "real_validation":
                rows.append({column: row[column] for column in required})

    assert set(training["anchor"].map(text_key)).isdisjoint(held_out_clients), (
        "Triplet anchors overlap with real_validation/real_test clients."
    )
    real_validation = pd.DataFrame(rows, columns=required)
    assert not real_validation.empty, "Real validation is empty."
    assert real_validation["split"].eq("real_validation").all(), (
        "Only real_validation rows may enter the retrieval evaluator."
    )
    return real_validation


def load_final_training_and_validation():
    """
    Read only the required columns and discard test rows immediately.
    Stored client/program texts are not stripped or reconstructed.
    """

    required = ["pair_id", "split", "client_text", "program_text", "label"]

    pairs = pd.read_csv(
        FINAL_PATH,
        encoding="utf-8-sig",
        keep_default_na=False,
        usecols=lambda column: column in required,
    )

    missing = sorted(set(required) - set(pairs.columns))

    if missing:
        raise ValueError(f"{FINAL_PATH.name}: missing columns {missing}")

    pairs = pairs.loc[pairs["split"].isin(["training", "validation"])].copy()

    pairs = pairs.rename(columns={"pair_id": "source_pair_id"})

    training = pairs.loc[pairs["split"].eq("training")].copy().reset_index(drop=True)
    validation = pairs.loc[pairs["split"].eq("validation")].copy().reset_index(drop=True)

    for name, frame in [("Training", training), ("Validation", validation)]:

        for column in ["source_pair_id", "client_text", "program_text"]:
            assert frame[column].map(
                lambda value: isinstance(value, str) and bool(value.strip())
            ).all(), f"{name} contains empty or non-text {column}."

        frame["label"] = pd.to_numeric(frame["label"], errors="raise")

        assert frame["label"].isin([0, 1]).all(), f"{name} labels must be 0 or 1."
        assert set(frame["label"]) == {0, 1}, f"{name} must contain both labels."

        frame["label"] = frame["label"].astype(int)

        assert frame["source_pair_id"].is_unique, f"{name} has duplicate pair IDs."
        assert not frame.duplicated(["client_text", "program_text"]).any(), (
            f"{name} has duplicate text pairs."
        )

    assert set(training["source_pair_id"].str.strip()).isdisjoint(
        set(validation["source_pair_id"].str.strip())
    ), "Training / validation source_pair_id overlap."

    clients = validation["client_text"].tolist()
    programs = validation["program_text"].tolist()

    assert set(zip(training["client_text"], training["program_text"])).isdisjoint(
        set(zip(clients, programs))
    ), "Training / validation text-pair overlap."

    training_clients = {
        " ".join(text.casefold().split()) for text in training["client_text"]
    }
    validation_clients = {" ".join(text.casefold().split()) for text in clients}

    assert training_clients.isdisjoint(validation_clients), (
        "Training / validation normalized client_text overlap."
    )

    return training, validation, clients, programs


def load_training_and_validation(dataset="zestama"):

    if dataset == "mnrl_triplets":
        return load_mnrl_training_and_validation()

    if dataset == "final_v05":
        return load_final_training_and_validation()

    if dataset != "zestama":
        raise ValueError(f"Unknown dataset: {dataset}")

    training = pd.read_csv(
        TRAINING_PATH,
        encoding="utf-8-sig",
        keep_default_na=False,
    )

    required = ["source_pair_id", "client_text", "program_text", "label"]

    missing = sorted(set(required) - set(training.columns))

    if missing:
        raise ValueError(f"{TRAINING_PATH.name}: missing columns {missing}")

    for column in ["source_pair_id", "client_text", "program_text"]:
        assert training[column].map(
            lambda value: isinstance(value, str) and bool(value.strip())
        ).all(), f"Training contains empty or non-text {column}."

    # Do not strip / rebuild the prepared training texts.
    training["label"] = pd.to_numeric(training["label"], errors="raise")

    assert training["label"].isin([0, 1]).all(), "Training labels must be 0 or 1."

    assert set(training["label"]) == {0, 1}, "Training must contain both labels."

    training["label"] = training["label"].astype(int)

    assert training["source_pair_id"].is_unique

    assert not training.duplicated(["client_text", "program_text"]).any()

    validation, _, _ = load_pairs(VALIDATION_PATH, validation=True)

    assert len(validation) == 100

    assert set(validation["label"]) == {0, 1}, "Validation must contain both labels."

    clients, programs = build_texts(validation)

    assert set(training["source_pair_id"].str.strip()).isdisjoint(
        set(validation["source_pair_id"])
    ), "Training / validation source_pair_id overlap."

    assert set(zip(training["client_text"], training["program_text"])).isdisjoint(
        set(zip(clients, programs))
    ), "Training / validation text-pair overlap."

    return training, validation, clients, programs


def label_counts(df):

    return {
        "No match (0)": int(df["label"].eq(0).sum()),
        "Match (1)": int(df["label"].eq(1).sum()),
    }


def token_length_report(tokenizer, texts, limit):
    """
    Count full lengths, including special tokens, without truncation.
    Count text occurrences (client + program), not just unique strings.
    """

    lengths = []

    for start in range(0, len(texts), 32):
        encoded = tokenizer(
            texts[start:start + 32],
            add_special_tokens=True,
            truncation=False,
            padding=False,
            return_attention_mask=False,
            return_token_type_ids=False,
        )
        lengths.extend(len(ids) for ids in encoded["input_ids"])

    return {
        "text_count": len(lengths),
        "over_max_seq_length": sum(length > limit for length in lengths),
        "max_tokens": max(lengths, default=0),
        "max_seq_length": limit,
    }


def numeric_metrics(metrics):

    result = {key: float(value) for key, value in metrics.items()}

    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError("Validation evaluator returned non-finite metrics.")

    return result


# ============================================================
# Real validation retrieval evaluator (loaded only for triplets)
# ============================================================

def create_real_pairs_evaluator(real_validation, batch_size):
    # Keep sentence-transformers imports lazy, as in the existing modes.
    from sentence_transformers.evaluation import SentenceEvaluator

    class RealPairsRetrievalEvaluator(SentenceEvaluator):

        def __init__(self):
            super().__init__()
            self.name = "real_validation"
            self.primary_metric = REAL_METRIC
            self.greater_is_better = True
            self.batch_size = batch_size
            self.pair_count = len(real_validation)
            assert real_validation["split"].eq(self.name).all()

            catalog = load_program_expenses().reset_index(drop=True)
            contexts = defaultdict(list)
            for index, row in catalog.iterrows():
                context = (text_key(row["program_title"]), to_int_or_none(row["subprogram_id"]))
                contexts[context].append(index)
            text_frame = pd.DataFrame({
                "client_expense_title": "", "client_expense_description": "",
                "program_category": catalog["category"].map(clean_value),
                "program_subcategory": catalog["subcategory"].map(clean_value),
                "program_expense_description": catalog["expense_description"].map(clean_value),
            })
            _, catalog_texts = build_texts(text_frame)
            records = []
            correct_ids = defaultdict(set)
            for _, row in real_validation.iterrows():
                title = clean_value(row["client_expense_title"])
                description = clean_value(row["client_expense_description"])
                context = (text_key(row["program_title"]), to_int_or_none(row["subprogram_id"]))
                client = (text_key(title), text_key(description))
                number = pd.to_numeric(row["program_expense_id"], errors="raise")
                assert np.isfinite(number) and number % 1 == 0 and 1 <= number <= len(catalog), (
                    "Invalid real validation program_expense_id."
                )
                correct_id = int(number)
                assert correct_id - 1 in contexts[context], (
                    "Correct expense is absent from its real validation subprogram."
                )
                correct_ids[(client, context)].add(correct_id)
                records.append((f"Title: {title}. Description: {description}", client, context, correct_id))

            self.client_texts = list(dict.fromkeys(record[0] for record in records))
            needed_contexts = list(dict.fromkeys(record[2] for record in records))
            self.program_texts = list(dict.fromkeys(
                catalog_texts[index] for context in needed_contexts for index in contexts[context]
            ))
            client_lookup = {text: index for index, text in enumerate(self.client_texts)}
            program_lookup = {text: index for index, text in enumerate(self.program_texts)}
            self.pairs = []
            for client_text, client, context, correct_id in records:
                indices = contexts[context]
                self.pairs.append({
                    "client_index": client_lookup[client_text],
                    "candidates": np.asarray([program_lookup[catalog_texts[index]] for index in indices]),
                    "correct_position": indices.index(correct_id - 1),
                    "incorrect": np.asarray([
                        index + 1 not in correct_ids[(client, context)] for index in indices
                    ], dtype=bool),
                })

        def __call__(self, model, output_path=None, epoch=-1, steps=-1):
            # Never cache changing model embeddings across training epochs.
            clients = model.encode(
                self.client_texts, normalize_embeddings=True,
                batch_size=self.batch_size, convert_to_numpy=True,
                show_progress_bar=True, prompt="",
            )
            programs = model.encode(
                self.program_texts, normalize_embeddings=True,
                batch_size=self.batch_size, convert_to_numpy=True,
                show_progress_bar=True, prompt="",
            )
            assert clients.ndim == programs.ndim == 2 and clients.shape[1] == programs.shape[1]
            assert np.isfinite(clients).all() and np.isfinite(programs).all()
            ranks = []
            for pair in self.pairs:
                scores = programs[pair["candidates"]] @ clients[pair["client_index"]]
                correct_score = scores[pair["correct_position"]]
                ranks.append(1 + int(np.count_nonzero(
                    (scores > correct_score) & pair["incorrect"]
                )))
            ranks = np.asarray(ranks)
            metrics = self.prefix_name_to_metrics({
                "recall@1": float((ranks <= 1).mean()),
                "recall@3": float((ranks <= 3).mean()),
                "mrr": float((1.0 / ranks).mean()),
            }, self.name)
            self.store_metrics_in_model_card_data(model, metrics, epoch, steps)
            return metrics

        def get_config_dict(self):
            return {"name": self.name, "pairs": self.pair_count, "batch_size": self.batch_size}

    return RealPairsRetrievalEvaluator()


# ============================================================
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Fine-tune BGE-M3 on synthetic POC pairs (validation-only checkpoint selection)."
    )

    parser.add_argument("--model-name-or-path", default=MODEL_NAME)
    parser.add_argument("--dataset", choices=["zestama", "final_v05", "mnrl_triplets"], default="zestama")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--allow-cpu", action="store_true", help="Allow a slow CPU smoke test; disables fp16.")
    parser.add_argument("--loss", choices=["OnlineContrastiveLoss", "MultipleNegativesRankingLoss"], default=None,
                        help="Default: OnlineContrastiveLoss, or MultipleNegativesRankingLoss for mnrl_triplets.")
    parser.add_argument("--max-seq-length", type=int, default=1024)
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--eval-batch-size", type=int, default=8)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=1)
    parser.add_argument("--warmup-ratio", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fp16", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--gradient-checkpointing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-steps", type=int, default=-1, help="Positive value overrides epochs, e.g. 5 for a smoke test.")

    args = parser.parse_args()

    if args.loss is None:
        args.loss = "MultipleNegativesRankingLoss" if args.dataset == "mnrl_triplets" else "OnlineContrastiveLoss"
    if args.dataset == "mnrl_triplets" and args.loss != "MultipleNegativesRankingLoss":
        parser.error("--dataset mnrl_triplets requires --loss MultipleNegativesRankingLoss.")

    default_output_dir = (
        DEFAULT_FINAL_OUTPUT_DIR if args.dataset == "final_v05" else DEFAULT_OUTPUT_DIR
    )
    if args.dataset == "mnrl_triplets":
        default_output_dir = DEFAULT_MNRL_OUTPUT_DIR
    output_dir = (args.output_dir or default_output_dir).expanduser().resolve()
    training_path = FINAL_PATH if args.dataset == "final_v05" else TRAINING_PATH
    validation_path = FINAL_PATH if args.dataset == "final_v05" else VALIDATION_PATH
    model_id = "bge-m3-ft-final-v05" if args.dataset == "final_v05" else "bge-m3-ft-zestama"
    selection_metric = AP_METRIC
    if args.dataset == "mnrl_triplets":
        training_path = TRIPLETS_PATH
        validation_path = FINAL_PATH
        model_id = "bge-m3-ft-mnrl-subprogram"
        selection_metric = REAL_METRIC

    if output_dir.exists() and (not args.overwrite or not output_dir.is_dir()):
        raise FileExistsError(
            f"{output_dir} already exists. Use --overwrite for an existing model directory."
        )

    for key in ["max_seq_length", "epochs", "learning_rate", "batch_size",
                "eval_batch_size", "gradient_accumulation_steps"]:
        if getattr(args, key) <= 0:
            parser.error(f"--{key.replace('_', '-')} must be positive.")

    if not 0 <= args.warmup_ratio < 1:
        parser.error("--warmup-ratio must be at least 0 and less than 1.")

    if args.max_steps != -1 and args.max_steps <= 0:
        parser.error("--max-steps must be -1 or positive.")

    try:
        import torch
        from datasets import Dataset
        from sentence_transformers import (
            SentenceTransformer,
            SentenceTransformerTrainer,
            SentenceTransformerTrainingArguments,
            losses,
        )
        from sentence_transformers.evaluation import BinaryClassificationEvaluator, SequentialEvaluator
        from sentence_transformers.base.sampler import BatchSamplers
        from transformers import TrainerCallback, set_seed
        import accelerate  # noqa: F401
    except ImportError as error:
        raise RuntimeError(
            "Training requires sentence-transformers 6.x, datasets and accelerate "
            "in the Azure ML environment. No packages are installed by this script."
        ) from error

    if version("sentence-transformers").split(".", 1)[0] != "6":
        raise RuntimeError("This script requires sentence-transformers 6.x.")

    gpu_available = torch.cuda.is_available()

    if not gpu_available and not args.allow_cpu:
        raise RuntimeError(
            "No CUDA GPU is available. Use Azure ML GPU compute (e.g. one T4), "
            "or pass --allow-cpu for a slow smoke test."
        )

    if gpu_available and torch.cuda.device_count() != 1:
        raise RuntimeError("Expose exactly one GPU, e.g. CUDA_VISIBLE_DEVICES=0.")

    if int(os.environ.get("WORLD_SIZE", "1")) != 1:
        raise RuntimeError("This POC script expects a single training process.")

    device = "cuda" if gpu_available else "cpu"
    effective_fp16 = bool(args.fp16 and gpu_available)
    gpu_name = torch.cuda.get_device_name(0) if gpu_available else None
    started = time.perf_counter()
    set_seed(args.seed)

    training, validation, validation_clients, validation_programs = load_training_and_validation(args.dataset)

    used_training = training.copy()
    real_evaluator = None

    if args.loss == "MultipleNegativesRankingLoss":
        if args.dataset != "mnrl_triplets":
            used_training = training.loc[training["label"].eq(1)].copy()
        if len(used_training) < 2 or args.batch_size < 2:
            raise ValueError("MultipleNegativesRankingLoss needs at least two positive pairs and batch size >= 2.")

    if args.dataset == "mnrl_triplets":
        real_validation = load_real_validation_pairs(training)
        real_evaluator = create_real_pairs_evaluator(real_validation, args.eval_batch_size)

    try:
        model = SentenceTransformer(
            args.model_name_or_path,
            device=device,
            local_files_only=True,
        )
    except OSError as error:
        raise RuntimeError(
            "Model unavailable locally. Stage BAAI/bge-m3 on Azure ML and pass "
            "--model-name-or-path <local-directory>, or populate its local cache."
        ) from error

    model.max_seq_length = args.max_seq_length
    model.default_prompt_name = None

    if args.gradient_checkpointing:
        transformer = model[0].auto_model
        if hasattr(transformer.config, "use_cache"):
            transformer.config.use_cache = False
        # Enabled on the model here: transformers 5.17 Trainer passes every_n_layers, which sentence-transformers 6.1.0 does not accept.
        model.gradient_checkpointing_enable({"use_reentrant": False})

    training_texts = (
        training["anchor"].tolist() + training["positive"].tolist() + training["negative"].tolist()
        if args.dataset == "mnrl_triplets"
        else training["client_text"].tolist() + training["program_text"].tolist()
    )
    token_counts = {
        "training_loaded": token_length_report(
            model.tokenizer,
            training_texts,
            args.max_seq_length,
        ),
        "validation": token_length_report(
            model.tokenizer, validation_clients + validation_programs, args.max_seq_length,
        ),
    }
    if real_evaluator is not None:
        token_counts["real_validation"] = token_length_report(
            model.tokenizer, real_evaluator.client_texts + real_evaluator.program_texts,
            args.max_seq_length,
        )

    for name, report in token_counts.items():
        print(
            f"{name}: {report['over_max_seq_length']}/{report['text_count']} texts "
            f"exceed {args.max_seq_length} tokens; longest={report['max_tokens']}."
        )

    print("GPU:", gpu_name or "CPU (--allow-cpu)")
    print("Training pairs loaded / used:", len(training), "/", len(used_training))
    print("fp16:", effective_fp16, "| bf16: False")

    evaluator = BinaryClassificationEvaluator(
        sentences1=validation_clients,
        sentences2=validation_programs,
        labels=validation["label"].tolist(),
        name=EVALUATOR_NAME,
        batch_size=args.eval_batch_size,
        similarity_fn_names=["cosine"],
        show_progress_bar=True,
        write_csv=False,
    )
    if real_evaluator is not None:
        evaluator = SequentialEvaluator(
            [real_evaluator, evaluator], main_score_function=lambda scores: scores[0],
        )

    before = numeric_metrics(evaluator(model))

    if AP_METRIC not in before or F1_METRIC not in before:
        raise RuntimeError(f"Unexpected evaluator metric keys: {sorted(before)}")

    if real_evaluator is not None:
        if not set(REAL_METRICS).issubset(before):
            raise RuntimeError(f"Real validation metrics missing: {sorted(before)}")
        print(
            f"Before training: real_validation Recall@1={before[REAL_METRIC]:.4f}, "
            f"Recall@3={before[REAL_METRICS[1]]:.4f}, MRR={before[REAL_METRICS[2]]:.4f} | "
            f"synthetic validation AP={before[AP_METRIC]:.4f}, F1={before[F1_METRIC]:.4f}"
        )
    else:
        print(f"Before training: validation AP={before[AP_METRIC]:.4f}, F1={before[F1_METRIC]:.4f}")

    if args.dataset == "mnrl_triplets":
        dataset = Dataset.from_dict({
            "anchor": used_training["anchor"].tolist(),
            "positive": used_training["positive"].tolist(),
            "negative": used_training["negative"].tolist(),
        })
    else:
        dataset = Dataset.from_dict({
            "sentence1": used_training["client_text"].tolist(),
            "sentence2": used_training["program_text"].tolist(),
            "label": used_training["label"].tolist(),
        })

    if args.dataset == "mnrl_triplets":
        loss = losses.MultipleNegativesRankingLoss(model=model)
        batch_sampler = BatchSamplers.NO_DUPLICATES
        loss_settings = {
            "loss": "MultipleNegativesRankingLoss",
            "hard_negatives": "reviewed same-subprogram",
            "batch_sampler": "no_duplicates",
        }
    elif args.loss == "OnlineContrastiveLoss":
        loss = losses.OnlineContrastiveLoss(
            model=model,
            distance_metric=losses.SiameseDistanceMetric.COSINE_DISTANCE,
        )
        # Margin is left at the library default (record its actual value).
        loss_settings = {"distance": "cosine", "margin": float(loss.margin)}
        batch_sampler = BatchSamplers.BATCH_SAMPLER
    else:
        loss = losses.MultipleNegativesRankingLoss(model=model)
        dataset = dataset.remove_columns("label")
        # Avoid identical positives appearing as in-batch negatives.
        batch_sampler = BatchSamplers.NO_DUPLICATES
        loss_settings = {"positive_pairs_only": True, "batch_sampler": "no_duplicates"}

    # A fresh local temp directory keeps intermediate checkpoints off the
    # (possibly remote) output directory. Only the final model is saved at the root.
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = Path(tempfile.mkdtemp(prefix="checkpoints_"))

    training_args = SentenceTransformerTrainingArguments(
        output_dir=str(checkpoint_dir),
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        learning_rate=args.learning_rate,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.eval_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        warmup_steps=args.warmup_ratio,
        seed=args.seed,
        data_seed=args.seed,
        fp16=effective_fp16,
        bf16=False,
        tf32=False,
        use_cpu=not gpu_available,
        gradient_checkpointing=False,
        optim="adamw_torch",
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        save_only_model=True,
        load_best_model_at_end=True,
        metric_for_best_model=selection_metric,
        greater_is_better=True,
        batch_sampler=batch_sampler,
        logging_steps=1,
        logging_first_step=True,
        report_to="none",
        push_to_hub=False,
        dataloader_num_workers=0,
    )

    class ValidationHistory(TrainerCallback):

        def __init__(self):
            self.results = []

        def on_evaluate(self, args, state, control, metrics=None, **kwargs):
            metrics = metrics or {}
            if f"eval_{selection_metric}" not in metrics:
                raise RuntimeError(f"Selection metric {selection_metric} missing from trainer metrics: {sorted(metrics)}")
            self.results.append({
                "epoch": float(state.epoch or 0),
                "step": int(state.global_step),
                "metrics": numeric_metrics(metrics),
            })

    history = ValidationHistory()

    trainer = SentenceTransformerTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        loss=loss,
        evaluator=evaluator,
        callbacks=[history],
        # No eval_dataset: validation is used only by the evaluator.
    )

    training_started = time.perf_counter()
    train_result = trainer.train()
    training_seconds = time.perf_counter() - training_started

    best_checkpoint = trainer.state.best_model_checkpoint

    if not best_checkpoint:
        raise RuntimeError("No best checkpoint was saved; inspect the training/evaluation logs.")

    best_step = int(Path(best_checkpoint).name.rsplit("-", 1)[1])
    best_records = [row for row in history.results if row["step"] == best_step]

    if not best_records:
        raise RuntimeError("Cannot associate the best checkpoint with a validation epoch.")

    best_epoch = best_records[-1]["epoch"]

    # train() has reloaded the checkpoint selected by the active validation metric.
    after = numeric_metrics(evaluator(trainer.model))
    trainer.model.max_seq_length = args.max_seq_length
    trainer.model.save_pretrained(str(output_dir))
     # Final model is saved; the checkpoints are no longer needed.
    shutil.rmtree(checkpoint_dir)

    configuration = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items()
    }
    configuration.update({
        "dataset": args.dataset,
        "output_dir": str(output_dir),
        "effective_fp16": effective_fp16,
        "bf16": False,
        "device": device,
        "local_files_only": True,
        "effective_batch_size": args.batch_size * args.gradient_accumulation_steps,
        "loss_settings": loss_settings,
        "selection_metric": selection_metric,
        "training_arguments": training_args.to_dict(),
    })

    summary = {
        "configuration": configuration,
        "training_path": str(training_path),
        "validation_path": str(validation_path),
        "training_sha256": hashlib.sha256(training_path.read_bytes()).hexdigest(),
        "validation_sha256": hashlib.sha256(validation_path.read_bytes()).hexdigest(),
        "training_pairs_loaded": len(training),
        "training_pairs_used": len(used_training),
        "training_labels_loaded": label_counts(training) if args.dataset != "mnrl_triplets" else None,
        "training_labels_used": label_counts(used_training) if args.dataset != "mnrl_triplets" else None,
        "validation_pairs": len(validation),
        "validation_labels": label_counts(validation),
        "token_lengths": token_counts,
        "validation_before": before,
        "validation_per_epoch": history.results,
        "validation_after_best_checkpoint": after,
        "best_checkpoint": str(best_checkpoint),
        "best_epoch": best_epoch,
        "best_step": best_step,
        "best_validation_cosine_ap": (
            after[AP_METRIC] if args.dataset == "mnrl_triplets" else float(trainer.state.best_metric)
        ),
        "train_metrics": train_result.metrics,
        "training_seconds": training_seconds,
        "total_seconds": time.perf_counter() - started,
        "gpu_name": gpu_name,
        "gpu_memory_gb": torch.cuda.get_device_properties(0).total_memory / 1024**3 if gpu_available else None,
        "cuda_version": torch.version.cuda,
        "library_versions": {
            name: version(name)
            for name in ["torch", "sentence-transformers", "transformers", "datasets", "accelerate", "pandas", "numpy"]
        },
        "notes": [
            "Test rows are never used. Checkpoint selection uses validation cosine AP only.",
            "Evaluator F1 selects a threshold on validation; this is not fixed-threshold test performance.",
            "Initial and fine-tuned validation evaluations use the same max_seq_length.",
            "Use a new model path/id or clear that model's embedding cache after overwriting weights.",
            "max_steps > 0 overrides epochs and may produce a fractional best_epoch.",
        ],
    }
    if real_evaluator is not None:
        summary.update({
            "training_triplets": len(used_training),
            "real_validation_pairs": real_evaluator.pair_count,
            "real_validation_path": str(REAL_PAIRS_PATH),
            "real_validation_sha256": hashlib.sha256(REAL_PAIRS_PATH.read_bytes()).hexdigest(),
            "selection_metric": selection_metric,
            "best_selection_metric": float(trainer.state.best_metric),
            "loss_settings": loss_settings,
            "real_validation_before": {key: before[key] for key in REAL_METRICS},
            "real_validation_after_best_checkpoint": {key: after[key] for key in REAL_METRICS},
            "synthetic_validation_before": {
                key: value for key, value in before.items() if key.startswith(f"{EVALUATOR_NAME}_")
            },
            "synthetic_validation_after_best_checkpoint": {
                key: value for key, value in after.items() if key.startswith(f"{EVALUATOR_NAME}_")
            },
        })
        summary["notes"][0] = (
            "Test clients are used only for leakage checks. Checkpoint selection uses real_validation Recall@1; "
            "synthetic validation AP/F1 are monitoring metrics only."
        )

    with (output_dir / "training_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, allow_nan=False)

    print("=" * 70)
    print(f"BGE-M3 FINE-TUNING ({args.dataset.upper()})")
    if real_evaluator is not None:
        for name, metrics in [("Before training", before), ("Best checkpoint", after)]:
            print(
                f"{name}: real_validation Recall@1={metrics[REAL_METRIC]:.4f}, "
                f"Recall@3={metrics[REAL_METRICS[1]]:.4f}, MRR={metrics[REAL_METRICS[2]]:.4f} | "
                f"synthetic validation AP={metrics[AP_METRIC]:.4f}, F1={metrics[F1_METRIC]:.4f}"
            )
        print("Training triplets:", len(used_training), "| real_validation pairs:", real_evaluator.pair_count)
    else:
        print(f"Validation before: AP={before[AP_METRIC]:.4f}, F1={before[F1_METRIC]:.4f}")
        print(f"Validation best:   AP={after[AP_METRIC]:.4f}, F1={after[F1_METRIC]:.4f}")
    print("Best epoch:", best_epoch, "| step:", best_step)
    print(f"Elapsed: {summary['total_seconds']:.1f} seconds")
    print("Model saved:", output_dir)
    print("Summary:", output_dir / "training_summary.json")
    print("Add to MODELS in src/evaluate_embeddings_on_pairs.py:")
    print(json.dumps({
        "id": model_id, "name": str(output_dir),
        "backend": "sentence_transformers",
    }, ensure_ascii=False)[:-1] + ', "query_prompt": None}')
    print("=" * 70)


if __name__ == "__main__":
    main()
