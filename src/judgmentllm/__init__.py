"""Provider-independent assessment contracts, normalization, and offline prompts."""

__version__ = "0.1.0a3"

from judgmentllm.config import (
    GenerationSpec,
    PromptSpecV2,
    RunSpec,
    RunSpecification,
    RunSpecV2,
    load_rubric,
    load_run_spec,
    validate_run_spec,
    validate_run_spec_json,
)
from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.models import (
    Exemplar,
    Level,
    PairTask,
    PairwiseCriterion,
    ResponseItem,
    Rubric,
    Trait,
)
from judgmentllm.planning import (
    CapabilityProfile,
    iter_requests,
    iter_tasks,
    plan_run,
    prepare_pairs,
    validate_run,
)
from judgmentllm.preview import preview_prompt
from judgmentllm.prompts import (
    RenderedPrompt,
    RenderedPromptV2,
    RenderLimits,
    render_prompt,
)
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import (
    NormalizationOptions,
    PairwiseResult,
    RubricResult,
    normalize_pairwise_result,
    normalize_rubric_result,
    validate_categorical_output,
    validate_pairwise_output,
)

__all__ = [
    "__version__",
    "ResponseItem",
    "Level",
    "Trait",
    "Rubric",
    "Exemplar",
    "PairTask",
    "PairwiseCriterion",
    "RunSpec",
    "RunSpecV2",
    "RunSpecification",
    "PromptSpecV2",
    "RenderedPrompt",
    "RenderedPromptV2",
    "validate_run_spec",
    "validate_run_spec_json",
    "GenerationSpec",
    "GenerationCapabilities",
    "GenerationParameterCapability",
    "RubricResult",
    "PairwiseResult",
    "NormalizationOptions",
    "CapabilityProfile",
    "RenderLimits",
    "load_rubric",
    "load_run_spec",
    "normalize_rubric_result",
    "normalize_pairwise_result",
    "validate_categorical_output",
    "validate_pairwise_output",
    "validate_run",
    "plan_run",
    "iter_tasks",
    "iter_requests",
    "prepare_pairs",
    "render_prompt",
    "preview_prompt",
    "export_pairwise_r_csv",
    "export_rubric_r_csv",
]

from judgmentllm.lifecycle import (
    aresume_run,
    arun_live,
    estimate_run,
    export_run_results,
    get_run,
    list_runs,
    resume_run,
    run_live,
    validate_inputs,
)
from judgmentllm.lifecycle_models import (
    ArtifactPaths,
    ExecutionPlan,
    InputValidationReport,
    LifecycleError,
    MockExecutionOptions,
    ProgressEvent,
    ResultManifest,
    RetrySelection,
    RunPage,
    RunStatus,
    RunSummary,
)

__all__ += [
    "validate_inputs",
    "estimate_run",
    "run_live",
    "arun_live",
    "resume_run",
    "aresume_run",
    "list_runs",
    "get_run",
    "export_run_results",
    "ArtifactPaths",
    "ExecutionPlan",
    "InputValidationReport",
    "LifecycleError",
    "MockExecutionOptions",
    "ProgressEvent",
    "ResultManifest",
    "RetrySelection",
    "RunPage",
    "RunStatus",
    "RunSummary",
]
