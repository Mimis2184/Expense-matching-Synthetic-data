import argparse
import re
import sys
import unicodedata
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


# ============================================================
# Lexical score
#
# Word-level overlap coefficient between:
#
#   client  = expense title + expense description
#   program = category + subcategory
#
#   score = common words / words of the smaller side
#
# The overlap coefficient is used instead of cosine (or Jaccard)
# so that one meaningful common word is not diluted by the
# length of a long text.
#
# Expense classes are NOT included: client and program classes
# are the same by construction.
#
# Text normalization, applied identically to both sides:
#
#   1. lowercase
#   2. remove accents
#   3. Greek and Greeklish -> one common Latin form
#      (e.g. "εξοπλισμός" and "eksoplismos" -> "eksoplismos")
#   4. remove stopwords ("και", "για", "της", ...)
#
# Two words count as the same word when they share at least
# MIN_PREFIX_LENGTH first characters (e.g. "κτιρίου" / "κτίρια",
# "εξοπλισμός" / "εξοπλισμού") AND the shared beginning covers
# at least MIN_PREFIX_SHARE of the shorter word. The second
# condition stops different words with the same first letters
# from matching (e.g. "λογιστικό" / "λογισμικό",
# "κατεστράφη" / "κατασκευή"). A word shorter than
# MIN_PREFIX_LENGTH must match exactly.
#
# The same function must be used for the human examples and
# for newly generated pairs, so that one threshold means the
# same thing for both.
# ============================================================

MIN_PREFIX_LENGTH = 5

MIN_PREFIX_SHARE = 0.70

MIN_TOKEN_LENGTH = 2

GREEK_STOPWORDS = """
και του της των τον την το τα τη στο στη στην στα στον στις στους
σε με για από απο ως η ο οι ενός ένα ενα μια μία που είναι ειναι
κατά κατα προς επί επι ή αλλά καθώς όπως δεν μη μην θα να ανά ανα
περί λπ κλπ οτι ότι αυτό αυτή αυτά εκ έως εως μέσω μεσω
""".split()


# ------------------------------------------------------------
# Greek -> common Latin form
#
# Multi-letter combinations first, then single letters.
# Sounds written in several ways are unified:
#   η, ι, υ, ει, οι -> i
#   ο, ω            -> o
#   αι              -> e
# ------------------------------------------------------------

GREEK_DIGRAPHS = [
    ("ου", "u"),
    ("ει", "i"),
    ("οι", "i"),
    ("αι", "e"),
    ("αυ", "av"),
    ("ευ", "ev"),
    ("μπ", "b"),
    ("ντ", "d"),
    ("γκ", "g"),
    ("γγ", "g"),
]

GREEK_LETTERS = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e",
    "ζ": "z", "η": "i", "θ": "th", "ι": "i", "κ": "k",
    "λ": "l", "μ": "m", "ν": "n", "ξ": "ks", "ο": "o",
    "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t",
    "υ": "i", "φ": "f", "χ": "x", "ψ": "ps", "ω": "o",
}

# ------------------------------------------------------------
# Greeklish -> the same common Latin form
#
# Greeklish has no single standard; the most common spellings
# are covered.
# ------------------------------------------------------------

LATIN_RULES = [
    ("8", "th"),
    ("ou", "u"),
    ("ei", "i"),
    ("oi", "i"),
    ("ai", "e"),
    ("au", "av"),
    ("eu", "ev"),
    ("mp", "b"),
    ("nt", "d"),
    ("ch", "x"),
    ("ph", "f"),
    ("y", "i"),
    ("w", "o"),
]


# ============================================================
# Normalization
# ============================================================

def remove_accents(
    text: str,
) -> str:

    return "".join(
        character
        for character in unicodedata.normalize("NFD", text)
        if unicodedata.category(character) != "Mn"
    )


def to_common_latin(
    token: str,
) -> str:
    """
    Convert one lowercase, accent-free token (Greek or Greeklish)
    to the common Latin form.
    """

    for greek, latin in GREEK_DIGRAPHS:
        token = token.replace(greek, latin)

    token = "".join(
        GREEK_LETTERS.get(character, character)
        for character in token
    )

    for source, target in LATIN_RULES:
        token = token.replace(source, target)

    # Double letters (λλ, ππ, "ll") count as single letters.
    token = re.sub(r"(.)\1+", r"\1", token)

    return token


