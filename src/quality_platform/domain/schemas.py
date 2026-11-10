from typing import Literal

from pydantic import Field, model_validator

from quality_platform.domain.models import Record, SourceReference


class TestStep(Record):
    action: str = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    expected_result_sources: tuple[SourceReference, ...] = ()


class TestCase(Record):
    artifact_kind: Literal["TEST_CASES"] = "TEST_CASES"
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    scenario: str = Field(min_length=1)
    case_type: str = "FUNCTIONAL"
    priority: str = "MEDIUM"
    preconditions: tuple[str, ...] = ()
    test_data: dict[str, str] = Field(default_factory=dict)
    steps: tuple[TestStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def canonical_identity_and_coverage(self) -> "TestCase":
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("duplicate test case requirement ID")
        return self


class TestCaseSet(Record):
    cases: tuple[TestCase, ...]

    @model_validator(mode="after")
    def unique_case_ids(self) -> "TestCaseSet":
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate test case ID")
        return self
