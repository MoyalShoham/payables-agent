# Design decisions

## 1. The model reads; code decides

**Chosen:** Claude extracts fields into a JSON schema, and nothing else. Every judgement is plain
Python with `Decimal` arithmetic: does it add up, is the VAT right for its date, is it a duplicate,
did the bank account change.

**Rejected:**
- asking the model "is this invoice OK?" or letting it propose approval;
- an agent loop with tools that could call "approve".

**Why:**
- Checks in code are deterministic, testable and auditable: every line of a draft's reasoning
  comes from a named check.
- There is no "approve" anywhere the model can reach, so a prompt-injected invoice cannot approve
  itself. It can still steer what is extracted: a document can make the model misread every field
  consistently (inflate all amounts, print another vendor's tax ID, hide a bank notice). The checks
  only catch inconsistencies, so that risk is reduced, not removed. Two things cover the rest: money
  only goes to the vendor-master account, and text addressed to the processor raises an
  INSTRUCTION_TEXT flag that sends the draft to a person who compares it with the document.

## 2. Copy what is printed, then check it

**Chosen:** the prompt tells the model to copy values exactly as printed, wrong VAT lines included,
and never to compute or correct.

**Why:** a model that silently fixes `VAT ₪1,700` into `₪1,800` hides the very error accounts
payable must catch. Extraction and validation are separate jobs: extraction measures fidelity to the
document, and validation measures the document. A test fails if this rule is removed from the prompt.

## 3. Money never touches a float

**Chosen:**
- The JSON schema returns amounts as strings, which Pydantic parses into `Decimal` with banker's
  rounding (floats are rejected outright). Amounts are capped at twelve integer digits, what
  `NUMERIC(14,2)` holds.
- Postgres stores `NUMERIC(14,2)`, and the API returns decimal strings.
- The anomaly statistic (log amounts, median, MAD) is computed with `Decimal.ln` and `Decimal.exp`.

**Why:** structured outputs guarantee the shape of the answer, not its numeric type. A float would
lose precision before any check ran.

## 4. Changed bank details need a structured callback on a number already on file

**Chosen:**
1. Payments go to the vendor-master account, never the account printed on an invoice. The payee is
   resolved again at approval; if the master changed since drafting, approval is refused until a
   re-check.
2. An invoice whose bank details differ from the master in any field, or cannot be compared (an
   account number without a branch), or any bank-change wording in the invoice or the covering
   email, sets `requires_bank_verification`.
3. Approval then needs a callback: the number dialled, which must be one of the vendor's phones on
   the master; who confirmed; and when (not in the future, within 7 days). A `CHECK` constraint
   requires all three to be non-blank on an approved bank-change draft.

**Rejected:**
- a confidence score that lets "probably fine" bank changes through;
- updating the vendor master from an invoice;
- a free-text note (the first version), which accepted fifteen characters of anything.

**Why:** business email compromise works because the change looks legitimate. The control that works
in practice is out-of-band verification on a known number, so the software asks for exactly that.
It cannot prove the call happened; a second approver for bank changes is the next step.

## 5. Separation of duties in the database, not only the code

**Chosen:**
- Tables belong to `payables_owner`, a role that cannot log in. The API connects as
  `payables_api`, the inbox agent as `payables_agent`; neither owns anything, so neither can alter a
  table or disable a trigger. Only migrations and the demo seed use the bootstrap superuser.
- `payables_agent` may insert documents, invoices, undecided drafts and audit entries. A trigger
  rejects any decision it tries to record, column grants keep it off the decision columns and the
  password hashes, and it has no grant on `simulated_payments`.
- Only `APPROVER`s approve, never a document they submitted by any channel. `CHECK (decided_by <>
  prepared_by)` and the callback `CHECK` hold for every role, a decided draft is immutable, and a
  payment needs an approved draft with the same amount and currency.

**Rejected:** one shared superuser connection (the first version). The review showed that code in the
agent's container could approve a payment and log it as the approver.

**Why:** an application bug, a future refactor or a compromised agent process should not be able to
let the preparer approve its own payment. The tests connect as each role and try exactly that.

## 6. An HMAC-chained, append-only audit log with checkpoints outside the database

**Chosen:**
- Every action is written in the same transaction as the change it records.
- Each entry has a gap-free sequence number and an HMAC-SHA256 over the previous hash, the actor id
  and every field, keyed with a secret kept outside the database. Appends are serialised with an
  advisory lock so the chain cannot fork.
- A trigger rejects `UPDATE`, `DELETE` and `TRUNCATE`, and the application roles cannot disable it.
- After each decision, and from `python -m payables.audit checkpoint`, the head sequence number and
  hash are appended to a file outside the database. `verify` checks every link, the sequence, and
  that the latest checkpoint is still in the chain.

**Rejected:** a plain SHA-256 chain (the first version). Anyone who could disable the trigger could
rewrite an entry and recompute every later hash, and deleting the newest entries left a valid chain.

**Why:** the trigger stops ordinary mistakes; the key stops a database-only attacker from forging
history; the checkpoints catch truncation up to the last checkpoint. What remains (entries after the
last checkpoint, an attacker with both the key and the host) is stated in the README. Production
would put checkpoints in WORM storage.

## 7. Record once, replay everywhere

**Chosen:**
- Every model response can be stored, keyed by model, effort, a hash of the system prompt and of the
  exact user text (which contains the covering message), the schema hash and the document's bytes.
- The evaluation, CI and the offline demo replay these recordings. A key mismatch fails loudly.
- Recordings and reports keep the model that actually answered, which can differ from the one
  requested when the API falls back.

**Rejected:**
- calling the API in CI (needs secrets, costs money, flaky, and not reproducible);
- mocking the model with ground truth in the evaluation (measures nothing);
- a hand-maintained prompt version as the only key (the first version): editing the prompt without
  bumping it would have replayed stale answers.

**Why:** the reported numbers come from real model answers and are reproducible by anyone with the
repository.

## 8. An oracle run separates rule errors from reading errors

**Chosen:** the evaluation has an `--oracle` mode that runs the checks and rules on the ground-truth
invoices. It reports BLOCKED, NEEDS ATTENTION and missed separately, and false alarms on controls. It
does not report field accuracy or prompt-injection survival, which cannot fail when the extraction is
the ground truth.

**Why:** when an end-to-end number is bad, you need to know whether the model misread or the rule is
wrong. The oracle found two problems on dev before any model call (a generator whose vendors' totals
varied tenfold, and an anomaly rule that flagged small invoices). Its detection rates are largely by
construction, because the generator plants what the rules look for; its useful number is the false
alarm rate. A separate hand-written set of bank-change phrasings measures the wording check on
sentences the generator never produces.

## 9. The browser never renders an uploaded PDF

**Chosen:**
- The review UI shows a PNG of the first page, rendered once at ingest with pypdfium2 and capped at
  2,000 px on its longest side, then stored with the document.
- The original is download-only, with `Content-Security-Policy: sandbox`.

**Rejected:**
- an `<iframe>` of the PDF (Chrome's viewer also refuses sandboxed frames, so the safe variant does
  not even work);
- rendering on every view at a fixed scale (the first version): a PDF declaring a 5-metre page asked
  for a multi-gigabyte bitmap.

**Why:** PDFs are an active-content format and invoices are attacker-supplied; a raster image is
inert, and a size-capped one is cheap.

## 10. Uploads are identified by content, bounded, and kept as received

**Chosen:**
- The file type comes from magic bytes; the filename and declared content type are ignored.
- Files are capped at 10 MB and 10 pages; images at 40 megapixels, checked from the header before
  any pixel is decoded; encrypted PDFs are refused. Every parser exception becomes a rejection.
- Images are EXIF-rotated, downscaled to 2,000 px, stripped of metadata and re-encoded for the model.
  The original bytes are stored too and are what "download" returns.
- Re-uploading the same file is idempotent. If it brings a new covering message, the draft is
  re-assessed with it and the message is audited.

**Why:** the inbox is the attack surface. Re-encoding keeps token cost bounded and the recording key
stable; keeping the original keeps the evidence.

## 11. Synthetic data with planted problems, dev and holdout

**Chosen:**
- A generator for Israeli invoices: Hebrew, English and mixed; valid tax IDs; the VAT rate by date;
  PDFs and degraded scans, rendered deterministically (seeded noise), so the committed manifest hashes
  stay valid.
- 15 categories with a planted problem and 2 controls (clean and rotated scans), with ground truth.
- Dev (100) for all prompt and rule work; holdout (200) to be recorded and scored once.

**Why:** there is no public dataset of Israeli supplier invoices with fraud cases. The honest
limitation is that real invoices are messier than anything generated.

## 12. Secrets are generated at first start, and the API refuses unsafe ones

**Chosen:** an init container writes random values (session secret, audit key, three database
passwords) into a Docker volume and never overwrites them. Services read them as files. The API
refuses to start when the session secret is missing, shorter than 32 bytes or a published
placeholder.

**Rejected:** a default secret in the code (the first version). With the documented
`docker compose up`, anyone could sign a cookie for any user.

**Why:** the one-command run must still be safe by default; asking people to invent secrets before
the demo starts would mean most never do.

## 13. Short transactions, one connection per request

**Chosen:** the document is stored in one transaction, the model is called with none open, and the
invoice and draft are written together afterwards, under a row lock on the document. A failure in
between leaves a document that the next upload of the same file finishes. Each API request holds at
most one database connection at a time.

**Rejected:** a request-scoped session opened for authentication and kept while the service opened a
second one (the first version): under 20 concurrent approvals the pool ran dry and requests failed
after 30 seconds.

**Why:** a payables queue must never strand a document, and concurrency bugs in approval are the
expensive kind. A test fires twelve approvals of one draft at once and expects exactly one payment
and eleven clean conflicts; the smoke test does the same with twenty through nginx.

## 14. A rejected invoice does not block the next one with the same number

**Chosen:** duplicate checks compare invoice numbers as letters and digits only (case-folded,
leading zeros dropped). A number matching an accepted invoice blocks; a number matching only a
rejected one is a warning.

**Why:** otherwise an attacker's rejected fake would block the genuine invoice that follows it, and
"INV 2031" would slip past "INV-2031".
