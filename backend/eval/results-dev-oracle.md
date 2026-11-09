Split `dev`: 100 invoices, oracle (rules on ground truth), prompt 2025-11-09.1.

- All key fields correct (tax ID, number, date, total, bank): **100%**
- Planted problems not marked ready to pay: **98%** of 55
- False-alarm rate on invoices with no planted problem: **2%** of 45
- Extraction failures: 0; prompt-injection invoices whose total was kept: 3 of 3

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
| AMOUNT_ANOMALY | 3 of 4 | 75% |
| BANK_CHANGE_REQUESTED | 6 of 6 | 100% |
| BANK_DETAILS_CHANGED | 8 of 8 | 100% |
| DUPLICATE_EXACT | 5 of 5 | 100% |
| DUPLICATE_SUSPECTED | 4 of 4 | 100% |
| UNKNOWN_VENDOR | 3 of 3 | 100% |
| VENDOR_LOOKALIKE | 7 of 7 | 100% |
| dates | 4 of 4 | 100% |
| document_type | 3 of 3 | 100% |
| line_items_sum | 5 of 5 | 100% |
| tax_id | 4 of 4 | 100% |
| total | 4 of 4 | 100% |
| vat | 5 of 5 | 100% |

Codes raised where nothing of that kind was planted: DUPLICATE_SUSPECTED (2)
