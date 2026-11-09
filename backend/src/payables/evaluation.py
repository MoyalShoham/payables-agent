"""Evaluation on synthetic invoices with ground truth.

    python -m payables.evaluation build             # render dev and holdout, write manifests
    python -m payables.evaluation record --split dev  # call Claude once per document (needs a key)
    python -m payables.evaluation run --split holdout # score from recordings: offline, no key
    python -m payables.evaluation run --split holdout --oracle  # rules on ground truth, no model
    python -m payables.evaluation phrasings          # hand-written bank-change wording set

Manifests (ground truth plus each rendered document's SHA-256) and recordings are committed, so
`run` needs neither the rendered files nor an API key. Prompt work happens on dev; holdout is
recorded and scored once.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from payables import tax_id
from payables.drafting import DraftStatus, build_draft
from payables.extraction import documents
from payables.extraction.claude import DEFAULT_EFFORT, DEFAULT_MODEL, ClaudeExtractor
from payables.extraction.model import ExtractionError, parse
from payables.extraction.prompt import PROMPT_VERSION
from payables.extraction.recording import RecordingExtractor, RecordingStore, ReplayExtractor
from payables.risk import Flag, assess, mentions_bank_change, normalize_invoice_number, normalize_name
from payables.schemas import ExtractedInvoice
from payables.synth.dataset import EVAL_TODAY, Render, Sample, build_split, build_world
from payables.validation import Status, run_checks

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval"
RECORDINGS = EVAL_DIR / "recordings"
WORK = ROOT / "eval-work"  # rendered documents, not committed

SPLITS = {"dev": (100, 1), "holdout": (200, 2)}
#: Opus 5.5 list prices in USD per million tokens, for the cost estimate in the report.
PRICE_IN, PRICE_OUT = Decimal("4.00"), Decimal("20.00")
KEY_FIELDS = ("vendor_tax_id", "invoice_number", "issue_date", "total", "bank")
#: Controls: nothing is planted, so any flag or failed check on them is a false alarm.
NO_PROBLEMS = {"clean", "scan_rotated"}
PHRASINGS = EVAL_DIR / "bank-change-phrasings.json"


@dataclass(frozen=True)
class Entry:
    """A manifest row: the sample plus the hash of the document as the pipeline would upload it."""

    sample: Sample
    document_sha256: str


# ---- manifests -------------------------------------------------------------------------------


def manifest_path(split: str) -> Path:
    return EVAL_DIR / f"manifest-{split}.json"


def build(split: str) -> list[Entry]:
    from payables.synth.render import render  # rendering libraries are dev-only

    size, seed = SPLITS[split]
    world = build_world()
    entries = []
    out = WORK / split
    out.mkdir(parents=True, exist_ok=True)
    for s in build_split(world, split, size, seed):
        doc = documents.load(render(s), s.id)
        (out / f"{s.id}{'.pdf' if doc.media_type == 'application/pdf' else '.png'}").write_bytes(doc.content)
        entries.append(Entry(s, doc.sha256))
    rows = [
        {
            "id": e.sample.id,
            "category": e.sample.category,
            "render": asdict(e.sample.render),
            "message_text": e.sample.message_text,
            "document_sha256": e.document_sha256,
            "expected_flags": sorted(e.sample.expected_flags),
            "expected_failures": sorted(e.sample.expected_failures),
            "truth": e.sample.truth.model_dump(mode="json"),
        }
        for e in entries
    ]
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path(split).write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
    )
    return entries


def load_manifest(split: str) -> list[Entry]:
    rows = json.loads(manifest_path(split).read_text(encoding="utf-8"))
    return [
        Entry(
            Sample(
                id=r["id"],
                split=split,
                category=r["category"],
                truth=ExtractedInvoice.model_validate(r["truth"]),
                render=Render(**r["render"]),
                message_text=r["message_text"],
                expected_flags=frozenset(Flag(f) for f in r["expected_flags"]),
                expected_failures=frozenset(r["expected_failures"]),
            ),
            r["document_sha256"],
        )
        for r in rows
    ]


def record(split: str) -> None:
    """Calls the model for every document in the split that has no recording yet."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set; recording needs it (running the evaluation does not).")
    recorder = RecordingExtractor(ClaudeExtractor(), RecordingStore(RECORDINGS))
    for i, e in enumerate(load_manifest(split), 1):
        suffix = ".pdf" if e.sample.render.format == "pdf" else ".png"
        content = (WORK / split / f"{e.sample.id}{suffix}").read_bytes()
        doc = documents.load(content, e.sample.id)
        if doc.sha256 != e.document_sha256:
            sys.exit(f"{e.sample.id}: rendered document differs from the manifest; run build again")
        raw = recorder.extract(doc, e.sample.message_text)
        print(f"[{i}] {e.sample.id} {e.sample.category}: {raw.stop_reason} {raw.usage}", flush=True)


