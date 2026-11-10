from pydantic import Field, model_validator

from quality_platform.domain.models import Record


class TestStep(Record):
    action: str = Field(min_length=1)
    expected_result: str = Field(min_length=1)


class TestCase(Record):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    steps: tuple[TestStep, ...] = Field(min_length=1)


class TestCaseSet(Record):
    cases: tuple[TestCase, ...]

    @model_validator(mode="after")
    def unique_case_ids(self) -> "TestCaseSet":
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate test case ID")
        return self
