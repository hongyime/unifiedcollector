#!/usr/bin/env python3
import unittest

from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

from src.core.media_policy import classify_document_media


class FakeDocument:
    def __init__(self, mime_type="", attributes=None):
        self.mime_type = mime_type
        self.attributes = attributes or []


class MediaPolicyTests(unittest.TestCase):
    def test_accepts_supported_document_backed_media(self):
        image_doc = FakeDocument(
            mime_type="image/png",
            attributes=[DocumentAttributeFilename("photo.png")],
        )
        video_doc = FakeDocument(
            mime_type="video/quicktime",
            attributes=[
                DocumentAttributeVideo(0, 0, 0, False),
                DocumentAttributeFilename("clip.mov"),
            ],
        )
        video_note_doc = FakeDocument(
            mime_type="video/mp4",
            attributes=[DocumentAttributeVideo(0, 0, 0, True)],
        )

        self.assertEqual(classify_document_media(image_doc), ("image", "png"))
        self.assertEqual(classify_document_media(video_doc), ("video", "mov"))
        self.assertEqual(classify_document_media(video_note_doc), ("videonote", "mp4"))

    def test_rejects_non_media_documents_and_unsupported_formats(self):
        docx_doc = FakeDocument(
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            attributes=[DocumentAttributeFilename("report.docx")],
        )
        zip_doc = FakeDocument(
            mime_type="application/zip",
            attributes=[DocumentAttributeFilename("archive.zip")],
        )
        audio_doc = FakeDocument(
            mime_type="audio/ogg",
            attributes=[DocumentAttributeFilename("voice.ogg")],
        )
        webm_doc = FakeDocument(
            mime_type="video/webm",
            attributes=[
                DocumentAttributeVideo(0, 0, 0, False),
                DocumentAttributeFilename("clip.webm"),
            ],
        )
        animated_gif_doc = FakeDocument(
            mime_type="image/gif",
            attributes=[DocumentAttributeAnimated(), DocumentAttributeFilename("anim.gif")],
        )

        self.assertIsNone(classify_document_media(docx_doc))
        self.assertIsNone(classify_document_media(zip_doc))
        self.assertIsNone(classify_document_media(audio_doc))
        self.assertIsNone(classify_document_media(webm_doc))
        self.assertIsNone(classify_document_media(animated_gif_doc))


if __name__ == "__main__":
    unittest.main()
