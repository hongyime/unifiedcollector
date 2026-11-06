from __future__ import annotations

import pytest

from src.core.profile_media import (
    approved_profile_image_url,
    profile_image_extension,
    write_qualified_profile_image,
)


def test_profile_media_rejects_non_https_or_unapproved_origins():
    assert approved_profile_image_url("https://pps.whatsapp.net/avatar", ("whatsapp.net",))
    assert not approved_profile_image_url("http://pps.whatsapp.net/avatar", ("whatsapp.net",))
    assert not approved_profile_image_url("https://whatsapp.net.evil.test/avatar", ("whatsapp.net",))
    assert profile_image_extension("image/jpeg", b"not-an-image") is None


def test_profile_media_atomic_write_hashes_and_reads_back(tmp_path):
    artifact, digest = write_qualified_profile_image(
        source="whatsapp",
        artifact_id="profile_photo/contact",
        data=b"\xff\xd8\xffavatar",
        extension="jpg",
        metadata={"provenance": "test"},
        root=tmp_path,
    )

    assert artifact.path and artifact.path.is_file()
    assert len(digest) == 64
