import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def heading_slugs(document):
    seen = Counter()
    slugs = set()
    in_code = False
    for line in document.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not in_code and heading:
            slug = re.sub(r"[^\w\- ]", "", heading[1].lower()).replace(" ", "-")
            slugs.add(f"{slug}-{seen[slug]}" if seen[slug] else slug)
            seen[slug] += 1
    return slugs


def assert_design_links(document, slugs):
    links = re.findall(r"design\.md#([^\s)\]>]+)", document)
    for anchor in links:
        assert anchor in slugs, f"UNRESOLVED_DESIGN_ANCHOR: {anchor}"
    return len(links)


def test_all_testing_design_links_resolve_to_public_headings():
    slugs = heading_slugs((ROOT / "docs/design.md").read_text())
    assert {"measurement-rules", "tts-sentence-timing"} <= slugs
    count = sum(
        assert_design_links(path.read_text(), slugs)
        for path in (ROOT / "docs/testing").glob("*.md")
    )
    assert count >= 4


def test_design_link_checker_rejects_broken_anchor():
    slugs = heading_slugs("### Measurement rules\n### TTS sentence timing\n")
    assert assert_design_links("[rules](../design.md#measurement-rules)", slugs) == 1
    with pytest.raises(AssertionError, match="UNRESOLVED_DESIGN_ANCHOR: broken"):
        assert_design_links("[rules](../design.md#broken)", slugs)
