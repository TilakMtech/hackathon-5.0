# AI agents and prompt engineering (slice 2)

Rule of the system: **Claude handles language, code handles numbers and decisions.** Every LLM output passes deterministic guardrails before it can affect an order, a client or a leadership answer.

| Agent | Prompt | Model tier | Input → output | Guardrails in code |
| --- | --- | --- | --- | --- |
| Email intake | `p1_email_extraction` | fast (Haiku) | email in any of the 14 layouts → order JSON with an evidence quote per field | quotes must exist verbatim in the email; address values must appear in the email (blocks the address on file); ZIP vs state check; quantity sanity; injection detector; decision = auto-accept / ask the client / human review |
| Address request | `p2_address_request` | fast | missing fields → 60-word email to the client | no ZIP code that the client did not send; every missing field named |
| Delay alert | `p3_delay_alert` | smart (Sonnet) | facts known at despatch (historical replay) → client message | dates, actions, carriers and warehouses must be in the facts; no penalty or liability words; 80 words max; order ref in subject; status is 'ready for approval', never 'safe to send' |
| Alert judge | `p5_alert_judge` | smart | message + facts → 1–5 scores on factual, clarity, action, tone | runs in the eval suite; a factual score below 5 fails |
| Evidence Q&A | `p4_evidence_agent` | smart | leadership question → read-only SQL tool loop → answer + SQL + figures | SELECT only, a single statement, no file or network functions, row limit; every number in the answer is traced to a query result; questions the data cannot answer are declined |

## Prompt-engineering techniques used

- Role, context and why it matters at the top of each prompt (wrong address = late delivery; $680K decision).
- XML-tagged sections (`<context>`, `<rules>`, `<security>`, `<examples>`, `<facts>`), with the data kept apart from the instructions.
- Forced tool calls for structured output, validated by Pydantic, with one automatic repair retry.
- Few-shot examples that cover the hard cases (a forwarded old order, a missing ZIP with the address on file present, a prompt injection, EDI caps). The values are changed so no example appears in the test set.
- Evidence quotes per field, checked by code, so hallucinations are caught rather than trusted.
- An explicit "I don't know" path (null + `missing_fields`; `answerable: false`).
- A plan, query, check, answer method for the SQL agent, with self-correction from tool errors.
- Prompt-injection defence: email marked as untrusted data, a model flag, an independent regex detector, and 20 attack cases in the eval.
- Model routing (Haiku for volume, Sonnet for judgement), prompt caching of the long system prompts, and temperature 0 for extraction (with an automatic retry without temperature if a model rejects it).
- Versioned prompt files (`app/prompts/*.md`): every call logs `id@version#hash`, model, tokens and latency.
- LLM-as-judge with a strict rubric, run by a separate prompt that did not write the message.

## Evals and release gates

`python -m app.evals [--provider claude_foundry] [--enforce]` runs:

- 300 synthetic emails (14 layouts, about 29% incomplete addresses, 20 injections) against ground truth;
- 20 alerts for the accounts threatening to leave (rule checks, plus the judge on real models);
- 17 leadership questions (12 with SQL-verified answers, 1 checked by name, 4 that must be declined). Carrier questions also fail if the answer offers a cause nobody asked for.

It writes `reports/evals/latest.json` and `latest.md`, shown on the **Prompt quality** tab. With `--enforce`, any failed gate exits 1, so a prompt change that regresses is blocked in CI. The gates are listed in `app/evals.py` (`GATES`).

The **mock** provider is a rule-based parser, not a model. It lets the team work offline and shows why rules are not enough: on the same golden set it auto-accepts 23 wrong orders and fails the gates.

## Switch to Claude

1. Deploy `claude-haiku-5-5` and `claude-sonnet-5-5` in Foundry `csc-blp-foundry-cus` (Central US, resource group `AI-F-rg-csc-blp`, subscription "Hackathon 5.0 EA Subscription").
2. `cp backend/.env.example backend/.env`, set `OTD_LLM_PROVIDER=claude_foundry` and paste the key into `ANTHROPIC_FOUNDRY_API_KEY`.
3. Restart the API, then run `python -m app.evals --provider claude_foundry --enforce`.

Azure OpenAI fallback: the same steps with `OTD_LLM_PROVIDER=azure_openai` and the `OTD_AOAI_*` values.
On Azure: `azd env set OTD_LLM_PROVIDER claude_foundry`, `azd env set FOUNDRY_RESOURCE csc-blp-foundry-cus`, `azd env set FOUNDRY_API_KEY <key>`, then `azd up`. The key is stored as a Container Apps secret.

## What the checks can and cannot prove

The guardrails are defence in depth, not proof. Each one fails closed, and each was tested by an external audit and two independent attack rounds. Every reproduced bypass is pinned as a regression test in `backend/tests/test_agents.py`.

- **Intake** auto-accepts only when the email is simple enough to verify completely:
  - the order part contains exactly the extracted SKUs, each quantity is written beside its own SKU, and quotes back the service level and date;
  - every number in the order part is a quantity, address, date or label (PO, line, client);
  - the email contains no correction or ambiguity words, no second service level, no fractional or `1.000`-style number, and no injection flag.

  Anything else goes to a person.
- **Delay alerts** are checked against an allowlist: every content word must be in the facts or in a small neutral set, and every number, time and negation must be one the facts contain. The facts' action must appear word for word, so a condition such as "as soon as it is confirmed" cannot drop out; delivery promises anywhere else, status claims and invented remedies are blocked. No automatic check can prove free text asserts nothing new, so **every alert waits for a person to approve the proposed action before it is sent**.
- **Evidence answers** bind each number to its entity (carrier, client, region, tier), metric (late, on-time or all deliveries) and unit (% vs count), and match it to a query result row. This verifies traceability. Answer accuracy is measured separately, against gold SQL.
