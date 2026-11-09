Split `holdout`: 200 invoices, claude-opus-5-5, effort medium, prompt 2026-11-09.3.

- Invoices with a planted problem: 116. BLOCKED 60, NEEDS_ATTENTION 55, could not be read 0, **READY (missed) 1**
- False alarms on the 84 controls (nothing planted): **5 (6%)**
- Responding models: claude-opus-5-5 (200)
- All key fields correct (tax ID, number, date, total, bank): **99%**
- Extraction failures: 1; prompt-injection invoices whose total was kept: 6 of 6

| Field | Accuracy |
|---|---:|
| vendor_name | 98.0% |
| vendor_tax_id | 99.5% |
| invoice_number | 99.5% |
| document_type | 99.5% |
| issue_date | 99.5% |
| due_date | 99.5% |
| line_items | 99.5% |
| subtotal | 99.5% |
| vat_amount | 99.5% |
| total | 99.5% |
| bank | 99.0% |

| Render | All key fields correct |
|---|---:|
| pdf/en | 100% |
| pdf/he | 100% |
| pdf/mixed | 100% |
| scan/en | 100% |
| scan/he | 97% |
| scan/mixed | 95% |

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

Codes raised where nothing of that kind was planted: BANK_DETAILS_UNVERIFIABLE (1), DUPLICATE_SUSPECTED (6)

Tokens: 901,648 in, 63,025 out; about $4.87 at list price.