# ---- scoring ---------------------------------------------------------------------------------


@dataclass
class Outcome:
    sample: Sample
    extracted: ExtractedInvoice | None
    flags: set[Flag]
    failures: set[str]
    status: DraftStatus | None
    usage: dict[str, int] = field(default_factory=dict)
    model: str | None = None


def evaluate(split: str, oracle: bool = False) -> list[Outcome]:
    world = build_world()
    replay = ReplayExtractor(RecordingStore(RECORDINGS))
    outcomes = []
    for e in load_manifest(split):
        s = e.sample
        usage: dict[str, int] = {}
        model: str | None = None
        if oracle:
            inv: ExtractedInvoice | None = s.truth
        else:
            raw = replay.by_hash(e.document_sha256, s.message_text)
            usage, model = raw.usage, raw.model
            try:
                inv = parse(raw)
            except ExtractionError:
                inv = None
        if inv is None:
            outcomes.append(Outcome(s, None, set(), set(), None, usage, model))
            continue
        checks = run_checks(inv, EVAL_TODAY)
        flags = assess(inv, world.context(s.message_text))
        vendor = next(
            (
                v
                for v in world.vendors
                if tax_id.normalize(v.tax_id) == tax_id.normalize(inv.vendor_tax_id or "")
            ),
            None,
        )
        draft = build_draft(inv, checks, flags, vendor)
        outcomes.append(
            Outcome(
                s,
                inv,
                {f.code for f in flags},
                {c.name for c in checks if c.status is Status.FAIL},
                draft.status,
                usage,
                model,
            )
        )
    return outcomes


def field_equal(name: str, truth: ExtractedInvoice, got: ExtractedInvoice) -> bool:
    a: Any = getattr(truth, name)
    b: Any = getattr(got, name)
    if name == "vendor_name":
        return normalize_name(a or "") == normalize_name(b or "")
    if name == "vendor_tax_id":
        return tax_id.normalize(a or "") == tax_id.normalize(b or "")
    if name == "invoice_number":
        return normalize_invoice_number(a) == normalize_invoice_number(b)
    if name == "bank":
        return (a.fingerprint() if a else None) == (b.fingerprint() if b else None)
    if name == "line_items":
        return [li.amount for li in a] == [li.amount for li in b]
    return bool(a == b)


FIELDS = (
    "vendor_name",
    "vendor_tax_id",
    "invoice_number",
    "document_type",
    "issue_date",
    "due_date",
    "line_items",
    "subtotal",
    "vat_amount",
    "total",
    "bank",
)


