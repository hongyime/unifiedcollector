#!/usr/bin/env python3
import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaDocument,
)

import src.core.state_manager as state_manager_module
from src.core.state_manager import StateManager, shutdown_state_manager
from src.managers.processors.media_downloader_processor import MediaDownloaderProcessor


class FakeDocument:
    def __init__(self, mime_type="", attributes=None):
        self.mime_type = mime_type
        self.attributes = attributes or []


class FakePhotoMedia:
    pass


class FakeMessage:
    def __init__(self, message_id, media):
        from datetime import datetime
        self.id = message_id
        self.media = media
        self.date = datetime(2026, 1, 1, 12, 0, 0)


class FakeClient:
    def __init__(self):
        self.download_calls = []

    async def download_media(self, message, file):
        self.download_calls.append((message.id, file))
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test-media")


class MediaDownloaderProcessorTests(unittest.TestCase):
    def setUp(self):
        shutdown_state_manager()
        StateManager._instance = None
        self.state = StateManager(":memory:")
        self.state._shutdown = True
        state_manager_module._state_manager = self.state

    def tearDown(self):
        shutdown_state_manager()
        StateManager._instance = None
        state_manager_module._state_manager = None

    def _build_processor(self, save_path):
        processor = MediaDownloaderProcessor(save_path=save_path)
        processor.state = self.state
        return processor

    def test_classify_document_media_accepts_supported_types(self):
        with TemporaryDirectory() as temp_dir:
            processor = self._build_processor(temp_dir)

            image_doc = FakeDocument(
                mime_type="image/png",
                attributes=[DocumentAttributeFilename("photo.png")],
            )
            video_doc = FakeDocument(
                mime_type="video/mp4",
                attributes=[DocumentAttributeVideo(0, 0, 0, False), DocumentAttributeFilename("clip.mp4")],
            )
            video_note_doc = FakeDocument(
                mime_type="video/mp4",
                attributes=[DocumentAttributeVideo(0, 0, 0, True)],
            )

            self.assertEqual(processor.classify_document_media(image_doc), ("image", "png"))
            self.assertEqual(processor.classify_document_media(video_doc), ("video", "mp4"))
            self.assertEqual(processor.classify_document_media(video_note_doc), ("videonote", "mp4"))

    def test_classify_document_media_rejects_unsupported_types(self):
        with TemporaryDirectory() as temp_dir:
            processor = self._build_processor(temp_dir)

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
                attributes=[DocumentAttributeVideo(0, 0, 0, False), DocumentAttributeFilename("clip.webm")],
            )
            animated_doc = FakeDocument(
                mime_type="image/gif",
                attributes=[DocumentAttributeAnimated(), DocumentAttributeFilename("anim.gif")],
            )

            self.assertIsNone(processor.classify_document_media(docx_doc))
            self.assertIsNone(processor.classify_document_media(zip_doc))
            self.assertIsNone(processor.classify_document_media(audio_doc))
            self.assertIsNone(processor.classify_document_media(webm_doc))
            self.assertIsNone(processor.classify_document_media(animated_doc))

    def test_process_message_skips_unsupported_document_downloads(self):
        with TemporaryDirectory() as temp_dir:
            processor = self._build_processor(temp_dir)
            client = FakeClient()
            message = FakeMessage(
                50,
                MessageMediaDocument(
                    document=FakeDocument(
                        mime_type="application/zip",
                        attributes=[DocumentAttributeFilename("archive.zip")],
                    )
                ),
            )

            asyncio.run(
                processor.process_message(
                    {
                        "message": message,
                        "group_name": "Audit Group",
                        "group_id": "123",
                        "account_name": "acct1",
                        "client": client,
                    }
                )
            )

            self.assertEqual(client.download_calls, [])
            self.assertEqual(processor.stats["unsupported_skipped"], 1)
            self.assertEqual(processor.stats["media_downloaded"], 0)

    def test_process_message_downloads_supported_video_documents(self):
        with TemporaryDirectory() as temp_dir:
            processor = self._build_processor(temp_dir)
            client = FakeClient()
            message = FakeMessage(
                51,
                MessageMediaDocument(
                    document=FakeDocument(
                        mime_type="video/mp4",
                        attributes=[DocumentAttributeVideo(0, 0, 0, False), DocumentAttributeFilename("clip.mp4")],
                    )
                ),
            )

            asyncio.run(
                processor.process_message(
                    {
                        "message": message,
                        "group_name": "Audit Group",
                        "group_id": "123",
                        "account_name": "acct1",
                        "client": client,
                    }
                )
            )

            self.assertEqual(len(client.download_calls), 1)
            self.assertEqual(processor.stats["media_downloaded"], 1)


if __name__ == "__main__":
    unittest.main()
