from contextvars import ContextVar
from typing import Optional, Union, List
from pydantic import BaseModel, Field

from deepeval.test_case import (
    LLMTestCase,
    LLMTestCaseParams,
    ConversationalTestCase,
)
from deepeval.metrics import BaseMetric
from deepeval.utils import get_or_create_event_loop
from deepeval.metrics.utils import (
    validate_conversational_test_case,
    trimAndLoadJson,
    check_llm_test_case_params,
    initialize_model,
)
from deepeval.metrics.hallucination.template import HallucinationTemplate
from deepeval.models import DeepEvalBaseLLM
from deepeval.metrics.indicator import metric_progress_indicator

required_params: List[LLMTestCaseParams] = [
    LLMTestCaseParams.INPUT,
    LLMTestCaseParams.ACTUAL_OUTPUT,
    LLMTestCaseParams.CONTEXT,
]

class HallucinationVerdict(BaseModel):
    verdict: str
    reason: str = Field(default=None)


class HallucinationMetric(BaseMetric):

    _verdicts: ContextVar[List[HallucinationTemplate]] = ContextVar('verdicts', default=[])
    _score: ContextVar[float] = ContextVar('score', default=0)
    _reason: ContextVar[str] = ContextVar('reason', default="")
    _success: ContextVar[bool] = ContextVar('success', default=False)

    def __init__(
        self,
        threshold: float = 0.5,
        model: Optional[Union[str, DeepEvalBaseLLM]] = None,
        include_reason: bool = True,
        async_mode: bool = False,
        strict_mode: bool = False,
    ):
        self.threshold = 0 if strict_mode else threshold
        self.model, self.using_native_model = initialize_model(model)
        self.evaluation_model = self.model.get_model_name()
        self.include_reason = include_reason
        self.async_mode = async_mode
        self.strict_mode = strict_mode

    @property
    def verdicts(self) -> List[HallucinationVerdict]:
        return self._verdicts.get()
    @property
    def score(self) -> float:
        return self._score.get()
    @property
    def reason(self) -> str:
        return self._reason.get()
    @property
    def success(self) -> str:
        return self._success.get()

    def measure(
        self, test_case: Union[LLMTestCase, ConversationalTestCase]
    ) -> float:
        if isinstance(test_case, ConversationalTestCase):
            test_case = validate_conversational_test_case(test_case, self)
        check_llm_test_case_params(test_case, required_params, self)

        self.evaluation_cost = 0 if self.using_native_model else None
        with metric_progress_indicator(self):
            if self.async_mode:
                loop = get_or_create_event_loop()
                (
                    verdicts,
                    score,
                    reason,
                    success
                ) = loop.run_until_complete(
                    self._measure_async(test_case)
                )
                self._verdicts.set(verdicts)
                self._score.set(score)
                self._reason.set(reason)
                self._success.set(success)
            else:
                verdicts: List[HallucinationVerdict] = (
                    self._generate_verdicts(
                        test_case.actual_output, test_case.context
                    )
                )
                self._verdicts.set(verdicts)

                score = self._calculate_score()
                self._score.set(score)

                reason = self._generate_reason()
                self._reason.set(reason)

                success = self.score <= self.threshold
                self._success.set(success)

                return self.score
    
    async def _measure_async(
            self,
            test_case: Union[LLMTestCase, ConversationalTestCase]):
        await self.a_measure(test_case, _show_indicator=False)
        return (
            self.verdicts,
            self.score,
            self.reason,
            self.success
            )

    async def a_measure(
        self,
        test_case: Union[LLMTestCase, ConversationalTestCase],
        _show_indicator: bool = True,
    ) -> float:
        if isinstance(test_case, ConversationalTestCase):
            test_case = validate_conversational_test_case(test_case, self)
        check_llm_test_case_params(test_case, required_params, self)

        self.evaluation_cost = 0 if self.using_native_model else None
        with metric_progress_indicator(
            self, async_mode=True, _show_indicator=_show_indicator
        ):
            verdicts: List[HallucinationVerdict] = (
                await self._a_generate_verdicts(
                    test_case.actual_output, test_case.context
                )
            )
            self._verdicts.set(verdicts)

            score = self._calculate_score()
            self._score.set(score)

            reason = await self._a_generate_reason()
            self._reason.set(reason)

            success = self.score <= self.threshold
            self._success.set(success)

            return self.score

    async def _a_generate_reason(self):
        if self.include_reason is False:
            return None

        factual_alignments = []
        contradictions = []
        for verdict in self.verdicts:
            if verdict.verdict.strip().lower() == "no":
                factual_alignments.append(verdict.reason)
            else:
                contradictions.append(verdict.reason)

        prompt: dict = HallucinationTemplate.generate_reason(
            factual_alignments=factual_alignments,
            contradictions=contradictions,
            score=format(self.score, ".2f"),
        )

        if self.using_native_model:
            res, cost = await self.model.a_generate(prompt)
            self.evaluation_cost += cost
        else:
            res = await self.model.a_generate(prompt)
        return res

    def _generate_reason(self):
        if self.include_reason is False:
            return None

        factual_alignments = []
        contradictions = []
        for verdict in self.verdicts:
            if verdict.verdict.strip().lower() == "no":
                factual_alignments.append(verdict.reason)
            else:
                contradictions.append(verdict.reason)

        prompt: dict = HallucinationTemplate.generate_reason(
            factual_alignments=factual_alignments,
            contradictions=contradictions,
            score=format(self.score, ".2f"),
        )

        if self.using_native_model:
            res, cost = self.model.generate(prompt)
            self.evaluation_cost += cost
        else:
            res = self.model.generate(prompt)
        return res

    async def _a_generate_verdicts(
        self, actual_output: str, contexts: List[str]
    ) -> List[HallucinationVerdict]:
        verdicts: List[HallucinationVerdict] = []
        prompt = HallucinationTemplate.generate_verdicts(
            actual_output=actual_output, contexts=contexts
        )
        if self.using_native_model:
            res, cost = await self.model.a_generate(prompt)
            self.evaluation_cost += cost
        else:
            res = await self.model.a_generate(prompt)
        data = trimAndLoadJson(res, self)
        verdicts = [HallucinationVerdict(**item) for item in data["verdicts"]]
        return verdicts

    def _generate_verdicts(
        self, actual_output: str, contexts: List[str]
    ) -> List[HallucinationVerdict]:
        verdicts: List[HallucinationVerdict] = []
        prompt = HallucinationTemplate.generate_verdicts(
            actual_output=actual_output, contexts=contexts
        )
        if self.using_native_model:
            res, cost = self.model.generate(prompt)
            self.evaluation_cost += cost
        else:
            res = self.model.generate(prompt)
        data = trimAndLoadJson(res, self)
        verdicts = [HallucinationVerdict(**item) for item in data["verdicts"]]
        return verdicts

    def _calculate_score(self) -> float:
        number_of_verdicts = len(self.verdicts)
        if number_of_verdicts == 0:
            return 0

        hallucination_count = 0
        for verdict in self.verdicts:
            if verdict.verdict.strip().lower() == "no":
                hallucination_count += 1

        score = hallucination_count / number_of_verdicts
        return 1 if self.strict_mode and score > self.threshold else score

    def is_successful(self) -> bool:
        if self.error is not None:
            self.success = False
        else:
            try:
                self._success.set(self.score >= self.threshold)
            except:
                self._success.set(False)
        return self.success

    @property
    def __name__(self):
        return "Hallucination"
