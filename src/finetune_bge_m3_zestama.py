import argparse
import hashlib
import json
import math
import os
import sys
import shutil
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

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
from src.evaluate_embeddings_on_pairs import (
    VALIDATION_PATH,
    build_texts,
    load_pairs,
)


# ============================================================
# BGE-M3 fine-tuning (zestama / final_v05)
#
# Training texts are used exactly as stored in the CSV.
# Zestama validation uses the baseline loader and text formatter.
# Final v0.5 uses the stored training / validation texts as-is.
# Validation is only an evaluator, never a training dataset.
# Test rows are never used for training or checkpoint selection.
#
# Best checkpoint: validation cosine average precision.
# The evaluator's F1 uses its own validation-optimal threshold;
# it is not the baseline's fixed-threshold test F1.
#
# One T4: fp16, no bf16, gradient checkpointing by default.
# No API call. Existing input data files are not modified.
# ============================================================

TRAINING_PATH = PROCESSED_DATA_DIR / "training_pairs_poc.csv"
FINAL_PATH = PROCESSED_DATA_DIR / "final_pairs_v05.csv"
DEFAULT_OUTPUT_DIR = BASE_DIR / "models" / "bge-m3-ft-zestama"
DEFAULT_FINAL_OUTPUT_DIR = BASE_DIR / "models" / "bge-m3-ft-final-v05"
MODEL_NAME = "BAAI/bge-m3"
EVALUATOR_NAME = "validation"
AP_METRIC = f"{EVALUATOR_NAME}_cosine_ap"
F1_METRIC = f"{EVALUATOR_NAME}_cosine_f1"


# ============================================================
# Inputs and safety checks
# ============================================================

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
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Fine-tune BGE-M3 on synthetic POC pairs (validation-only checkpoint selection)."
    )

    parser.add_argument("--model-name-or-path", default=MODEL_NAME)
    parser.add_argument("--dataset", choices=["zestama", "final_v05"], default="zestama")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--allow-cpu", action="store_true", help="Allow a slow CPU smoke test; disables fp16.")
    parser.add_argument("--loss", choices=["OnlineContrastiveLoss", "MultipleNegativesRankingLoss"], default="OnlineContrastiveLoss")
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

    default_output_dir = (
        DEFAULT_FINAL_OUTPUT_DIR if args.dataset == "final_v05" else DEFAULT_OUTPUT_DIR
    )
    output_dir = (args.output_dir or default_output_dir).expanduser().resolve()
    training_path = FINAL_PATH if args.dataset == "final_v05" else TRAINING_PATH
    validation_path = FINAL_PATH if args.dataset == "final_v05" else VALIDATION_PATH
    model_id = "bge-m3-ft-final-v05" if args.dataset == "final_v05" else "bge-m3-ft-zestama"

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
        from sentence_transformers.evaluation import BinaryClassificationEvaluator
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

    if args.loss == "MultipleNegativesRankingLoss":
        used_training = training.loc[training["label"].eq(1)].copy()
        if len(used_training) < 2 or args.batch_size < 2:
            raise ValueError("MultipleNegativesRankingLoss needs at least two positive pairs and batch size >= 2.")

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

    token_counts = {
        "training_loaded": token_length_report(
            model.tokenizer,
            training["client_text"].tolist() + training["program_text"].tolist(),
            args.max_seq_length,
        ),
        "validation": token_length_report(
            model.tokenizer, validation_clients + validation_programs, args.max_seq_length,
        ),
    }

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

    before = numeric_metrics(evaluator(model))

    if AP_METRIC not in before or F1_METRIC not in before:
        raise RuntimeError(f"Unexpected evaluator metric keys: {sorted(before)}")

    print(f"Before training: validation AP={before[AP_METRIC]:.4f}, F1={before[F1_METRIC]:.4f}")

    dataset = Dataset.from_dict({
        "sentence1": used_training["client_text"].tolist(),
        "sentence2": used_training["program_text"].tolist(),
        "label": used_training["label"].tolist(),
    })

    if args.loss == "OnlineContrastiveLoss":
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
        metric_for_best_model=AP_METRIC,
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
            if f"eval_{AP_METRIC}" not in metrics:
                raise RuntimeError(f"Validation AP missing from trainer metrics: {sorted(metrics)}")
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

    # train() has reloaded the checkpoint selected by validation cosine AP.
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
        "training_labels_loaded": label_counts(training),
        "training_labels_used": label_counts(used_training),
        "validation_pairs": len(validation),
        "validation_labels": label_counts(validation),
        "token_lengths": token_counts,
        "validation_before": before,
        "validation_per_epoch": history.results,
        "validation_after_best_checkpoint": after,
        "best_checkpoint": str(best_checkpoint),
        "best_epoch": best_epoch,
        "best_step": best_step,
        "best_validation_cosine_ap": float(trainer.state.best_metric),
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

    with (output_dir / "training_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, allow_nan=False)

    print("=" * 70)
    print(f"BGE-M3 FINE-TUNING ({args.dataset.upper()})")
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