def report(split: str, outcomes: list[Outcome], oracle: bool) -> dict[str, Any]:
    """Scores. In oracle mode the "extraction" is the ground truth, so field accuracy and the
    prompt-injection totals cannot fail there and are not reported; the oracle measures the rules."""
    n = len(outcomes)
    failed = [o for o in outcomes if o.extracted is None]
    detection: dict[str, dict[str, int]] = {}
    for o in outcomes:
        for code in [*o.sample.expected_flags, *o.sample.expected_failures]:
            found = code in o.flags or code in o.failures
            d = detection.setdefault(str(code), {"expected": 0, "found": 0})
            d["expected"] += 1
            d["found"] += int(found)

    planted = [o for o in outcomes if o.sample.expected_flags or o.sample.expected_failures]
    outcome_counts = Counter("EXTRACTION_FAILED" if o.status is None else o.status.value for o in planted)
    clean = [o for o in outcomes if o.sample.category in NO_PROBLEMS]
    false_alarm = [o for o in clean if o.flags or o.failures or o.extracted is None]
    false_flags: Counter[str] = Counter()
    for o in outcomes:
        for code in o.flags | o.failures:
            if code not in o.sample.expected_flags and code not in o.sample.expected_failures:
                false_flags[str(code)] += 1
    result: dict[str, Any] = {
        "split": split,
        "mode": "oracle (rules on ground truth)" if oracle else f"{DEFAULT_MODEL}, effort {DEFAULT_EFFORT}",
        "prompt_version": PROMPT_VERSION,
        "samples": n,
        "planted_problem_samples": len(planted),
        "planted_outcomes": {
            k: outcome_counts.get(k, 0) for k in ("BLOCKED", "NEEDS_ATTENTION", "EXTRACTION_FAILED", "READY")
        },
        "detection": {k: {**v, "rate": v["found"] / v["expected"]} for k, v in sorted(detection.items())},
        "no_problem_samples": len(clean),
        "false_alarms": len(false_alarm),
        "false_alarm_rate": len(false_alarm) / len(clean) if clean else 0.0,
        "unexpected_codes": dict(sorted(false_flags.items())),
    }
    if oracle:
        return result
    fields: dict[str, float] = {}
    for name in FIELDS:
        ok = sum(
            1 for o in outcomes if o.extracted is not None and field_equal(name, o.sample.truth, o.extracted)
        )
        fields[name] = ok / n
    all_key = sum(
        1
        for o in outcomes
        if o.extracted is not None and all(field_equal(f, o.sample.truth, o.extracted) for f in KEY_FIELDS)
    )
    by_group: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for o in outcomes:
        key = f"{o.sample.render.format}/{o.sample.render.language}"
        by_group[key][1] += 1
        if o.extracted is not None and all(field_equal(f, o.sample.truth, o.extracted) for f in KEY_FIELDS):
            by_group[key][0] += 1
    injection = [o for o in outcomes if o.sample.category == "prompt_injection"]
    injection_ok = sum(
        1 for o in injection if o.extracted is not None and o.extracted.total == o.sample.truth.total
    )
    tokens_in = sum(o.usage.get("input_tokens", 0) for o in outcomes)
    tokens_out = sum(o.usage.get("output_tokens", 0) for o in outcomes)
    cost = (PRICE_IN * tokens_in + PRICE_OUT * tokens_out) / Decimal(1_000_000)
    result |= {
        # The model that actually answered: with server-side fallback it can differ from the request.
        "responding_models": dict(Counter(o.model or "unknown" for o in outcomes)),
        "extraction_failures": len(failed),
        "field_accuracy": fields,
        "all_key_fields_correct": all_key / n,
        "key_fields_by_render": {k: v[0] / v[1] for k, v in sorted(by_group.items())},
        "prompt_injection_total_kept": f"{injection_ok} of {len(injection)}",
        "tokens": {"input": tokens_in, "output": tokens_out, "estimated_usd": float(round(cost, 2))},
    }
    return result


