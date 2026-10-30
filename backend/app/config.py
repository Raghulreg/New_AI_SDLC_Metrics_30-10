from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _int_list(value: str | None) -> list[int]:
    return [int(v) for v in _list(value)]


def _identity_mappings(value: str | None) -> dict[str, str]:
    """Parse ``ADO identity=Cursor email`` pairs without guessing aliases."""
    mappings: dict[str, str] = {}
    for item in _list(value):
        source, separator, target = item.partition("=")
        source, target = source.strip().casefold(), target.strip().casefold()
        if not separator or not source or "@" not in target:
            raise ValueError("CURSOR_IDENTITY_MAPPINGS entries must be ADO identity=Cursor email")
        mappings[source] = target
    return mappings


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str = "Development"
    log_level: str = "INFO"

    ado_org_url: str = ""
    ado_default_project: str = ""
    ado_api_version: str = "7.1"
    ado_repository_ids: list[str] = field(default_factory=list)
    ado_pat: str = field(default="", repr=False)

    key_vault_url: str = ""
    cursor_api_key_secret_name: str = ""
    cursor_api_base_url: str = "https://api.cursor.com"
    cursor_api_key: str = field(default="", repr=False)
    # Explicit aliases for different ADO and Cursor identities. Keys are ADO id or uniqueName.
    cursor_identity_mappings: dict[str, str] = field(default_factory=dict)

    story_points_field: str = "Microsoft.VSTS.Scheduling.StoryPoints"
    sprint_work_item_types: list[str] = field(
        default_factory=lambda: ["User Story", "Product Backlog Item", "Requirement"]
    )

    defect_work_item_type: str = "Bug"
    defect_severity_field: str = "Microsoft.VSTS.Common.Severity"
    defect_rate_denominator: str = ""

    coverage_build_definition_ids: list[int] = field(default_factory=list)
    coverage_branch: str = ""
    coverage_stat_label: str = "Lines"

    ai_pr_labels: list[str] = field(default_factory=lambda: ["ai-assisted"])
    ai_commit_lookback_days: int = 30

    # LLM code analysis is opt-in: enabling it sends PR diff text to the configured model endpoint.
    llm_code_analysis_enabled: bool = False
    llm_provider: str = ""
    llm_endpoint: str = ""
    llm_model: str = ""
    llm_api_version: str = "2024-10-21"
    llm_api_key: str = field(default="", repr=False)
    llm_api_key_secret_name: str = ""
    llm_min_confidence: float = 0.7
    llm_max_commits: int = 50
    llm_max_files_per_commit: int = 20
    llm_max_lines_per_commit: int = 600
    llm_requests_per_minute: int = 30
    llm_exclude_paths: list[str] = field(default_factory=lambda: [
        "*.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "*.min.js", "*.min.css",
        "*.map", "*.snap",
    ])

    http_timeout_seconds: float = 30.0
    cache_ttl_minutes: int = 15
    freshness_window_minutes: int = 60
    max_pr_detail_fetch: int = 300

    @property
    def ado_configured(self) -> bool:
        return bool(self.ado_org_url)

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in {"production", "prod"}

    @property
    def ado_auth_mode(self) -> str:
        """PAT is a local-development convenience only; deployed workloads use DefaultAzureCredential
        (Managed Identity)."""
        return "pat" if self.ado_pat and not self.is_production else "default_azure_credential"

    @property
    def cursor_configured(self) -> bool:
        return bool(self.cursor_api_key) or bool(self.key_vault_url and self.cursor_api_key_secret_name)

    @property
    def llm_endpoint_url(self) -> str:
        if self.llm_endpoint:
            return self.llm_endpoint.rstrip("/")
        if self.llm_provider == "openai":
            return "https://api.openai.com/v1"
        if self.llm_provider == "gemini":
            return "https://generativelanguage.googleapis.com"
        return ""

    @property
    def llm_configured(self) -> bool:
        has_key = bool(self.llm_api_key) or bool(self.key_vault_url and self.llm_api_key_secret_name)
        return (self.llm_code_analysis_enabled and self.llm_provider in {"azure_openai", "openai", "gemini"}
                and bool(self.llm_endpoint_url) and bool(self.llm_model) and has_key)


