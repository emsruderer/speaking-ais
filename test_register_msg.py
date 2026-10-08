"""Tests for warning-message storage initialization."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import register_msg


class RegisterMessageTests(unittest.TestCase):
    def test_message_is_spoken_without_creating_storage(self):
        speech = Mock()
        fake_speech = Mock(speak=speech)
        with tempfile.TemporaryDirectory() as temporary_directory:
            original_directory = Path.cwd()
            os.chdir(temporary_directory)
            try:
                with patch.dict(sys.modules, {"speech": fake_speech}):
                    register_msg.add_msg("Test warning")
                self.assertEqual(speech.call_args.args, ("Test warning",))
                self.assertEqual(list(Path(temporary_directory).iterdir()), [])
            finally:
                os.chdir(original_directory)


if __name__ == "__main__":
    unittest.main()