def tokenize(
    text,
) -> list[str]:

    if pd.isna(text):
        return []

    text = remove_accents(
        str(text).casefold()
    )

    return re.findall(
        r"[a-zα-ω0-9]+",
        text,
    )


STOPWORDS = frozenset(
    to_common_latin(token)
    for word in GREEK_STOPWORDS
    for token in tokenize(word)
)


def normalize_words(
    *texts,
) -> list[str]:
    """
    Full normalization of one side of a pair.
    Returns the set of distinct normalized words.
    """

    words = set()

    for text in texts:

        for token in tokenize(text):

            token = to_common_latin(token)

            if token in STOPWORDS:
                continue

            if len(token) < MIN_TOKEN_LENGTH:
                continue

            words.add(token)

    return words


def same_word(
    first: str,
    second: str,
) -> bool:
    """
    Words match when their shared beginning is at least
    MIN_PREFIX_LENGTH characters long and covers at least
    MIN_PREFIX_SHARE of the shorter word. A word shorter than
    MIN_PREFIX_LENGTH must match exactly.
    """

    if (
        len(first) < MIN_PREFIX_LENGTH
        or len(second) < MIN_PREFIX_LENGTH
    ):
        return first == second

    shared = 0

    for first_character, second_character in zip(first, second):

        if first_character != second_character:
            break

        shared += 1

    return (
        shared >= MIN_PREFIX_LENGTH
        and shared >= MIN_PREFIX_SHARE * min(len(first), len(second))
    )


# ============================================================
# Score
# ============================================================

def lexical_score(
    client_title,
    client_description,
    program_category,
    program_subcategory,
) -> float:
    """
    Overlap coefficient of normalized words:

        words of the smaller side that also appear on the
        other side / number of words of the smaller side

    0 = no common words, 1 = every word of the smaller side
    also appears on the other side.
    """

    client_words = normalize_words(
        client_title,
        client_description,
    )

    program_words = normalize_words(
        program_category,
        program_subcategory,
    )

    if not client_words or not program_words:
        return 0.0

    smaller, larger = sorted(
        [client_words, program_words],
        key=len,
    )

    common = sum(
        1
        for word in smaller
        if any(
            same_word(word, other)
            for other in larger
        )
    )

    return common / len(smaller)


# ============================================================
# Score an expense-match Excel
# ============================================================

INPUT_COLUMNS = {
    "client_title": "Client Expense",
    "client_description": "Client Expense Description",
    "program_category": "Ideal Expense Category",
    "program_subcategory": "Ideal Expense Subcategory",
}

SCORE_COLUMN = "new_score"


def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Add a new_score lexical score column to an "
            "expense-match Excel, sorted from lowest to highest."
        )
    )

    parser.add_argument(
        "input_path",
        type=Path,
    )

    parser.add_argument(
        "--output-path",
        type=Path,
        default=None,
    )

    args = parser.parse_args()

    output_path = args.output_path or (
        BASE_DIR
        / "data"
        / "processed"
        / f"{args.input_path.stem}_new_score.xlsx"
    )

    df = pd.read_excel(
        args.input_path,
    )

    missing_columns = [
        column
        for column in INPUT_COLUMNS.values()
        if column not in df.columns
    ]

    if missing_columns:
        raise ValueError(
            "Missing required columns: "
            f"{missing_columns}"
        )

    scores = [
        round(
            lexical_score(
                row[INPUT_COLUMNS["client_title"]],
                row[INPUT_COLUMNS["client_description"]],
                row[INPUT_COLUMNS["program_category"]],
                row[INPUT_COLUMNS["program_subcategory"]],
            ),
            4,
        )
        for _, row in df.iterrows()
    ]

    # --------------------------------------------------------
    # new_score right after the existing Score column
    # --------------------------------------------------------

    insert_position = (
        df.columns.get_loc("Score") + 1
        if "Score" in df.columns
        else len(df.columns)
    )

    df.insert(
        insert_position,
        SCORE_COLUMN,
        scores,
    )

    df = (
        df
        .sort_values(
            SCORE_COLUMN,
            kind="mergesort",
        )
        .reset_index(drop=True)
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    df.to_excel(
        output_path,
        index=False,
        engine="openpyxl",
    )

    print("=" * 70)
    print("LEXICAL SCORE")
    print("=" * 70)
    print("Pairs scored:", len(df))
    print(df[SCORE_COLUMN].describe().round(3).to_string())
    print()
    print("Saved:", output_path)
    print("=" * 70)


if __name__ == "__main__":
    main()
