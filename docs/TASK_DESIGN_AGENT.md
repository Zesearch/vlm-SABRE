# Task Design Agent

The Task Design Agent is the user-facing stage before structured sample-design generation.
It is built on the open-source Codex agent runtime through the optional `openai-codex` Python SDK.

## Boundary

```text
user design.md
  -> Codex Task Design Agent
  + base design/schema references (few-shot only)
  -> canonical_design.md + task-local schema.py + compilation_report.json
  -> local OpenAI strict-schema preflight
  -> GPT structured-output design generator
  -> designs.jsonl
```

Codex interprets, clarifies, and compiles the user's task. It does not replace the existing batch
design generator. The second stage still uses the OpenAI Responses API to create each validated
task-specific `BenchmarkDesign` record.

The packaged base schema is a reference template, not a universal output schema. It is never
modified. For each ready task, the agent derives a concrete `schema.py` with fixed fields for that
task's generation mode, question format, answer contract, and evaluator. The derived class keeps the
stable downstream envelope so image construction and question extraction remain shared.

## Required task decisions

A ready canonical design contains:

- Task Objective
- Target Failure Mode
- Image Construction
- Question Format
- Evaluation Protocol
- Validity and Rejection Criteria
- Diversity Requirements

The agent returns `needs_clarification` instead of inventing a missing question format, answer
contract, image-construction mode, or evaluation rule.

## Filter-gate compatibility

The initial agent only marks a task ready when every planned question can use one of the shared
deterministic evaluators:

- `yes_no_exact`
- `choice_exact`
- `count_exact`
- `exact_match`
- `contains`

LLM-judge and manual evaluation are represented in the compilation schema, but they are intentionally
not eligible for the automated failure-only gate in this release.

## Commands

API behavior is explicit in the repository-level `settings.json`: OpenAI base URL, Codex agent
model, design-generation model, reasoning effort, retry count, and token limit. CLI flags override
the file. Every command prints its resolved non-secret configuration before making an API request.
Add `--dry-run` to print that configuration and exit without reading the API key or making a request.
The `build` command asks how many structured designs to create in the current batch when `--count`
is omitted. Non-interactive runs must pass `--count N` explicitly.

Compile only:

```bash
vlmbench-agent compile --task design.md --output-dir runs/my_task
```

The output directory contains the untouched user `design.md`, enriched `canonical_design.md`,
derived `schema.py`, and `compilation_report.json`. Generated schema code is restricted to
declarative Pydantic classes and is rejected locally if it uses dynamic object maps, changes the
downstream envelope, or fails OpenAI strict structured-output rules.

Compile and generate structured designs:

```bash
vlmbench-agent build \
  --task design.md \
  --output-dir runs/my_task \
  --design-model <design-model> \
  --count 100
```

Both commands require `OPENAI_API_KEY` (or `--api-key-file`). The Codex SDK is explicitly placed in
API-key mode and does not reuse a local Codex Desktop or ChatGPT login. The `build` command reuses
the same key for the existing GPT structured-output stage.

Use `--non-interactive` for automation. An incomplete task then writes a compilation report and exits
with status code 2. Use `--template context` to select the paper-aligned Context template
explicitly; otherwise the registry only loads a detailed template when its keywords match the task.
The pre-release ID `context_prior` remains accepted as a compatibility alias.

To generate designs from an already compiled task without calling the agent again:

```bash
vlmbench-design \
  --task runs/my_task/canonical_design.md \
  --schema runs/my_task/schema.py:TaskDesign \
  --output runs/my_task/designs.jsonl \
  --model <design-model> \
  --count 100
```
