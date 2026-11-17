# Partner US storefront discovery and source review

This operator workflow records US storefront evidence and plans source-only review. A successful source review allows read-only catalog preflight. Merchant identity, buyer destination eligibility, checkout quotes, and publication use their existing acceptance paths.

## Discover and resume

Keep the approved partner tracker and run artifacts in the operator workspace. Start a batch using the tracked official hosts and product names in `data/catalog_enrichment/us_discovery_storefronts.json`.

```sh
python -m scripts.ops.partner_discovery.manifest \
  --tracker "$evidence/<TRACKER_JSON>" --batch-id <BATCH_ID_A> > "$manifest"
bash scripts/ops/partner_discovery/scan_cloud.sh launch "$manifest" "$run"
bash scripts/ops/partner_discovery/scan_cloud.sh collect "$run"
```

The launcher uses the evidence-only service account in `pivota-staging`. It verifies the packaged scanner, manifest, image, and Cloud execution, then records one receipt per tracker line. The run directory is immutable with respect to the submitted manifest. A local run uses the same manifest on stdin with `python -m scripts.ops.partner_discovery.scan`.

Start a fresh run for a continuation. The continuation carries deferred candidates, variant pages, and retryable failures. Read `coverage.status` even when `queries` becomes empty. An unverified official host and a provider cap remain visible for follow-up outside the current scan.

```sh
python -m scripts.ops.partner_discovery.manifest \
  --resume "$run/manifest.json" --query-receipts "$run/queries.jsonl" \
  --batch-id <BATCH_ID_B> > "$next_manifest"
```

## Review exact source samples

Keep completed review directories under the same history root across runs. For a separate work directory, pass the common parent with `--history-root`.

```sh
python -m scripts.ops.partner_discovery.review_input "$run/queries.jsonl" \
  --run-dir "$review_run" --history-root "$review_runs"
jq -r '.runs[]' "$review_run/review-plan.json"
```

For each listed path, run `python -m scripts.ops.partner_discovery.review_cloud launch PATH` and then `collect PATH` once the execution finishes. The planner splits large inputs within the worker and Cloud Run argument limits. A plan with zero pending samples needs no execution.

Each sample binds one partner relationship, one merchant listing, and up to four exact native variant quotes. Price, stock, title, GTIN, SKU, merchant, market evidence, or relationship changes produce a fresh sample. Completed results are reused only after the planner revalidates their Cloud execution, input digest, submitted sample, and completed model rounds. An authenticated hold is counted in `held_samples`. The same evidence remains held, while incomplete samples can go into a new plan. A pre-policy archive or an expired log contributes no reuse evidence. Two independently successful model rounds are required, with arbitration only when they disagree.

To retry byte-identical input after a confirmed failed execution or a run whose model reviews are all incomplete, put that input in a new run directory and use `review_cloud launch NEW_RUN --retry-from PRIOR_RUN`. The retry reuses one execution per confirmed predecessor. Pending or ambiguous execution responses remain pending until Cloud Run supplies an authoritative result.

## Reconcile discovery and the handoff

Build a fresh retailer source manifest and optional intake ledger with `python -m scripts.ops.partner_discovery.sources --evidence-dir DIRECTORY --output MANIFEST --intake-ledger LEDGER`. The directory contains the existing tracker, original research, expansion, retailer, retailer-channel and baseline files consumed by the builder (named under `sources.evidence_files` in `data/catalog_enrichment/us_discovery_storefronts.json`). Keep original, alternative, dupe, historical and channel associations as evidence.

Use `python -m scripts.ops.partner_discovery.coverage --help` to reconcile the new intake, official query receipts, and older capture manifest with its bound receipts. Use `python -m scripts.ops.partner_discovery.handoff --coverage --audit AUDIT --cohort COHORT` to account for all audited quote keys against an already prepared cohort. An optional `--conflicts` file records separately reviewed reasons as a JSON list of `{"quote_key":["https://seller.example/products/item","1234"],"review_required":"distinct_native_gtin"}`. The next-page suggestion is a read-only review queue. The original cohort file remains the input for any separately reviewed apply.
