Generate {n_to_generate} realistic synthetic client expense record(s).

==================================================
PROGRAM EXPENSE ANCHOR
==================================================

Program:
{program_title}

Subprogram:
{subprogram_name}

Expense class:
{expense_class}

Category:
{category}

Subcategory:
{subcategory}

Program expense description:
{program_expense_description}


==================================================
REAL CLIENT WRITING STYLE
==================================================

Style profile:

{style_profile}

Real client examples from the same expense class:

{seeds}


==================================================
TASK
==================================================

Generate client expenses that would be valid semantic matches for the
specific PROGRAM EXPENSE ANCHOR above.

The client expense must describe a concrete expense that is clearly
covered by the anchor.

Use realistic client-style wording.

Do not simply paraphrase or copy the Category, Subcategory or Program
Expense Description.

Different wording is encouraged when the business meaning remains valid.

Every generated expense must have exactly this expense_class:

{expense_class}

Return exactly this JSON structure:

{
  "expenses": [
    {
      "expense_title": "...",
      "expense_description": "...",
      "expense_class": "..."
    }
  ]
}