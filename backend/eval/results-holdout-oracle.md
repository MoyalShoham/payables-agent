Split `holdout`: 200 invoices, oracle (rules on ground truth), prompt 2026-11-09.3.

- Invoices with a planted problem: 116. BLOCKED 60, NEEDS_ATTENTION 55, could not be read 0, **READY (missed) 1**
- False alarms on the 84 controls (nothing planted): **3 (4%)**

| Planted problem | Detected | Rate |
|---|---:|---:|
| AMOUNT_ANOMALY | 7 of 8 | 88% |
| BANK_CHANGE_REQUESTED | 12 of 12 | 100% |
| BANK_DETAILS_CHANGED | 16 of 16 | 100% |
| DUPLICATE_EXACT | 10 of 10 | 100% |
| DUPLICATE_SUSPECTED | 8 of 8 | 100% |
| INSTRUCTION_TEXT | 6 of 6 | 100% |
| UNKNOWN_VENDOR | 6 of 6 | 100% |
| VENDOR_LOOKALIKE | 14 of 14 | 100% |
| dates | 8 of 8 | 100% |
| document_type | 6 of 6 | 100% |
| line_items_sum | 10 of 10 | 100% |
| tax_id | 8 of 8 | 100% |
| total | 8 of 8 | 100% |
| vat | 10 of 10 | 100% |

Codes raised where nothing of that kind was planted: DUPLICATE_SUSPECTED (6)
