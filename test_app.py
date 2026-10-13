"""Desktop worker, file safety, and packaging entry-point checks."""

from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import app


class DesktopAppTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Path(self.folder.name) / 'camera clip.MP4'
        self.source.write_bytes(b'sample')
        self.output = Path(self.folder.name) / 'stabilized.mov'

    def wait_for_job(self, job):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = job.snapshot()
            if status['state'] != 'running':
                return status
            time.sleep(.02)
        self.fail('Worker did not finish')

    def test_output_path_is_suggested_and_existing_files_are_protected(self):
        self.assertEqual(app.suggested_output(self.source),
                         str(self.source.with_name('camera clip_stabilized_balanced.mov')))
        with self.assertRaisesRegex(ValueError, 'different name'):
            app.validate_request(str(self.source), str(self.source), 'balanced')
        self.output.write_bytes(b'existing')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            app.validate_request(str(self.source), str(self.output), 'balanced')
        self.assertEqual(self.output.read_bytes(), b'existing')

    def test_worker_uses_original_bitrate_and_audio_defaults(self):
        command = app.backend_command(self.source, self.output, 'balanced')
        self.assertEqual(command[0], sys.executable)
        self.assertIn('--worker', command)
        self.assertIn('--refine', command)
        self.assertEqual(command[command.index('--level') + 1], 'balanced')
        self.assertEqual(command[command.index('-o') + 1], str(self.output))
        self.assertNotIn('--crf', command)
        self.assertNotIn('--width', command)
        self.assertNotIn('--overwrite', command)

    def test_xml_sidecar_can_be_auto_found_or_selected_explicitly(self):
        xml = self.source.with_name('camera clipM01.XML')
        xml.write_text('<NonRealTimeMeta/>')
        self.assertEqual(app.matching_xml(self.source), xml)
        self.assertEqual(app.validate_request(str(self.source), str(self.output), 'balanced', str(xml))[2],
                         xml.resolve())
        command = app.backend_command(self.source, self.output, 'balanced', xml)
        self.assertEqual(command[command.index('--xml') + 1], str(xml))
        self.assertNotIn('--xml', app.backend_command(self.source, self.output, 'balanced'))

    def test_invalid_selected_xml_is_rejected_before_rendering(self):
        with self.assertRaisesRegex(ValueError, 'XML file was not found'):
            app.validate_request(str(self.source), str(self.output), 'balanced',
                                 str(Path(self.folder.name) / 'wrong.XML'))

    def test_frozen_worker_runs_same_executable_and_finds_bundled_ffmpeg(self):
        with patch.object(app.sys, 'frozen', True, create=True), \
             patch.object(app.sys, '_MEIPASS', self.folder.name, create=True):
            command = app.backend_command(self.source, self.output, 'light')
            self.assertEqual(command[:2], [sys.executable, '--worker'])
            self.assertTrue(app.ffmpeg_environment()['PATH'].startswith(self.folder.name))

    def test_worker_reports_completion(self):
        script = ('import pathlib,sys,time; '
                  'print("  rendering 1/2", flush=True); '
                  'time.sleep(.05); '
                  'pathlib.Path(sys.argv[1]).write_bytes(b"video"); '
                  'print("Done: stabilized.mov (2 frames verified)", flush=True)')
        command = [sys.executable, '-u', '-c', script, str(self.output)]
        job = app.JobState()
        with patch('app.shutil.which', return_value='/usr/bin/ffmpeg'), \
             patch('app.backend_command', return_value=command):
            job.start(str(self.source), str(self.output), 'balanced')
        final = self.wait_for_job(job)
        self.assertEqual(final['state'], 'done')
        self.assertEqual(final['stage'], 'Video saved')
        self.assertIn('rendering 1/2', '\n'.join(final['log']))
        self.assertEqual(self.output.read_bytes(), b'video')

    def test_worker_error_is_visible(self):
        command = [sys.executable, '-u', '-c',
                   'import sys; print("Error: gyro metadata missing", file=sys.stderr); sys.exit(1)']
        job = app.JobState()
        with patch('app.shutil.which', return_value='/usr/bin/ffmpeg'), \
             patch('app.backend_command', return_value=command):
            job.start(str(self.source), str(self.output), 'light')
        final = self.wait_for_job(job)
        self.assertEqual(final['state'], 'failed')
        self.assertEqual(final['error'], 'gyro metadata missing')
        self.assertFalse(self.output.exists())

    def test_worker_entry_point_needs_no_desktop_toolkit(self):
        result = subprocess.run([sys.executable, str(app.ROOT / 'app.py'), '--worker', '--help'],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--level', result.stdout)


if __name__ == '__main__':
    unittest.main()
