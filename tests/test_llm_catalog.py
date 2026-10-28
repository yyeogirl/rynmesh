"""Tests for the pinned three-profile model catalog."""

from __future__ import annotations

import pytest

import rynmesh.llm_package.catalog as llm_catalog

REVISIONS = {
    "light": "872f8a96064a1242ac3a3359cad77c3042548405",
    "balanced": "91cad51170dc346986eccefdc2dd33a9da36ead9",
    "quality": "7dabda4d13d513e3e842b20f0d435c732f172cbe",
}


def test_catalog_has_exactly_three_ordered_profiles():
    assert [profile.profile for profile in llm_catalog.PROFILES] == ["light", "balanced", "quality"]


@pytest.mark.parametrize("name", ["light", "balanced", "quality"])
def test_each_profile_url_is_pinned_https_on_huggingface(name):
    profile = llm_catalog.profile_by_name(name)
    assert profile.url.startswith("https://huggingface.co/")
    assert f"/resolve/{REVISIONS[name]}/" in profile.url
    # A moving ref (like "main") would silently change the bytes served.
    assert "/resolve/main/" not in profile.url


@pytest.mark.parametrize("name", ["light", "balanced", "quality"])
def test_each_profile_sha256_is_64_hex_chars(name):
    profile = llm_catalog.profile_by_name(name)
    assert len(profile.sha256) == 64
    assert set(profile.sha256) <= set("0123456789abcdef")


def test_profiles_are_strictly_increasing_in_size():
    sizes = [profile.parameter_millions for profile in llm_catalog.PROFILES]
    assert sizes == sorted(sizes)
    memory = [profile.estimated_memory_mb for profile in llm_catalog.PROFILES]
    assert memory == sorted(memory)


def test_profile_by_name_returns_the_matching_profile():
    profile = llm_catalog.profile_by_name("balanced")
    assert profile.model_alias == "rynmesh-qwen2.5-1.5b-q4"
    assert profile.display_name == "Qwen2.5-1.5B-Instruct-Q4_K_M"
    assert profile.size_bytes == 1117320736


def test_profile_by_name_raises_value_error_on_unknown_name():
    with pytest.raises(ValueError, match="unknown model profile"):
        llm_catalog.profile_by_name("nope")


def test_estimated_disk_mb_matches_the_hardware_recommend_formula():
    for profile in llm_catalog.PROFILES:
        expected = int(profile.estimated_memory_mb * 1.5 + 256)
        assert llm_catalog.estimated_disk_mb(profile) == expected


def test_catalog_module_has_no_network_or_subprocess_imports():
    import ast

    with open(llm_catalog.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"dataclasses", "__future__"}
