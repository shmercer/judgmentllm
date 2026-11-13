"""Original study definitions, separate from qualification execution policy."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Literal

import yaml

from judgmentllm.config import RunSpecV3
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes

STUDY_VERSION = "original-writing-v3-native-1"
SOURCE_MANIFEST_SHA256 = (
    "fc289257f1b76fb1afaeb6b10c9f5be63d5d17728e972164082b5cb2b1c269a6"
)
MODELS = (
    ("openai", "gpt-6-luna"),
    ("openai", "gpt-6.1-terra"),
    ("gemini", "gemini-3.5-flash-lite"),
    ("gemini", "gemini-3.8-flash"),
    ("anthropic", "claude-haiku-5-5"),
    ("anthropic", "claude-sonnet-5-5"),
)
ROUTES = {
    "openai": (
        "responses",
        "openai.responses.text.format",
        "openai",
        "3.42.1",
        "openai-v1",
    ),
    "anthropic": (
        "messages",
        "anthropic.messages.output_config.format",
        "anthropic",
        "2.3.0",
        "2026-10-21",
    ),
    "gemini": (
        "models.generate_content",
        "gemini.generate_content.response_json_schema",
        "google-genai",
        "2.34.0",
        "v1beta",
    ),
}
SCENARIOS = {
    "grouped_scores": ("all_12_score_only", 4096, False, "grouped"),
    "grouped_explanations": ("all_12_score_explanation", 8192, True, "grouped"),
    "independent_scores": ("one_trait_score_only", 2048, False, "independent"),
    "independent_explanations": (
        "one_trait_score_explanation",
        4096,
        True,
        "independent",
    ),
    "cj": ("cj_13_independent_score_only", 2048, False, "independent"),
}
TRAIT9 = "rephrasing_of_the_thesis_statement"
HOLISTIC = "overall_writing_quality"
SuiteName = Literal[
    "native_pilot", "tier_feasibility", "reasoning_comparison", "temperature_acceptance"
]
ORIGINAL_SUITES = ("native_pilot", "tier_feasibility")
CONTRASTS = {
    "gpt-6-luna": "none",
    "gpt-6.1-terra": "none",
    "gemini-3.5-flash-lite": "high",
    "gemini-3.8-flash": "low",
    "claude-haiku-5-5": "low",
    "claude-sonnet-5-5": "low",
}
EXTENSION_CAPS = {"grouped_scores": 8192, "cj": 4096}
CAMPAIGN_LIMITS = (66, 10, 39600)
EXTENDED_CAMPAIGN_LIMITS = (98, 16, 68400)
SUITES: dict[str, tuple[int, int, int]] = {
    "native_pilot": (42, 5, 8 * 3600),
    "tier_feasibility": (24, 5, 3 * 3600),
    "reasoning_comparison": (24, 5, 5 * 3600),
    "temperature_acceptance": (8, 1, 3 * 3600),
}


def source_manifest(package: Path) -> dict[str, Any]:
    raw = (package / "original-manifests.json").read_bytes()
    if bytes_digest(raw) != SOURCE_MANIFEST_SHA256:
        raise ValueError("Study manifest differs from the verified original")
    return json.loads(raw)


def source_inventory(package: Path, kind: str) -> dict[str, str]:
    """Require the complete original package, including unused supporting files."""
    record = source_manifest(package)[kind]
    original = record["files"] | {"manifest_sha256.json": record["manifest_sha256"]}
    # Historical programs are byte fixtures; keep their original bytes out of formatters.
    expected = {
        (name + ".txt" if name.endswith(".py") else name): digest
        for name, digest in original.items()
    }
    directories = {
        p.as_posix() for name in expected for p in Path(name).parents if p != Path(".")
    }
    root = package / "sources" / kind
    if (package / "sources").is_symlink() or root.is_symlink():
        raise ValueError("Study source symlinks are not permitted")
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode):
            actual[name] = bytes_digest(path.read_bytes())
        elif not stat.S_ISDIR(mode) or name not in directories:
            raise ValueError("Unexpected study source entry")
    if actual != expected:
        raise ValueError(
            "Study source inventory or bytes differ from the verified original"
        )
    return original


def configuration(
    package: Path,
    provider: str,
    model: str,
    scenario: str,
    tier: str,
    account_scope: str = "study-account-unbound",
    capture: Literal["full", "minimal"] = "full",
) -> tuple[str, RunSpecV3]:
    """Path-independent identity; source text is loaded verbatim, never rewritten."""
    if (provider, model) not in MODELS or scenario not in SCENARIOS:
        raise ValueError("Unknown original study selection")
    tiers = (
        ("standard_only",)
        if provider == "anthropic"
        else ("default", "flex", "fast")
        if provider == "openai"
        else ("standard", "flex", "priority")
    )
    if tier not in tiers:
        raise ValueError("Unsupported study tier")
    template, cap, explanations, grouping = SCENARIOS[scenario]
    source = package.resolve() / "sources" / ("cj" if scenario == "cj" else "rubric")
    digests = source_inventory(package, "cj" if scenario == "cj" else "rubric")
    endpoint, mechanism, _, _, _ = ROUTES[provider]
    prompt = source / "prompts" / (template + ".txt")
    definition = source / (
        "criteria_13_traits.yaml" if scenario == "cj" else "rubric_12_traits.yaml"
    )
    data: dict[str, Any] = {
        "schema_version": 3,
        "task_kind": "pairwise" if scenario == "cj" else "rubric",
        "input": {"path": source / "responses_demo.csv"},
        "provider": provider,
        "endpoint": endpoint,
        "model": model,
        "account_scope": account_scope,
        "prompt": {
            "file": prompt,
            "structured_output": "native",
            "native_mechanism": mechanism,
            "output_order": "together",
        },
        "execution": {
            "mode": "live",
            "grouping": grouping,
            "explanations": explanations,
            "cache_policy": "off",
            "concurrency": 1,
            "tier": tier,
        },
        "generation": {"max_output_tokens": cap},
        "capture": capture,
        "strict_capabilities": True,
        "allow_ties": False,
    }
    document = yaml.safe_load(definition.read_text(encoding="utf-8"))
    if scenario == "cj":
        data.update(
            criteria=document["criteria"], pairs={"path": source / "pairs_demo.jsonl"}
        )
    else:
        data["rubric"] = document
    identity = "study-config-" + bytes_digest(
        canonical_bytes(
            {
                "study_version": STUDY_VERSION,
                "sources": digests,
                "provider": provider,
                "endpoint": endpoint,
                "model": model,
                "scenario": scenario,
                "tier": tier,
                "account_scope": account_scope,
                "service_policy_version": 2,
                "generation": data["generation"],
                "task_selection": "all",
                "capture": data["capture"],
            }
        )
    )
    data["output_path"] = Path("study-output") / identity
    return identity, RunSpecV3.model_validate(data)


def library(package: Path) -> list[tuple[str, str, RunSpecV3, bool]]:
    entries: list[tuple[str, str, RunSpecV3, bool]] = []
    for provider, model in MODELS:
        standard = {
            "openai": "default",
            "gemini": "standard",
            "anthropic": "standard_only",
        }[provider]
        for tier in (standard,) if provider == "anthropic" else (standard, "flex"):
            for scenario in SCENARIOS:
                identity, spec = configuration(package, provider, model, scenario, tier)
                entries.append(
                    (
                        identity,
                        scenario,
                        spec,
                        tier
                        == ("standard_only" if provider == "anthropic" else "flex"),
                    )
                )
    return entries


def write_library(package: Path) -> dict[str, object]:
    """Materialize concrete resolved configs; only filesystem references vary."""
    output = package / "configs"
    output.mkdir(exist_ok=True)
    index: list[dict[str, Any]] = []
    for identity, scenario, spec, main in library(package):
        data = spec.model_dump(mode="json")
        # Keep runnable files relocatable, while retaining original content in sources.
        for parent, key in ((data, "input"), (data, "pairs")):
            if parent.get(key):
                parent[key]["path"] = (
                    "../"
                    + Path(parent[key]["path"])
                    .relative_to(package.resolve())
                    .as_posix()
                )
        data["prompt"]["file"] = (
            "../"
            + Path(data["prompt"]["file"]).relative_to(package.resolve()).as_posix()
        )
        path = output / (identity + ".json")
        path.write_bytes(canonical_bytes(data) + b"\n")
        index.append(
            {
                "configuration_id": identity,
                "scenario": scenario,
                "model": spec.model,
                "tier": spec.execution.tier,
                "main_selection": main,
                "file": "configs/" + path.name,
            }
        )
    result: dict[str, object] = {
        "study_version": STUDY_VERSION,
        "configurations": index,
        "library_count": len(index),
        "main_count": sum(row["main_selection"] for row in index),
    }
    (package / "configurations.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result
