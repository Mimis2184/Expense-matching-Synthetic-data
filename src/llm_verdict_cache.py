import hashlib
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(
    0,
    str(BASE_DIR),
)


from src.easy_negative_candidate_selection import (
    NEGATIVE_VALIDATOR_SYSTEM_PROMPT_PATH,
    NEGATIVE_VALIDATOR_PROMPT_PATH,
    PairValidationResult,
    clean_value,
)


# ============================================================
# Shared LLM verdict cache
#
# One row per LLM-validated Client Expense <-> Program Expense pair.
#
# A cached verdict is reused only when BOTH match:
#
# - pair_fingerprint: hash of the exact texts sent to the validator
# - prompt_hash:      hash of the validator system prompt + template
#
# If the validator prompt changes, old verdicts are NOT reused.
# ============================================================

VERDICT_CACHE_PATH = (
    BASE_DIR
    / "data/output/llm_pair_verdicts.csv"
)

EASY_VALIDATION_REPORT_PATH = (
    BASE_DIR
    / "data/output/checkpoint_easy_negative_report.csv"
)

CACHE_COLUMNS = [
    "analysis_client_id",
    "candidate_program_expense_id",
    "pair_fingerprint",
    "prompt_hash",
    "verdict",
    "reason",
    "source",
    "validated_at",
]

# Exactly the fields that build_validation_prompt() sends to the LLM.
PAIR_TEXT_FIELDS = [
    "client_expense_title",
    "client_expense_description",
    "client_expense_class",
    "program_category",
    "program_subcategory",
    "program_expense_description",
    "program_expense_class",
]


# ============================================================
# Hashes
# ============================================================

def compute_prompt_hash() -> str:

    digest = hashlib.sha256()

    for path in [
        NEGATIVE_VALIDATOR_SYSTEM_PROMPT_PATH,
        NEGATIVE_VALIDATOR_PROMPT_PATH,
    ]:
        digest.update(path.read_bytes())
        digest.update(b"\n<<prompt-file-boundary>>\n")

    return digest.hexdigest()[:16]


def _normalize_fingerprint_text(
    value,
) -> str:

    text = unicodedata.normalize(
        "NFC",
        clean_value(value),
    )

    return " ".join(text.split())


def compute_pair_fingerprint(
    row: pd.Series,
) -> str:

    digest = hashlib.sha256()

    for field in PAIR_TEXT_FIELDS:
        digest.update(field.encode("utf-8"))
        digest.update(b"=")
        digest.update(
            _normalize_fingerprint_text(row[field]).encode("utf-8")
        )
        digest.update(b"\x1f")

    return digest.hexdigest()[:24]


# ============================================================
# Cache access
# ============================================================

