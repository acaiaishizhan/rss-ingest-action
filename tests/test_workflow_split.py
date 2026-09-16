from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_sopilot_has_a_dedicated_entry_and_queue():
    normal = (ROOT / ".github/workflows/rss-ingest.yml").read_text(encoding="utf-8")
    sopilot = (ROOT / ".github/workflows/sopilot-info.yml").read_text(encoding="utf-8")

    assert "name: sopilot-info" in sopilot
    assert "uses: ./.github/workflows/rss-ingest.yml" in sopilot
    assert "sopilot_batch_id: ${{ inputs.sopilot_batch_id }}" in sopilot
    assert "workflow_call:" in normal
    assert "format('sopilot-info-{0}', inputs.sopilot_batch_id)" in normal
    assert "|| 'rss-ingest-normal'" in normal
    assert "group: feishu-write" not in normal
    assert "queue: max" not in normal


def test_normal_manual_entry_cannot_impersonate_sopilot():
    normal = (ROOT / ".github/workflows/rss-ingest.yml").read_text(encoding="utf-8")
    dispatch = normal.split("  workflow_dispatch:", 1)[1].split("  workflow_call:", 1)[0]
    assert "sopilot_batch_id" not in dispatch
