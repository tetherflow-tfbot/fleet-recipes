#!/usr/bin/env python3
"""
Unit tests for FleetImporter GitOps Google Cloud Storage signed URL support.

These tests import the real processor while stubbing AutoPkg, and use in-memory
fake GCS objects so no Google credentials or network access are required.
"""

import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

if sys.version_info >= (3, 14):
    sys.stderr.write(
        "ERROR: Python {}.{} is not supported for this test suite. "
        "Use Python 3.13 (see .python-version).\n".format(
            sys.version_info.major, sys.version_info.minor
        )
    )
    sys.exit(1)

if "autopkglib" not in sys.modules:
    _stub = types.ModuleType("autopkglib")

    class _Processor:
        def __init__(self):
            self.env = {}

        def output(self, msg):
            pass

    class _ProcessorError(Exception):
        pass

    _stub.Processor = _Processor
    _stub.ProcessorError = _ProcessorError
    sys.modules["autopkglib"] = _stub

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "FleetImporter"))

import FleetImporter  # noqa: E402


class FakeBlob:
    def __init__(self, exists=False, size=0, content=b""):
        self._exists = exists
        self.size = size
        self.content = content
        self.reload_called = False
        self.uploads = []
        self.signed_url_calls = []

    def exists(self):
        return self._exists

    def reload(self):
        self.reload_called = True

    def upload_from_filename(self, filename, content_type=None):
        self.uploads.append((filename, content_type))
        self._exists = True
        self.size = Path(filename).stat().st_size

    def generate_signed_url(self, **kwargs):
        self.signed_url_calls.append(kwargs)
        return "https://storage.example/signed"

    def open(self, mode):
        if mode != "rb":
            raise ValueError("FakeBlob only supports rb")
        return io.BytesIO(self.content)


class FakeBucket:
    def __init__(self, blob):
        self._blob = blob
        self.requested_keys = []

    def blob(self, key):
        self.requested_keys.append(key)
        return self._blob


class FakeClient:
    def __init__(self, blob):
        self.bucket_names = []
        self.bucket_obj = FakeBucket(blob)

    def bucket(self, name):
        self.bucket_names.append(name)
        return self.bucket_obj


class TestGcsSignedUrls(unittest.TestCase):
    def setUp(self):
        self.fi = FleetImporter.FleetImporter()
        self.fi.env = {}
        self.tmp = tempfile.TemporaryDirectory()
        self.pkg_path = Path(self.tmp.name) / "Example.pkg"
        self.pkg_path.write_bytes(b"example package")

    def tearDown(self):
        self.tmp.cleanup()

    def _set_fake_client(self, blob):
        client = FakeClient(blob)
        self.fi._get_gcs_client = lambda: client
        return client

    def test_upload_to_gcs_uploads_missing_object(self):
        blob = FakeBlob(exists=False)
        client = self._set_fake_client(blob)

        key, was_uploaded = self.fi._upload_to_gcs(
            "fleet-packages", "Example", "1.2.3", self.pkg_path
        )

        self.assertEqual(key, "software/Example/Example-1.2.3.pkg")
        self.assertTrue(was_uploaded)
        self.assertEqual(client.bucket_names, ["fleet-packages"])
        self.assertEqual(blob.uploads[0][1], "application/octet-stream")

    def test_upload_to_gcs_skips_existing_matching_object(self):
        blob = FakeBlob(exists=True, size=self.pkg_path.stat().st_size)
        self._set_fake_client(blob)

        key, was_uploaded = self.fi._upload_to_gcs(
            "fleet-packages", "Example", "1.2.3", self.pkg_path
        )

        self.assertEqual(key, "software/Example/Example-1.2.3.pkg")
        self.assertFalse(was_uploaded)
        self.assertTrue(blob.reload_called)
        self.assertEqual(blob.uploads, [])

    def test_upload_to_gcs_reuploads_existing_size_mismatch(self):
        blob = FakeBlob(exists=True, size=1)
        self._set_fake_client(blob)

        _, was_uploaded = self.fi._upload_to_gcs(
            "fleet-packages", "Example", "1.2.3", self.pkg_path
        )

        self.assertTrue(was_uploaded)
        self.assertEqual(len(blob.uploads), 1)

    def test_generate_gcs_signed_url_uses_v4_get_and_expiration(self):
        blob = FakeBlob(exists=True)
        self._set_fake_client(blob)
        self.fi.env = {"gcp_signed_url_expiration": "3600"}

        signed_url = self.fi._generate_gcs_signed_url(
            "fleet-packages", "software/Example/Example-1.2.3.pkg"
        )

        self.assertEqual(signed_url, "https://storage.example/signed")
        call = blob.signed_url_calls[0]
        self.assertEqual(call["version"], "v4")
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["expiration"].total_seconds(), 3600)

    def test_signed_url_expiration_rejects_values_over_gcs_max(self):
        self.fi.env = {"gcp_signed_url_expiration": "604801"}

        with self.assertRaises(FleetImporter.ProcessorError):
            self.fi._get_gcs_signed_url_expiration()

    def test_calculate_gcs_file_sha256_downloads_object(self):
        blob = FakeBlob(exists=True, content=b"abc")
        self._set_fake_client(blob)

        digest = self.fi._calculate_gcs_file_sha256(
            "fleet-packages", "software/Example/Example-1.2.3.pkg"
        )

        self.assertEqual(
            digest,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_get_gcs_client_accepts_raw_service_account_json(self):
        credentials_info = {
            "type": "service_account",
            "project_id": "fleet-project",
            "client_email": "fleet@example.iam.gserviceaccount.com",
        }
        self.fi.env = {"gcp_credentials_json": json.dumps(credentials_info)}

        original_modules = {
            name: sys.modules.get(name)
            for name in (
                "google",
                "google.cloud",
                "google.cloud.storage",
                "google.oauth2",
                "google.oauth2.service_account",
            )
        }

        class FakeCredentials:
            project_id = "fleet-project"

        class FakeCredentialsFactory:
            received_info = None

            @classmethod
            def from_service_account_info(cls, info):
                cls.received_info = info
                return FakeCredentials()

        class FakeStorageClient:
            received_credentials = None
            received_project = None

            def __init__(self, credentials=None, project=None):
                self.__class__.received_credentials = credentials
                self.__class__.received_project = project

        google_module = types.ModuleType("google")
        cloud_module = types.ModuleType("google.cloud")
        storage_module = types.ModuleType("google.cloud.storage")
        oauth2_module = types.ModuleType("google.oauth2")
        service_account_module = types.ModuleType("google.oauth2.service_account")

        storage_module.Client = FakeStorageClient
        service_account_module.Credentials = FakeCredentialsFactory
        cloud_module.storage = storage_module
        oauth2_module.service_account = service_account_module
        google_module.cloud = cloud_module
        google_module.oauth2 = oauth2_module

        sys.modules["google"] = google_module
        sys.modules["google.cloud"] = cloud_module
        sys.modules["google.cloud.storage"] = storage_module
        sys.modules["google.oauth2"] = oauth2_module
        sys.modules["google.oauth2.service_account"] = service_account_module

        try:
            client = self.fi._get_gcs_client()
        finally:
            for name, module in original_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.assertIsInstance(client, FakeStorageClient)
        self.assertEqual(FakeCredentialsFactory.received_info, credentials_info)
        self.assertIsInstance(
            FakeStorageClient.received_credentials, FakeCredentials
        )
        self.assertEqual(FakeStorageClient.received_project, "fleet-project")


if __name__ == "__main__":
    unittest.main()
