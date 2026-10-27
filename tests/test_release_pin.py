import json
from unittest.mock import patch


def test_release_pin_refuses_missing_or_different_published_version(tmp_path):
    from tools import check_release_pin

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"requirements": ["assist-live-client==0.0.0"]}))
    with patch("tools.check_release_pin.published", return_value=False):
        assert (
            check_release_pin.main(
                [
                    "--manifest",
                    str(manifest),
                    "--pypi-version",
                    "0.0.0",
                    "--require-published",
                ]
            )
            == 1
        )
    with patch("tools.check_release_pin.published", return_value=True) as published:
        assert (
            check_release_pin.main(
                [
                    "--manifest",
                    str(manifest),
                    "--pypi-version",
                    "0.0.0",
                    "--require-published",
                ]
            )
            == 0
        )
        published.assert_called_once_with("0.0.0")
        assert (
            check_release_pin.main(
                [
                    "--manifest",
                    str(manifest),
                    "--pypi-version",
                    "0.0.1",
                    "--require-published",
                ]
            )
            == 1
        )


def test_publication_requires_the_version_and_a_package_file():
    import io

    from tools.check_release_pin import published

    for body, want in [
        ({"info": {"version": "0.0.0"}, "urls": [{"filename": "client.whl"}]}, True),
        ({"info": {"version": "0.0.1"}, "urls": [{"filename": "client.whl"}]}, False),
        ({"info": {"version": "0.0.0"}, "urls": []}, False),
    ]:
        with patch(
            "tools.check_release_pin.urlopen",
            return_value=io.BytesIO(json.dumps(body).encode()),
        ):
            assert published("0.0.0") is want