class VerdictCache:

    def __init__(
        self,
        path: Path = VERDICT_CACHE_PATH,
    ) -> None:

        self.path = path

        # Lookup is by content: (pair_fingerprint, prompt_hash).
        # Different program ids can carry identical texts, so the
        # same verdict may be stored under several id pairs.
        self._verdicts: dict[tuple[str, str], dict] = {}
        self._stored_keys: set[tuple[int, int, str, str]] = set()

        if self.path.exists():

            cache_df = pd.read_csv(
                self.path,
                encoding="utf-8-sig",
                dtype={
                    "pair_fingerprint": str,
                    "prompt_hash": str,
                },
            )

            missing_columns = set(CACHE_COLUMNS) - set(cache_df.columns)

            if missing_columns:
                raise ValueError(
                    "Verdict cache is missing columns: "
                    f"{sorted(missing_columns)}"
                )

            for record in cache_df.to_dict(orient="records"):
                self._remember(record)

    def __len__(self) -> int:
        return len(self._stored_keys)

    @staticmethod
    def _stored_key(
        record: dict,
    ) -> tuple[int, int, str, str]:

        return (
            int(record["analysis_client_id"]),
            int(record["candidate_program_expense_id"]),
            record["pair_fingerprint"],
            record["prompt_hash"],
        )

    def _remember(
        self,
        record: dict,
    ) -> None:

        self._stored_keys.add(self._stored_key(record))

        self._verdicts.setdefault(
            (record["pair_fingerprint"], record["prompt_hash"]),
            record,
        )

    def get(
        self,
        pair_fingerprint: str,
        prompt_hash: str,
    ) -> dict | None:

        return self._verdicts.get(
            (pair_fingerprint, prompt_hash)
        )

    def add(
        self,
        analysis_client_id: int,
        candidate_program_expense_id: int,
        pair_fingerprint: str,
        prompt_hash: str,
        result: PairValidationResult,
        source: str,
        validated_at: str | None = None,
    ) -> None:

        """
        Store a verdict and append it to the CSV immediately,
        so a crash never loses an LLM call that was already paid for.
        """

        record = {
            "analysis_client_id": analysis_client_id,
            "candidate_program_expense_id": candidate_program_expense_id,
            "pair_fingerprint": pair_fingerprint,
            "prompt_hash": prompt_hash,
            "verdict": result.verdict,
            "reason": result.reason,
            "source": source,
            "validated_at": (
                validated_at
                or datetime.now(timezone.utc).isoformat(timespec="seconds")
            ),
        }

        if self._stored_key(record) in self._stored_keys:
            return

        write_header = not self.path.exists()

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        # The BOM is written only when the file is created;
        # appending with utf-8-sig would insert a BOM mid-file.
        pd.DataFrame(
            [record],
            columns=CACHE_COLUMNS,
        ).to_csv(
            self.path,
            mode="a",
            header=write_header,
            index=False,
            encoding="utf-8-sig" if write_header else "utf-8",
        )

        self._remember(record)


# ============================================================
# Initial build from the existing easy-negative validations
# ============================================================

def seed_cache_from_easy_report(
    cache: VerdictCache,
) -> tuple[int, int]:

    """
    Import every LLM verdict of the easy-negative run.

    The easy report has no per-call timestamp, so validated_at is the
    modification time of the report file (the end of the easy run).
    The validator prompts were last modified before that run, so the
    current prompt hash is the one those verdicts were produced with.
    """

    report_df = pd.read_csv(
        EASY_VALIDATION_REPORT_PATH,
        encoding="utf-8-sig",
    )

    llm_rows = report_df[
        report_df["used_llm"].astype(str).str.casefold() == "true"
    ]

    report_time = datetime.fromtimestamp(
        EASY_VALIDATION_REPORT_PATH.stat().st_mtime,
        tz=timezone.utc,
    ).isoformat(timespec="seconds")

    prompt_hash = compute_prompt_hash()

    size_before = len(cache)

    for _, row in llm_rows.iterrows():

        cache.add(
            analysis_client_id=int(row["analysis_client_id"]),
            candidate_program_expense_id=int(
                row["candidate_program_expense_id"]
            ),
            pair_fingerprint=compute_pair_fingerprint(row),
            prompt_hash=prompt_hash,
            result=PairValidationResult(
                verdict=clean_value(row["validation_verdict"]),
                reason=clean_value(row["validation_reason"]),
            ),
            source="easy",
            validated_at=report_time,
        )

    return (
        len(llm_rows),
        len(cache) - size_before,
    )


def main() -> None:

    cache = VerdictCache()

    (
        easy_llm_rows,
        added,
    ) = seed_cache_from_easy_report(cache)

    cache_df = pd.read_csv(
        VERDICT_CACHE_PATH,
        encoding="utf-8-sig",
    )

    print("=" * 70)
    print("LLM VERDICT CACHE")
    print("=" * 70)
    print("Easy-run LLM verdicts found:", easy_llm_rows)
    print("Newly added to cache:", added)
    print("Total cached verdicts:", len(cache_df))
    print("Prompt hash:", compute_prompt_hash())
    print()
    print(cache_df.groupby(["source", "verdict"]).size().to_string())
    print()
    print("Cache:", VERDICT_CACHE_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
