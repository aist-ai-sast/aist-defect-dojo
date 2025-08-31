import os
from django.conf import settings
from dataclasses import dataclass, field
from itertools import chain
from .models import AISTProject
from .utils import _load_analyzers_config

@dataclass
class PipelineArguments:
    project: AISTProject
    project_version: dict = field(default_factory=dict)
    selected_analyzers: list[str] = field(default_factory=list)
    selected_languages: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    rebuild_images: bool = False
    ask_push_to_ai: bool = True
    time_class_level: str = "slow" #TODO: change to enum
    is_initialized: bool = False
    additional_environments: dict = field(default_factory=dict)

    def __post_init__(self):
        self.aist_path = getattr(settings, "AIST_OUTPUT_PATH", os.path.join("/tmp", "aist", "output"))
        self.pipeline_path = getattr(settings, "AIST_PIPELINE_CODE_PATH", None)

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineArguments":
        """
        Build PipelineArguments instance from dictionary.
        The dictionary must contain `project_id` instead of `project`.
        """
        try:
            project = AISTProject.objects.get(id=data["project_id"])
        except AISTProject.DoesNotExist:
            raise ValueError(f"AISTProject with id={data['project_id']} not found")

        return cls(
            project=project,
            project_version=data.get("project_version") or {},
            selected_analyzers=data.get("analyzers") or [],
            selected_languages=data.get("selected_languages") or [],
            log_level=data.get("log_level") or "INFO",
            rebuild_images=data.get("rebuild_images") or False,
            ask_push_to_ai=data.get("ask_push_to_ai") if "ask_push_to_ai" in data else True,
            time_class_level=data.get("time_class_level") or "slow",
            additional_environments=data.get("env") or {},
        )

    @property
    def analyzers(self) -> list[str]:
        if self.selected_analyzers:
            return self.selected_analyzers

        cfg = _load_analyzers_config()
        if not cfg:
            return self.selected_analyzers

        filtered = cfg.get_filtered_analyzers(
            analyzers_to_run=None,
            max_time_class=self.time_class_level,
            non_compile_project=not self.project.compilable,
            target_languages=self.languages,
            show_only_parent=True
        )
        names = cfg.get_names(filtered)
        profile = self.project.profile
        if not profile:
            # Just default list, by language
            return names

        analyzer_profile = profile.get("analyzers", dict())
        if analyzer_profile:
            if analyzer_profile.get("exclude"):
                for name in analyzer_profile.get("exclude"):
                    names.remove(name)
            if analyzer_profile.get("include", None):
                for name in analyzer_profile.get("include"):
                    names.add(name)

        return names

    @property
    def languages(self) -> list[str]:
        seen = set()
        out: list[str] = []
        for lang in chain(self.selected_languages or [],
                          self.project.supported_languages or []):
            if lang not in seen:
                seen.add(lang)
                out.append(lang)
        return out

    @property
    def project_name(self) -> str:
        return self.project.product.name

    @property
    def script_path(self) -> str:
        script_path = self.project.script_path
        script_path = os.path.join(self.pipeline_path, script_path)
        if not os.path.isfile(script_path):
            raise RuntimeError("Incorrrect script path for AIST pipeline.")
        return script_path

    @property
    def output_dir(self) -> str:
        output_dir = os.path.join(self.aist_path, self.project_name or "project", self.project_version['version'] or "default")
        return output_dir

    @property
    def pipeline_src_path(self):
        return self.pipeline_path

    @property
    def dockerfile_path(self) -> str:
        dockerfile_path = os.path.join(self.pipeline_path, "Dockerfiles", "builder", "Dockerfile")
        if not os.path.isfile(dockerfile_path):
            raise RuntimeError("Dockerfile does not exist")
        return dockerfile_path