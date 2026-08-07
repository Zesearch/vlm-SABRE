"""Construct canonical questions from a validated design record."""

from __future__ import annotations

from vlmbench.data_model import Question

from .schema import BenchmarkDesign


def build_questions(
    *,
    design: BenchmarkDesign,
    sample_id: str,
    edit_id: str = "",
    source_asset_id: str = "",
    edited_asset_id: str = "",
    single_asset_id: str = "",
) -> list[Question]:
    """Map design-level question specs to concrete generated assets."""

    role_to_asset = {
        "base": source_asset_id,
        "edited": edited_asset_id,
        "single": single_asset_id,
    }
    role_to_canonical = {
        "base": "source",
        "edited": "edited",
        "single": "generated",
    }
    rows: list[Question] = []
    for spec in design.questions:
        asset_id = role_to_asset[spec.image_role]
        if not asset_id:
            raise ValueError(
                f"Question {spec.probe_id} requires an asset for role {spec.image_role}."
            )
        rows.append(
            Question(
                question_id=f"{sample_id}__{spec.probe_id}",
                sample_id=sample_id,
                edit_id=edit_id,
                image_asset_id=asset_id,
                image_role=role_to_canonical[spec.image_role],
                question_type=spec.question_type,
                prompt=spec.prompt,
                answer=spec.answer,
                eval_type=spec.eval_type,
                options=spec.options,
                metadata={
                    "probe": spec.probe_id,
                    "pressure_test_type": design.pressure_test_type,
                    "concept_id": design.concept_id,
                },
            )
        )
    return rows