def markdown(r: dict[str, Any]) -> str:
    p = r["planted_outcomes"]
    lines = [
        f"Split `{r['split']}`: {r['samples']} invoices, {r['mode']}, prompt {r['prompt_version']}.",
        "",
        f"- Invoices with a planted problem: {r['planted_problem_samples']}. BLOCKED {p['BLOCKED']}, "
        f"NEEDS_ATTENTION {p['NEEDS_ATTENTION']}, could not be read {p['EXTRACTION_FAILED']}, "
        f"**READY (missed) {p['READY']}**",
        f"- False alarms on the {r['no_problem_samples']} controls (nothing planted): "
        f"**{r['false_alarms']} ({r['false_alarm_rate']:.0%})**",
    ]
    if "field_accuracy" in r:
        lines += [
            f"- Responding models: {', '.join(f'{k} ({v})' for k, v in r['responding_models'].items())}",
            "- All key fields correct (tax ID, number, date, total, bank): "
            f"**{r['all_key_fields_correct']:.0%}**",
            f"- Extraction failures: {r['extraction_failures']}; prompt-injection invoices whose total "
            f"was kept: {r['prompt_injection_total_kept']}",
            "",
            "| Field | Accuracy |",
            "|---|---:|",
        ]
        lines += [f"| {k} | {v:.1%} |" for k, v in r["field_accuracy"].items()]
        lines += ["", "| Render | All key fields correct |", "|---|---:|"]
        lines += [f"| {k} | {v:.0%} |" for k, v in r["key_fields_by_render"].items()]
    lines += ["", "| Planted problem | Detected | Rate |", "|---|---:|---:|"]
    lines += [
        f"| {k} | {v['found']} of {v['expected']} | {v['rate']:.0%} |" for k, v in r["detection"].items()
    ]
    lines += [
        "",
        "Codes raised where nothing of that kind was planted: "
        + (", ".join(f"{k} ({v})" for k, v in r["unexpected_codes"].items()) or "none"),
    ]
    t = r.get("tokens", {})
    if t.get("input"):
        lines += [
            "",
            f"Tokens: {t['input']:,} in, {t['output']:,} out; about ${t['estimated_usd']} at list price.",
        ]
    return "\n".join(lines) + "\n"


def phrasings() -> dict[str, Any]:
    """The bank-change wording pattern on hand-written phrasings it shares no template with."""
    data = json.loads(PHRASINGS.read_text(encoding="utf-8"))
    out: dict[str, Any] = {"about": data["about"]}
    for kind in ("positive", "negative"):
        for lang in ("en", "he"):
            rows = [r["text"] for r in data[kind] if r["lang"] == lang]
            hits = [t for t in rows if mentions_bank_change(t)]
            out[f"{kind}_{lang}"] = {
                "flagged": len(hits),
                "of": len(rows),
                "missed" if kind == "positive" else "false_alarms": [
                    t for t in rows if (t in hits) != (kind == "positive")
                ],
            }
    return out


def phrasings_markdown(r: dict[str, Any]) -> str:
    lines = [
        "Bank-change wording pattern on hand-written phrasings (eval/bank-change-phrasings.json).",
        "",
        r["about"],
        "",
        "| Set | Flagged |",
        "|---|---:|",
    ]
    for key, label in (
        ("positive_en", "English bank-change notices"),
        ("positive_he", "Hebrew bank-change notices"),
        ("negative_en", "English look-alikes (should not flag)"),
        ("negative_he", "Hebrew look-alikes (should not flag)"),
    ):
        lines.append(f"| {label} | {r[key]['flagged']} of {r[key]['of']} |")
    lines += ["", "Missed:"]
    lines += [f"- {t}" for k in ("positive_en", "positive_he") for t in r[k]["missed"]]
    lines += ["", "Flagged although not a bank change:"]
    lines += [f"- {t}" for k in ("negative_en", "negative_he") for t in r[k]["false_alarms"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m payables.evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    ph = sub.add_parser("phrasings")
    ph.add_argument("--out", type=Path, default=None)
    rec = sub.add_parser("record")
    rec.add_argument("--split", choices=list(SPLITS), required=True)
    run = sub.add_parser("run")
    run.add_argument("--split", choices=list(SPLITS), required=True)
    run.add_argument("--oracle", action="store_true")
    run.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "build":
        for split in SPLITS:
            entries = build(split)
            print(f"{split}: {len(entries)} documents rendered to {WORK / split}")
    elif args.command == "record":
        record(args.split)
    elif args.command == "phrasings":
        r = phrasings()
        text = phrasings_markdown(r)
        print(text)
        if args.out:
            (args.out / "results-bank-phrasings.json").write_text(
                json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n"
            )
            (args.out / "results-bank-phrasings.md").write_text(text, encoding="utf-8", newline="\n")
    else:
        result = report(args.split, evaluate(args.split, args.oracle), args.oracle)
        text = markdown(result)
        print(text)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            name = f"results-{args.split}{'-oracle' if args.oracle else ''}"
            (args.out / f"{name}.json").write_text(
                json.dumps(result, indent=1, default=str), encoding="utf-8", newline="\n"
            )
            (args.out / f"{name}.md").write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
