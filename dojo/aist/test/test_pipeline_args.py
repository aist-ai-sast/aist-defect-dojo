# dojo/aist/test/test_pipeline_args.py
# -*- coding: utf-8 -*-
"""
Unit tests for PipelineArguments.analyzers profile logic.

Covers:
- default analyzers when project.profile is empty/absent
- include/exclude behavior when project.profile['analyzers'] is provided
"""

from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth import get_user_model

from dojo.models import Product, Product_Type, SLA_Configuration
from dojo.aist.models import AISTProject
from dojo.aist.pipeline_args import PipelineArguments


class DummyCfg:
    """Minimal stub to emulate analyzers config object."""
    def __init__(self, available):
        # use a set to match .add/.remove usage in PipelineArguments
        self._available = set(available)

    def get_filtered_analyzers(self, analyzers_to_run, max_time_class,
                               non_compile_project, target_languages,
                               show_only_parent):
        # For the test we ignore args and return pre-defined set
        return self._available

    def get_names(self, analyzer_set):
        # Return a set, since PipelineArguments expects .remove()/.add()
        return set(analyzer_set)


class PipelineArgsProfileTests(TestCase):
    def setUp(self):
        # Minimal objects for AISTProject
        self.user = get_user_model().objects.create(
            username="tester", email="tester@example.com", password="pass"
        )
        self.sla = SLA_Configuration.objects.create(name="SLA default")
        self.prod_type = Product_Type.objects.create(name="PT")
        self.product = Product.objects.create(
            name="Test Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id
        )

    @patch("dojo.aist.pipeline_args._load_analyzers_config")
    def test_analyzers_without_profile_returns_default(self, load_cfg_mock):
        """
        When project.profile is empty/absent:
        - analyzers property must return the default names from config
        filtered by languages/time_class/compilable (we stub to a fixed set).
        """
        load_cfg_mock.return_value = DummyCfg({"cppcheck", "bandit", "semgrep"})

        project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["cpp", "python"],
            script_path="scripts/build_and_scan.sh",
            compilable=True,
            profile={},  # no special analyzers profile
        )

        args = PipelineArguments(
            project=project,
            project_version="123",
            selected_analyzers=[],     # force reading from config
            selected_languages=["javascript"],  # will be merged with project languages
            log_level="INFO",
            rebuild_images=False,
            ask_push_to_ai=True,
            time_class_level="slow",
        )

        # Should equal the default config names
        self.assertEqual(set(args.analyzers), {"cppcheck", "bandit", "semgrep"})
        self.assertEqual(set(args.languages), {"cpp", "python", "javascript"})

    @patch("dojo.aist.pipeline_args._load_analyzers_config")
    def test_analyzers_with_profile_include_exclude(self, load_cfg_mock):
        """
        When project.profile['analyzers'] is present:
        - remove items in 'exclude'
        - add items in 'include'
        """
        load_cfg_mock.return_value = DummyCfg({"cppcheck", "bandit"})  # start set

        project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["cpp", "python"],
            script_path="scripts/build_and_scan.sh",
            compilable=True,
            profile={
                "analyzers": {
                    "exclude": ["bandit"],
                    "include": ["semgrep"],
                }
            },  # profile-driven modifications
        )

        args = PipelineArguments(
            project=project,
            project_version="123",
            selected_analyzers=[],     # force reading from config
            selected_languages=[],
            log_level="INFO",
            rebuild_images=False,
            ask_push_to_ai=True,
            time_class_level="slow",
        )

        # Expected = {"cppcheck"} (default) - {"bandit"} + {"semgrep"} = {"cppcheck", "semgrep"}
        self.assertEqual(set(args.analyzers), {"cppcheck", "semgrep"})
        self.assertEqual(set(args.languages), {"cpp", "python"})
