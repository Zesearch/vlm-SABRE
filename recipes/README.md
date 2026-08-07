# Recipes

Recipes are task-specific adapters for the general benchmark pipeline.

The default user-facing input is a natural-language Markdown task description.
The shared design stage combines that document with
`vlmbench.recipes.schema.BenchmarkDesign` and asks a structured-output model to
produce validated design JSONL. Users normally edit only the Markdown file.
Advanced users may supply a `BenchmarkDesign` subclass when additional
task-specific fields are required.

A recipe should define:

- task objective
- structured design schema
- design prompt
- image-generation/edit prompt rules
- question builder
- screening probes and evaluator types
- export format

The pipeline core should not hard-code context-prior, surface, entity-modify,
or language-prior assumptions. Those choices belong here.
The failure-only retention rule is not recipe-specific: all recipes reject a
candidate when the screening model answers every required probe correctly.
