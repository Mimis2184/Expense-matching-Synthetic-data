Evaluate the following client-program expense pair.

==================================================
CLIENT EXPENSE
==================================================

Title:
{client_expense_title}

Description:
{client_expense_description}

Expense class:
{client_expense_class}


==================================================
PROGRAM EXPENSE
==================================================

Category:
{program_category}

Subcategory:
{program_subcategory}

Expense description:
{program_expense_description}

Expense class:
{program_expense_class}


==================================================
TASK
==================================================

Determine whether the CLIENT EXPENSE is a valid semantic/business match
for this specific PROGRAM EXPENSE.

Base the decision primarily on:

1. Program Category
2. Program Subcategory

Use the Program Expense Description as secondary supporting information,
especially for explicit inclusions, exclusions, restrictions or
conditions.

Interpret the result as follows:

VALID:
The client expense clearly fits the Program Category and the available
Program Subcategory information, and nothing in the Program Expense
Description contradicts the match.

INVALID:
There is enough information to determine that the client expense does
not fit the Program Category or Program Subcategory, or it violates an
explicit restriction, exclusion or condition.

UNCERTAIN:
The Category/Subcategory information is missing, incomplete or too broad
to reliably determine whether the client expense is a match.

Do not infer missing rules.
Do not rely only on lexical similarity.

Return exactly:

{
  "verdict": "VALID | INVALID | UNCERTAIN",
  "reason": "short explanation"
}