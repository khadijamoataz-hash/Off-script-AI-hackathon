# The committed demo run, step by step

This page walks through the run whose files are in `outputs/` and whose log is `decision_log.json`. Every file there was written by the code during that run on 17 November 2026 (times below are UTC, from the log); nothing was edited by hand. Project `PRJ-479A6665`, model `qwen3.5:9b` on a laptop, cost $0.00.

**In one line:** the brief was screened, researched with fetched sources, challenged three times by the critic, given a demand target before the test, and stopped at the Validate gate. **This run demonstrates how the gate works; it is not a demand experiment.** The landing page was never published and no sign-ups were collected, so the recorded 0 does not show that nobody wants this product (see section 6).

## 1. Brief and screening (12:37)

The founder typed: "digital ai academy to help cs students gets into job market". The intake agent confirmed it is a digital product (not physical goods, finance, health or legal advice) and the guardrails cleared the brief. Log: `intake / screen_brief`.

## 2. Discover, step 1: opportunities (12:37 to 12:57)

File: [`outputs/phase1_step1_opportunities.md`](../outputs/phase1_step1_opportunities.md)

- The researcher sharpened the idea and rated it against two alternatives on the same criteria.
- Claims are a table: each one is **Sourced** only if the page was fetched in the run and the quote is found in it, otherwise `ASSUMPTION`. Pages that refused automated readers are listed as skipped, not hidden.
- The critic gave the strongest case, the risks, kill criteria and the cheapest test that could disprove the idea, and recommended **REPEAT** with medium confidence: the problem is real, but there is no evidence anyone would pay.
- In the final round the researcher scored the sharpened idea **17 of 20** (problem severity 5, reachable market 4, founder fit 3, cheap first test 5), with 7 of 7 claims backed by a fetched source. The critic's view was "unproven", not "weak": it recommended **REPEAT** in all three rounds (confidence low, then medium, then medium), the last time because "we have zero evidence of customer willingness to pay".
- The founder chose repeat at step 1 (12:45), continued (12:50), then chose repeat again at the end of Discover (12:52) and continued once more (12:57). Each system recommendation is logged with `approved_by: null` and each founder choice with `approved_by: "founder"`.

## 3. Discover, step 2: interview script (12:57)

File: [`outputs/phase1_step2_interview_script.md`](../outputs/phase1_step2_interview_script.md)

A Mom Test script: questions about what people did in the past, and a list of questions not to ask (no pitching, no "would you buy").

The historical script also contains a hypothetical question that the validator at the time missed. Current schema checks reject that pattern and request a repaired script before exporting it. The committed demo output is preserved as generated, not corrected by hand; these checks have been verified with scripted tests, not a new local-model demo run.

## 4. Demand target fixed before the test (12:59)

Before Validate started, the founder fixed the target: **55 waitlist sign-ups from people in the target market**. It is written to the log (`set_evidence_target`, `fix_demand_target`) and cannot be lowered afterwards.

File: [`outputs/phase1_gate_decision.md`](../outputs/phase1_gate_decision.md)

## 5. Validate: Lean Canvas, landing page, invite post (13:00 to 13:07)

Files: [`outputs/phase2_step1_lean_canvas.md`](../outputs/phase2_step1_lean_canvas.md), [`outputs/phase2_step2_landing_page.html`](../outputs/phase2_step2_landing_page.html)

- Each canvas box is a claim: 1 of 9 is backed by a fetched source, 8 are labelled `ASSUMPTION`. The assumptions are what the demand test has to check.
- The landing page is rendered by code: it has an empty slot `[Real customer quote - collect after launch]` and a `[YOUR-FORM-URL]` placeholder, and no testimonials, counters, prices or countdowns.
- The page and an invite post went to the approval queue. The founder approved both on the approval screen. Approval exported the exact text as copy-ready files, each with the content hash and a single-use approval token: [`phase2_step2_approved_deploy_act-0c629491.txt`](../outputs/phase2_step2_approved_deploy_act-0c629491.txt), [`phase2_step2_approved_post_act-2940e2ac.txt`](../outputs/phase2_step2_approved_post_act-2940e2ac.txt). Foundry posted nothing.

## 6. Gate mechanics demonstration: recording 0 and the STOP recommendation (13:07 to 13:08)

File: [`outputs/phase2_gate_decision.md`](../outputs/phase2_gate_decision.md)

**What this section shows, and what it does not.** No demand experiment took place in this run:

- The landing page was **never published**: the approved file still contains the `[YOUR-FORM-URL]` placeholder, so nobody could sign up.
- The founder recorded **0** sign-ups at 13:07:18, **before** approving the landing page (13:07:42) and the invite post (13:07:53). The 0 therefore describes a test that had not been run, not the result of one.
- **0 sign-ups here does not establish zero demand.** It does not contradict the Discover research (17 of 20, problem judged real), and it is not evidence that the idea is weak.
- What the section does demonstrate is the gate mechanism: the target fixed in advance cannot be lowered, only founder-recorded demand counts (fetched sources do not), below half of the target the system recommends STOP and states the numbers, and Continue is not available after a failed gate.
- A real demand test would publish the approved page with a working form, run for the planned period, and record what real people did. Until then the honest status of this idea is the critic's: **REPEAT, unproven**.
- Given the recorded 0, which is below half of 55, the system recommended **STOP** and stated the numbers. Continue was not available: a project whose evidence gate has failed cannot be advanced.
- The founder chose stop. Log: `validate_gate`, outcome `stop`, `approved_by: "founder"`.

The records are kept exactly as the run wrote them, including the results line, which reads "0 0" because the founder typed 0 in the "of what" field (that field now requires a word).

## 7. Delivery test (MOCKED)

File: [`outputs/payment_test_log.md`](../outputs/payment_test_log.md)

A simulated order checks that delivery happens once per order id and that a refund revokes access. It is labelled `MOCKED` and states that no payment took place. Foundry has no payment code.

## What the run did not reach

Commercial setup and Launch open only after Validate passes, so this run has no Phase 4 or Phase 5 drafts. Both steps are built and covered by tests (`tests/test_workflow_discover.py`, `tests/test_app_gates.py`). Build and Operate are `STUBBED`.

## Checking it yourself

```bash
python scripts/verify_decision_log.py     # 46 entries, required keys and the hash chain
```

Each entry in `decision_log.json` carries `prev_hash` and `entry_hash`, so a changed or removed entry breaks the chain.
