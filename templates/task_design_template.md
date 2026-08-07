# Task Name

## Task Objective

Describe the VLM capability or failure mode you want to test.

## Target Failure Mode

Describe the specific behavior that should count as a model failure.

## Image Construction

State whether the task uses one generated image or a Base/Edited pair. Describe the visual evidence
that must be created or changed and the content that must remain unchanged.

## Question Format

Choose yes/no, multiple choice, or open generation. State the required response shape and how a
reference answer can be determined from the image.

## Evaluation Protocol

Choose a deterministic evaluator when possible: `yes_no_exact`, `choice_exact`, `count_exact`,
`exact_match`, or `contains`. If none is suitable, say why.

## Validity and Rejection Criteria

Describe what makes a sample valid, ambiguous, or unusable.

## Diversity Requirements

Describe the scene, object, composition, answer, or difficulty dimensions that should vary.

## Additional Constraints

Optional task-specific requirements.
