from __future__ import annotations

import tempfile
from pathlib import Path

from unreal_profile import (
    add_vendor_path,
    apply_reviewed_results,
    extract_segments,
    load_locres,
    require_safe_relative,
    stable_id,
)


def main() -> int:
    add_vendor_path()
    from pylocres import Entry, LocresFile, Namespace

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "fixture.locres"
        segments = root / "segments.jsonl"
        reviewed = root / "reviewed.jsonl"

        locres = LocresFile()
        namespace = Namespace("FIXTURE_NAMESPACE")
        namespace.add(Entry("FIXTURE_KEY", "SOURCE_FIXTURE", 1))
        locres.add(namespace)
        locres.write(source)

        count, metadata = extract_segments(source, segments)
        assert count == 1
        assert metadata["namespace_count"] == 1
        target_id = stable_id("FIXTURE_NAMESPACE", "FIXTURE_KEY")
        reviewed.write_text(
            '{"id":"' + target_id + '","status":"reviewed","text":"TARGET_FIXTURE"}\n',
            encoding="utf-8",
        )
        loaded = load_locres(source)
        assert apply_reviewed_results(loaded, reviewed) == (1, 0)
        assert list(list(loaded)[0])[0].translation == "TARGET_FIXTURE"
        assert (
            require_safe_relative("Fixture/Content/File.uasset", "fixture")
            == "Fixture/Content/File.uasset"
        )
        try:
            require_safe_relative("../escape", "fixture")
        except RuntimeError:
            pass
        else:
            raise AssertionError("path traversal was accepted")
    print("unreal-5.4 profile self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
