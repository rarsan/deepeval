from contextvars import ContextVar
from typing import Optional, List, Union
from pydantic import BaseModel, Field
import asyncio

from deepeval.utils import get_or_create_event_loop
from deepeval.metrics.utils import (
    validate_conversational_test_case,
    trimAndLoadJson,
    check_llm_test_case_params,
    initialize_model,
)
from deepeval.test_case import (
    LLMTestCase,
    LLMTestCaseParams,
    ConversationalTestCase,
)
from deepeval.metrics import BaseMetric
from deepeval.models import DeepEvalBaseLLM
from deepeval.metrics.contextual_relevancy.template import (
    ContextualRelevancyTemplate,
)
from deepeval.metrics.indicator import metric_progress_indicator

required_params: List[LLMTestCaseParams] = [
    LLMTestCaseParams.INPUT,
    LLMTestCaseParams.ACTUAL_OUTPUT,
    LLMTestCaseParams.RETRIEVAL_CONTEXT,
]


class ContextualRelevancyVerdict(BaseModel):
    verdict: str
    reason: str = Field(default=None)


class ContextualRelevancyMetric(BaseMetric):

    _verdicts: ContextVar[List[ContextualRelevancyVerdict]] = ContextVar('verdicts', default=[])
    _score: ContextVar[float] = ContextVar('score', default=0)
    _reason: ContextVar[str] = ContextVar('reason', default="")
    _success: ContextVar[bool] = ContextVar('success', default=False)

    def __init__(
        self,
        threshold: float = 0.5,
        model: Optional[Union[str, DeepEvalBaseLLM]] = None,
        include_reason: bool = True,
        async_mode: bool = True,
        strict_mode: bool = False,
    ):
        self.threshold = 1 if strict_mode else threshold
        self.model, self.using_native_model = initialize_model(model)
        self.evaluation_model = self.model.get_model_name()
        self.include_reason = include_reason
        self.async_mode = async_mode
        self.strict_mode = strict_mode

    @property
    def verdicts(self) -> List[ContextualRelevancyVerdict]:
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
                verdicts: List[ContextualRelevancyVerdict] = (
                    self._generate_verdicts(
                        test_case.input, test_case.retrieval_context
                    )
                )
                self._verdicts.set(verdicts)

                score = self._calculate_score()
                self._score.set(score)

                reason = self._generate_reason(test_case.input)
                self._reason.set(reason)

                success = self.score >= self.threshold
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
            self,
            async_mode=True,
            _show_indicator=_show_indicator,
        ):
            verdicts: List[ContextualRelevancyVerdict] = (
                await self._a_generate_verdicts(
                    test_case.input, test_case.retrieval_context
                )
            )
            self._verdicts.set(verdicts)

            score = self._calculate_score()
            self._score.set(score)

            reason = await self._a_generate_reason(test_case.input)
            self._reason.set(reason)

            success = self.score >= self.threshold
            self._success.set(success)

            return self.score

    async def _a_generate_reason(self, input: str):
        if self.include_reason is False:
            return None

        irrelevancies = []
        for verdict in self.verdicts:
            if verdict.verdict.lower() == "no":
                irrelevancies.append(verdict.reason)

        prompt: dict = ContextualRelevancyTemplate.generate_reason(
            input=input,
            irrelevancies=irrelevancies,
            score=format(self.score, ".2f"),
        )
        if self.using_native_model:
            res, cost = await self.model.a_generate(prompt)
            self.evaluation_cost += cost
        else:
            res = await self.model.a_generate(prompt)
        return res

    def _generate_reason(self, input: str):
        if self.include_reason is False:
            return None

        irrelevancies = []
        for verdict in self.verdicts:
            if verdict.verdict.lower() == "no":
                irrelevancies.append(verdict.reason)

        prompt: dict = ContextualRelevancyTemplate.generate_reason(
            input=input,
            irrelevancies=irrelevancies,
            score=format(self.score, ".2f"),
        )
        if self.using_native_model:
            res, cost = self.model.generate(prompt)
            self.evaluation_cost += cost
        else:
            res = self.model.generate(prompt)
        return res

    def _calculate_score(self):
        total_verdicts = len(self.verdicts)
        if total_verdicts == 0:
            return 0

        relevant_nodes = 0
        for verdict in self.verdicts:
            if verdict.verdict.lower() == "yes":
                relevant_nodes += 1

        score = relevant_nodes / total_verdicts
        return 0 if self.strict_mode and score < self.threshold else score

    async def _a_generate_verdicts(
        self, text: str, retrieval_context: List[str]
    ) -> ContextualRelevancyVerdict:
        tasks = []
        for context in retrieval_context:
            prompt = ContextualRelevancyTemplate.generate_verdict(
                text=text, context=context
            )
            task = asyncio.create_task(self.model.a_generate(prompt))
            tasks.append(task)

        results = await asyncio.gather(*tasks)

        verdicts = []
        if self.using_native_model:
            for res, _ in results:
                data = trimAndLoadJson(res, self)
                verdict = ContextualRelevancyVerdict(**data)
                verdicts.append(verdict)
        else:
            for res in results:
                data = trimAndLoadJson(res, self)
                verdict = ContextualRelevancyVerdict(**data)
                verdicts.append(verdict)

        return verdicts

    def _generate_verdicts(
        self, text: str, retrieval_context: List[str]
    ) -> List[ContextualRelevancyVerdict]:
        verdicts: List[ContextualRelevancyVerdict] = []
        for context in retrieval_context:
            prompt = ContextualRelevancyTemplate.generate_verdict(
                text=text, context=context
            )
            if self.using_native_model:
                res, cost = self.model.generate(prompt)
                self.evaluation_cost += cost
            else:
                res = self.model.generate(prompt)
            data = trimAndLoadJson(res, self)
            verdict = ContextualRelevancyVerdict(**data)
            verdicts.append(verdict)

        return verdicts

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
        return "Contextual Relevancy"
