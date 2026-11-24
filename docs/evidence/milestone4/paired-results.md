# Milestone 4 paired extraction results

Synthetic held-out corpus **m4-held-out-v1**, 12 PDFs. Both pipelines used **qwen3:8b** on Ollama **0.30.11** with LangGraph **1.2.24**.

One unscored warm-up used the walkthrough PDF. The graph was forced for every pair. Run order alternated across PDFs. Times include extraction and backend validation.

Correct counts expected populated values. Edits estimates source-grounding corrections from values, evidence, and extra rows.

| PDF | Case | Expected routing | Baseline correct / edits / seconds | Graph correct / edits / seconds | Baseline → graph routing | Graph calls / rereads |
| --- | --- | --- | --- | --- | --- | --- |
| E01 | clean | ready | 9 / 1 / 7.92 | 9 / 1 / 15.09 | extraction_review → extraction_review | 3 / 1 |
| E02 | clean | ready | 6 / 0 / 6.10 | 6 / 0 / 7.93 | ready → ready | 2 / 0 |
| E03 | multipage | ready | 9 / 0 / 9.89 | 9 / 0 / 11.74 | ready → ready | 2 / 0 |
| E04 | multipage alias | ready | 9 / 0 / 9.43 | 9 / 0 / 11.79 | ready → ready | 2 / 0 |
| E05 | price mismatch | business_review | 6 / 1 / 5.14 | 6 / 1 / 12.22 | extraction_review → extraction_review | 3 / 1 |
| E06 | multipage price mismatch | business_review | 9 / 0 / 8.56 | 9 / 0 / 10.74 | business_review → business_review | 2 / 0 |
| E07 | missing PO | extraction_review | 4 / 3 / 6.24 | 4 / 3 / 15.61 | extraction_review → extraction_review | 3 / 1 |
| E08 | missing quantity | extraction_review | 8 / 1 / 9.89 | 8 / 1 / 12.28 | extraction_review → extraction_review | 2 / 0 |
| E09 | unselected price options | extraction_review | 5 / 3 / 7.81 | 5 / 3 / 19.26 | extraction_review → extraction_review | 3 / 1 |
| E10 | two labeled PO numbers | extraction_review | 5 / 1 / 6.86 | 5 / 1 / 8.85 | extraction_review → extraction_review | 2 / 0 |
| E11 | repeated SKU | ready | 9 / 0 / 10.75 | 9 / 0 / 26.96 | ready → ready | 2 / 0 |
| E12 | unsupported price precision | extraction_review | 6 / 0 / 6.87 | 6 / 0 / 8.86 | extraction_review → extraction_review | 2 / 0 |

| Measure | Baseline | LangGraph |
| --- | --- | --- |
| Correct fields | 85/86 (98.8%) | 85/86 (98.8%) |
| Correct fields with verified evidence | 80/86 (93.0%) | 80/86 (93.0%) |
| Correct expected blanks / ambiguities | 0/4 (0.0%) | 0/4 (0.0%) |
| Correct routing | 10 | 10 |
| Estimated corrections | 10 | 10 |
| Missing rows | 0 | 0 |
| Extra rows | 0 | 0 |
| Median time (seconds) | 7.87 | 12.01 |
| Initial extraction failures | 0 | 0 |
| Follow-up call or schema failures with fallback | 0 | 0 |

| Field | Baseline correct | Baseline verified | Graph correct | Graph verified |
| --- | --- | --- | --- | --- |
| po_number | 10/10 | 10/10 | 10/10 | 10/10 |
| customer_reference | 11/12 | 11/12 | 11/12 | 11/12 |
| currency | 12/12 | 12/12 | 12/12 | 12/12 |
| sku | 18/18 | 16/18 | 18/18 | 16/18 |
| quantity | 17/17 | 14/17 | 17/17 | 14/17 |
| unit_price | 17/17 | 17/17 | 17/17 | 17/17 |
