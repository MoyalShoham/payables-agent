Split `dev`: 100 invoices, oracle (rules on ground truth), prompt 2026-11-09.3.

- Invoices with a planted problem: 58. BLOCKED 30, NEEDS_ATTENTION 27, could not be read 0, **READY (missed) 1**
- False alarms on the 42 controls (nothing planted): **0 (0%)**

| Planted problem | Detected | Rate |
|---|---:|---:|
| AMOUNT_ANOMALY | 3 of 4 | 75% |
| BANK_CHANGE_REQUESTED | 6 of 6 | 100% |
| BANK_DETAILS_CHANGED | 8 of 8 | 100% |
| DUPLICATE_EXACT | 5 of 5 | 100% |
| DUPLICATE_SUSPECTED | 4 of 4 | 100% |
| INSTRUCTION_TEXT | 3 of 3 | 100% |
| UNKNOWN_VENDOR | 3 of 3 | 100% |
| VENDOR_LOOKALIKE | 7 of 7 | 100% |
| dates | 4 of 4 | 100% |
| document_type | 3 of 3 | 100% |
| line_items_sum | 5 of 5 | 100% |
| tax_id | 4 of 4 | 100% |
| total | 4 of 4 | 100% |
| vat | 5 of 5 | 100% |

Codes raised where nothing of that kind was planted: DUPLICATE_SUSPECTED (2)