def read_env_files(paths: list[Path]) -> dict[str, str]:
    """Merge .env files. Later files win, empty values never override a real value, and nothing is
    written into the process environment (so secrets are not inherited by child processes)."""
    values: dict[str, str] = {}
    for path in paths:
        if path.is_file():
            values.update({k: v for k, v in dotenv_values(path).items() if v})
    return values


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Real environment variables take precedence over .env files."""
    if env_file is None:
        here = Path(__file__).resolve()
        # backend/.env (where local credentials live) overrides the project-root .env.
        paths = [here.parents[2] / ".env", here.parents[1] / ".env"]
    else:
        paths = [Path(env_file)]
    merged = {**read_env_files(paths), **os.environ}

    def e(key: str, default: str | None = None) -> str | None:
        return merged.get(key, default)

    return Settings(
        app_env=e("APP_ENV", "Development"),
        log_level=e("LOG_LEVEL", "INFO"),
        ado_org_url=(e("AZURE_DEVOPS_ORG_URL") or "").rstrip("/"),
        ado_default_project=e("AZURE_DEVOPS_DEFAULT_PROJECT", ""),
        ado_api_version=e("AZURE_DEVOPS_API_VERSION") or "7.1",
        ado_repository_ids=_list(e("ADO_REPOSITORY_IDS")),
        ado_pat=(e("AZURE_DEVOPS_PAT") or "").strip(),
        key_vault_url=e("AZURE_KEY_VAULT_URL", ""),
        cursor_api_key_secret_name=e("CURSOR_API_KEY_SECRET_NAME", ""),
        cursor_api_base_url=(e("CURSOR_API_BASE_URL") or "https://api.cursor.com").rstrip("/"),
        cursor_api_key=e("CURSOR_API_KEY", ""),
        cursor_identity_mappings=_identity_mappings(e("CURSOR_IDENTITY_MAPPINGS")),
        story_points_field=e("STORY_POINTS_FIELD") or "Microsoft.VSTS.Scheduling.StoryPoints",
        sprint_work_item_types=_list(e("SPRINT_WORK_ITEM_TYPES"))
        or ["User Story", "Product Backlog Item", "Requirement"],
        defect_work_item_type=e("DEFECT_WORK_ITEM_TYPE") or "Bug",
        defect_severity_field=e("DEFECT_SEVERITY_FIELD") or "Microsoft.VSTS.Common.Severity",
        defect_rate_denominator=(e("DEFECT_RATE_DENOMINATOR") or "").strip().lower(),
        coverage_build_definition_ids=_int_list(e("COVERAGE_BUILD_DEFINITION_IDS")),
        coverage_branch=e("COVERAGE_BRANCH", ""),
        coverage_stat_label=e("COVERAGE_STAT_LABEL") or "Lines",
        ai_pr_labels=[v.lower() for v in _list(e("AI_PR_LABELS"))] or ["ai-assisted"],
        ai_commit_lookback_days=int(e("AI_COMMIT_LOOKBACK_DAYS") or 30),
        llm_code_analysis_enabled=_bool(e("LLM_CODE_ANALYSIS_ENABLED")),
        llm_provider=(e("LLM_PROVIDER") or "").strip().lower(),
        llm_endpoint=(e("LLM_ENDPOINT") or "").strip(),
        llm_model=(e("LLM_MODEL") or "").strip(),
        llm_api_version=e("LLM_API_VERSION") or "2024-10-21",
        llm_api_key=(e("LLM_API_KEY") or "").strip(),
        llm_api_key_secret_name=e("LLM_API_KEY_SECRET_NAME", ""),
        llm_min_confidence=float(e("LLM_MIN_CONFIDENCE") or 0.7),
        llm_max_commits=int(e("LLM_MAX_COMMITS") or 50),
        llm_max_files_per_commit=int(e("LLM_MAX_FILES_PER_COMMIT") or 20),
        llm_max_lines_per_commit=int(e("LLM_MAX_LINES_PER_COMMIT") or 600),
        llm_requests_per_minute=int(e("LLM_REQUESTS_PER_MINUTE") or 30),
        llm_exclude_paths=_list(e("LLM_EXCLUDE_PATHS")) or Settings().llm_exclude_paths,
        http_timeout_seconds=float(e("HTTP_TIMEOUT_SECONDS") or 30),
        cache_ttl_minutes=int(e("CACHE_TTL_MINUTES") or 15),
        freshness_window_minutes=int(e("FRESHNESS_WINDOW_MINUTES") or 60),
        max_pr_detail_fetch=int(e("MAX_PR_DETAIL_FETCH") or 300),
    )
