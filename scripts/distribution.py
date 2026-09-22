#!/usr/bin/env python3
"""Single source of truth for the minimal distributable Skill payload."""

from __future__ import annotations

BUNDLE_FORMAT = 1
BUNDLE_FILES = (
    "LICENSE",
    "SKILL.md",
    "VERSION",
    "agents/openai.yaml",
    "references/api_reference.md",
    "references/architecture.md",
    "references/host_adapters.md",
    "requirements-lock.txt",
    "requirements.txt",
    "scripts/__init__.py",
    "scripts/analyze.py",
    "scripts/cdp_profile.py",
    "scripts/cli_contract.py",
    "scripts/create_state.py",
    "scripts/demo.py",
    "scripts/deliver.py",
    "scripts/distribution.py",
    "scripts/doctor.py",
    "scripts/evaluate.py",
    "scripts/install_skill.py",
    "scripts/login_state.py",
    "scripts/monitor.py",
    "scripts/setup.py",
    "scripts/spider.py",
    "scripts/state_check.py",
    "scripts/task_manager.py",
    "scripts/version_info.py",
    "scripts/xianyu.py",
)

GENERATED_BUNDLE_FILES = ("MANIFEST.json", "SBOM.spdx.json")
OPTIONAL_PROVENANCE_FILES = GENERATED_BUNDLE_FILES
INSTALL_MANIFEST = ".xianyu-install.json"
