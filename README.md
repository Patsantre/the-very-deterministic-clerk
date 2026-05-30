# the-very-deterministic-clerk

A hybrid **deterministic-solver + LLM-fallback** agent for [BitGN's ECOM1 benchmark](https://bitgn.com/challenge/ecom), running on DeepSeek V4 Flash via OpenRouter.

The premise is in the name: most "e-commerce ops assistant" tasks in this benchmark are not creative work. They are policy checks, catalogue lookups, SQL questions, and side-effect mutations (checkout, discount, refund, 3DS recovery) with strict preconditions. A clerk does them by following a checklist, not by reasoning from scratch. So this agent works that way too — a deterministic, code-driven clerk in front, with an LLM only as a fallback when no checklist matches.

**Latest dev sweep on `bitgn/ecom1-dev`: 99.62%** (53 tasks, two non-perfect at `t40 = 0.95` and `t48 = 0.85`). Live ranking benchmark scheduled to open 2026-05-30.

---

## Why hybrid?

LLM agents on benchmarks like ECOM tend to fail in a small number of recurring ways:

- they paraphrase yes/no answers instead of emitting the exact `<YES>` / `<NO>` token the scorer expects;
- they cite "the catalogue" or `/proc/catalog` instead of the exact `/proc/catalog/.../path.json` row required for grounding refs;
- they invoke side-effect tools (`/bin/checkout`, `/bin/discount`, `/bin/payments recover-3ds`) without running the policy preflight — wrong runtime identity, wrong role, basket not in the right state, payment retry lockout, etc.;
- they over-refuse, returning `OUTCOME_DENIED_SECURITY` on a normal task whose wording happens to look like an injection attempt;
- they hallucinate basket IDs, payment IDs, or store IDs that weren't present in the task text.

All of these are deterministic problems with deterministic answers. So this agent solves them with code:

1. The task text is **classified** into a closed set of classes (`availability_count`, `catalogue_lookup`, `checkout`, `discount`, `refund`, `three_ds_recovery`, `fraud_export`, `quote_tsv`, `receipt_price_check`, `count_report`, `city_quantity`, …).
2. A class-specific **solver** runs a preflight checklist against the ECOM runtime — identity, role, ownership, state eligibility, inventory availability, policy refs from `/docs` — and only then invokes the side-effect tool (or returns the correct refusal outcome).
3. If no solver claims the task with high confidence, control falls through to an **LLM loop** (DeepSeek V4 Flash by default) with a tool-use API, runtime adapters, repeated-read / bad-SQL / exact-format guards, and auto-finalization when a deterministic answer is already in hand.

This makes the agent's behaviour on common task shapes predictable and cheap, and reserves model calls for the long tail.

---

## Architecture

```
                 ┌─────────────────────────────────────────────────┐
   task text ──▶ │  ecom_task_classifier.classify_task()           │
                 └─────────────────────────────────────────────────┘
                                       │
                                       ▼
                 ┌─────────────────────────────────────────────────┐
                 │  Ordered deterministic solver pipeline          │
                 │                                                 │
                 │  1. pre-mutation security (prompt-injection,    │
                 │     identity override, employee-contact denial) │
                 │  2. 3DS payment recovery                        │
                 │  3. archived fraud export                       │
                 │  4. refund workflow                             │
                 │  5. service-recovery discount                   │
                 │  6. checkout mutation                           │
                 │  7. read-only solvers (catalogue yes/no,        │
                 │     support-note check, availability counts,    │
                 │     quote TSV, catalogue count report,          │
                 │     city quantity sums)                         │
                 │                                                 │
                 │  Each solver: detect class → load policy refs   │
                 │  → check identity/role → check ownership →      │
                 │  check state → execute → finalize with exact    │
                 │  refs, OR decline and pass through.             │
                 └─────────────────────────────────────────────────┘
                                       │
                                       │ (no solver claimed the task)
                                       ▼
                 ┌─────────────────────────────────────────────────┐
                 │  Bootstrap context collection                   │
                 │  (root tree, /docs tree, sqlite schema,         │
                 │   product property keys, runtime date,          │
                 │   runtime identity)                             │
                 └─────────────────────────────────────────────────┘
                                       │
                                       ▼
                 ┌─────────────────────────────────────────────────┐
                 │  LLM fallback loop                              │
                 │  • Pydantic NextStep schema (tool router)       │
                 │  • DeepSeek V4 Flash via OpenAI-compatible API  │
                 │  • Domain tools: catalogue_lookup, store_lookup,│
                 │    catalogue_count_report, inventory_count      │
                 │  • Runtime tools: tree/find/search/list/read/   │
                 │    write/delete/stat/exec/answer                │
                 │  • Guards: repeated reads/lookups, invalid SQL, │
                 │    bad inventory refs, exact-format checks,     │
                 │    catalogue-count doc refs                     │
                 │  • Auto-finalize on exact counts                │
                 └─────────────────────────────────────────────────┘
```

The data plane is the public ECOM runtime exposed by BitGN (`bitgn.vm.ecom`) — a file-shaped workspace plus `/bin/sql`, `/bin/checkout`, `/bin/discount`, `/bin/payments`, `/bin/id`, `/bin/date`. Tool results are formatted back to the model in shell-shaped output (`ls`, `cat`, `rg -n --no-heading`, heredoc'd `<<'SQL'`) instead of protobuf JSON — easier to read for the model, easier to debug in logs.

### Solver boundary

Read-only solvers are deterministic and **must not** mutate runtime state. Side-effect solvers share an identical preflight shape:

1. Detect task class.
2. Load applicable policy refs from `/docs`.
3. Check runtime identity and role (`/bin/id`).
4. Check ownership.
5. Check state eligibility.
6. Execute the side effect.
7. Finalize with exact refs.

If any preflight check fails, the solver returns the correct refusal outcome (`OUTCOME_DENIED_SECURITY`, `OUTCOME_NONE_CLARIFICATION`, `OUTCOME_NONE_UNSUPPORTED`) with the policy doc as a grounding ref — instead of bailing out to the LLM, which would just guess.

### Why DeepSeek V4 Flash

DeepSeek V4 Flash through OpenRouter is currently the cheapest model that holds the strict Pydantic `Union` tool router on every step without falling back to the simplest union member. On commodity backends, large strict `json_schema` unions tend to collapse — V4 Flash holds, at meaningful cost savings vs. GPT-5.3-codex or Claude.

---

## Module map

```
agent.py                  Orchestration, runtime dispatch, tool schemas,
                          solver kit wiring, ordered solver pipeline.

ecom_task_classifier.py   Closed-set task classifier + ID extractors.

ecom_solvers/
  security.py             Prompt/identity override denial, employee
                          contact denial, archived fraud export
                          classification.
  checkout.py             Checkout mutation with target/identity/
                          ownership/basket/inventory preflight.
  discounts.py            Service-recovery discount with role/scope/
                          basket/subtotal preflight.
  refunds.py              Refund workflow with explicit unsupported /
                          security-denied outcomes (no refund approval
                          side effects from the agent).
  payments_3ds.py         3DS recovery with target/ownership/lockout/
                          state/attempt-limit preflight.
  read_only.py            Catalogue yes/no, support-note checks,
                          availability counts, quote TSV product-list
                          checks, receipt OCR price checks, catalogue
                          count reports, city quantity sums.

ecom_domain_tools.py      SQL-backed domain tools (catalogue_lookup,
                          store_lookup, catalogue_count_report,
                          inventory_count) and CSV/TSV parsers.
ecom_parsers.py           Task parsers, config-backed domain memory
                          for aliases, constraints, units, store
                          selection, and exact answer formats.

ecom_bootstrap.py         LLM-fallback startup context collection.
ecom_llm_loop.py          LLM fallback loop, guard dispatch, runtime
                          exec, exact-count auto-finalization.
ecom_guards.py            Repeated-read / bad-SQL / inventory-ref /
                          exact-format / catalogue-count guards.
ecom_policy_index.py      Small policy-doc index for SHA/path-aware
                          runtime document lookup.

config/
  property_aliases.json   Natural-language label → product_properties
                          snake_case key map.
  store_aliases.json      Store-name aliases for store_lookup.
  numeric_constraints.json Unit normalization rules
                          (e.g. "15 mm" → diameter_mm, value_number=15).

tests/                    Unit tests covering solvers, parsers, guards,
                          classifier, domain tools, and the main runner.
```

---

## Getting started

Requires Python 3.14 and [uv](https://github.com/astral-sh/uv). The dependencies pull BitGN's runtime SDK from `buf.build/gen/python`, so you also need a [Buf](https://buf.build/docs/bsr/authentication/) registry token configured (`buf registry login buf.build`).

```bash
# 1. clone
git clone https://github.com/Patsantre/the-very-deterministic-clerk.git
cd the-very-deterministic-clerk

# 2. install
make sync           # uv sync

# 3. credentials
export BITGN_API_KEY=...                                  # from bitgn.com
export OPENAI_API_KEY=...                                 # OpenRouter key works
export OPENAI_BASE_URL=https://openrouter.ai/api/v1
export MODEL_ID=deepseek/deepseek-chat-v4-flash
export BENCH_ID=bitgn/ecom1-dev                           # or bitgn/ecom1-prod when live

# 4. run
make run                                                  # full benchmark
make task TASKS="t01 t05 t12"                             # subset
uv run python main.py t01                                 # single task
```

Useful overrides:

| Variable | Default | Purpose |
|---|---|---|
| `BITGN_API_KEY` | _required_ | BitGN harness auth |
| `OPENAI_API_KEY` | _required_ | OpenAI-compatible key |
| `OPENAI_BASE_URL` | OpenAI default | Point at OpenRouter / other gateway |
| `MODEL_ID` | `gpt-4.1-2025-04-14` | Override model |
| `BENCH_ID` | `bitgn/ecom1-dev` | Benchmark id |
| `HARNESS_RPC_TIMEOUT_MS` | `60000` | Harness call timeout |
| `RUNTIME_RPC_TIMEOUT_MS` | `60000` | VM runtime call timeout |
| `RUNTIME_RPC_ATTEMPTS` | `3` | VM runtime retries on Connect errors |
| `ECOM_DISABLE_DETERMINISTIC_SOLVERS` | `0` | Force LLM fallback only (diagnostic) |

Recommended config for live runs (matches what produced the 99.62% dev sweep):

```bash
MODEL_TIMEOUT_S=75 HARNESS_RPC_TIMEOUT_MS=60000 \
RUNTIME_RPC_TIMEOUT_MS=15000 RUNTIME_RPC_ATTEMPTS=2 \
  uv run python main.py
```

---

## Tests

```bash
uv run python -m unittest discover -s tests
```

Unit tests cover the solvers, parsers, guards, classifier, domain tools, and the main runner. They run without `BITGN_API_KEY` or `OPENAI_API_KEY` — runtime calls are stubbed.

---

## Design notes

A few non-obvious choices worth calling out for portfolio reviewers:

- **Closed-set classifier with a structural fallback.** The task classifier prefers a tight LLM JSON call, but a few task shapes are recognized by structure first (`quote_tsv`, `receipt_price_check`) — for those, even if the classifier hedges, the solver runs. This bounds the worst case where the model misroutes a high-value structural task.
- **Classifier IDs are gated by a local extractor.** A `basket_id` or `payment_id` extracted by the LLM is trusted only if the local regex extractor also sees that ID in the task text. This kills a class of "the model invented `basket_42` because the prompt mentioned `42`" failures.
- **Side-effect solvers don't act without runtime identity.** If `/bin/id` is unavailable (e.g. the VM runtime is degraded), no mutation is attempted from cached assumptions or task text alone. The agent returns a refusal outcome with the policy doc as a ref, not a guess.
- **Shell-shaped tool output, not protobuf JSON.** ECOM responses are rendered as `cat`, `ls`, `rg -n --no-heading`, `tree -L 2`, and heredoc'd `<<'SQL'` blocks before being fed back into the LLM. This both compresses context and lets the model treat tool output as familiar shell output instead of nested JSON to be parsed.
- **`OUTCOME_*` discipline.** The final answer is constrained to a closed enum of ECOM outcomes (`OK`, `DENIED_SECURITY`, `NONE_CLARIFICATION`, `NONE_UNSUPPORTED`, `ERR_INTERNAL`). The system prompt narrows when each one applies. Most LLM agents on this benchmark fail in the wrong-outcome bucket; the deterministic solvers eliminate that bucket entirely for the classes they own.
- **Perturbation preflight (in the parent run harness, not in this repo).** Before a full sweep, a local wording-perturbation probe runs the classifier and parsers against mutated task wording. If task classification or parsing degrades on variants, the sweep aborts before burning tokens on a regressed router.

---

## Acknowledgments

- [BitGN](https://bitgn.com) for the ECOM1 benchmark and the sample agent harness this is built on top of.
- [DeepSeek](https://www.deepseek.com/) for V4 Flash, and [OpenRouter](https://openrouter.ai/) for routing.
- [Schema-Guided Reasoning](https://abdullin.com/schema-guided-reasoning/) by Rinat Abdullin — the Pydantic `Union` tool-router pattern in the LLM fallback comes from there.

## License

MIT. See [LICENSE](LICENSE).
