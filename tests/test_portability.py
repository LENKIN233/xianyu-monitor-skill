from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_skill_uses_strict_portable_frontmatter() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]
    keys = [line.split(":", 1)[0] for line in frontmatter.splitlines() if ":" in line]

    assert keys == ["name", "description"]
    assert "name: xianyu-monitor" in frontmatter


def test_core_skill_has_no_host_specific_path_or_silence_tokens() -> None:
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "{baseDir}" not in text
    assert "HEARTBEAT_OK" not in text
    assert "NO_REPLY" not in text
    assert "openclaw cron" not in text.lower()
    assert "references/host_adapters.md" in text


def test_every_referenced_skill_resource_exists() -> None:
    for relative_path in (
        "references/api_reference.md",
        "references/architecture.md",
        "references/host_adapters.md",
        "scripts/create_state.py",
        "scripts/login_state.py",
        "scripts/monitor.py",
        "scripts/spider.py",
        "scripts/task_manager.py",
    ):
        assert (ROOT / relative_path).exists(), relative_path
