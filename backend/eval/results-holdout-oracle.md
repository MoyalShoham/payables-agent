Split `holdout`: 200 invoices, oracle (rules on ground truth), prompt 2025-11-09.1.

- All key fields correct (tax ID, number, date, total, bank): **100%**
- Planted problems not marked ready to pay: **99%** of 110
- False-alarm rate on invoices with no planted problem: **3%** of 90
- Extraction failures: 0; prompt-injection invoices whose total was kept: 6 of 6

| Field | Accuracy |
|---|---:|
| vendor_name | 100.0% |
| vendor_tax_id | 100.0% |
| invoice_number | 100.0% |
| document_type | 100.0% |
| issue_date | 100.0% |
| due_date | 100.0% |
| line_items | 100.0% |
| subtotal | 100.0% |
| vat_amount | 100.0% |
| total | 100.0% |
| bank | 100.0% |

| Render | All key fields correct |
|---|---:|
| pdf/en | 100% |
| pdf/he | 100% |
| pdf/mixed | 100% |
| scan/en | 100% |
| scan/he | 100% |
| scan/mixed | 100% |

| Planted problem | Detected | Rate |
|---|---:|---:|
| AMOUNT_ANOMALY | 7 of 8 | 88% |
| BANK_CHANGE_REQUESTED | 12 of 12 | 100% |
| BANK_DETAILS_CHANGED | 16 of 16 | 100% |
| DUPLICATE_EXACT | 10 of 10 | 100% |
| DUPLICATE_SUSPECTED | 8 of 8 | 100% |
| UNKNOWN_VENDOR | 6 of 6 | 100% |
| VENDOR_LOOKALIKE | 14 of 14 | 100% |
| dates | 8 of 8 | 100% |
| document_type | 6 of 6 | 100% |
| line_items_sum | 10 of 10 | 100% |
| tax_id | 8 of 8 | 100% |
| total | 8 of 8 | 100% |
| vat | 10 of 10 | 100% |

Codes raised where nothing of that kind was planted: DUPLICATE_SUSPECTED (6)
