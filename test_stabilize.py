"""Regression checks for geometry, timing, crop continuity, and safe video IO."""
import io
import struct
import unittest
from unittest.mock import patch

import numpy as np
from scipy.spatial.transform import Rotation as R

import stabilize as s
from sony_metadata import camera_rates, parse_tags, Telemetry


class StabilizationTests(unittest.TestCase):
    def test_pcm_uses_original_compatible_audio_packaging(self):
        pcm = [{'codec_name': 'pcm_s16be'}]
        s.validate_audio_container('stabilized.mov', pcm)
        s.validate_audio_container('stabilized.mp4', [{'codec_name': 'aac'}])
        with self.assertRaisesRegex(ValueError, 'Use a .mov'):
            s.validate_audio_container('stabilized.mp4', pcm)

    def test_source_level_is_preserved_without_oversized_decoder_buffer(self):
        source = {'bit_rate': '98003241', 'level': 51, 'profile': 'High'}
        rate = s.encoding_rate(source)
        self.assertLessEqual(int(rate[rate.index('-maxrate')+1]), 300000000)
        self.assertLessEqual(int(rate[rate.index('-bufsize')+1]), 300000000)
        opts = s.compatibility_options(source, '24000/1001')
        self.assertEqual(opts[opts.index('-level:v')+1], '5.1')
        self.assertEqual(opts[opts.index('-profile:v')+1], 'high')
        self.assertEqual(opts[opts.index('-g')+1], '12')
        self.assertEqual(opts[opts.index('-bf')+1], '1')

    def test_source_bitrate_is_default_and_crf_requires_opt_in(self):
        options = s.encoding_rate({'bit_rate': '98003241'})
        self.assertEqual(options[options.index('-b:v')+1], '98003241')
        self.assertNotIn('-crf', options)
        self.assertEqual(s.encoding_rate({'bit_rate': '98003241'}, 18), ['-crf', '18'])
        with self.assertRaises(ValueError):
            s.encoding_rate({})

    def test_levels_keep_existing_strong_and_allow_numeric_overrides(self):
        self.assertEqual(s.level_settings('strong'), (0.5, 0.2))
        self.assertEqual(s.level_settings('balanced'), (0.35, 0.1))
        self.assertEqual(s.level_settings('light'), (0.25, 0.06))
        self.assertEqual(s.level_settings('light', smooth=0.4, max_crop=0.08), (0.4, 0.08))
        self.assertEqual(s.level_settings('balanced', max_crop=0.08), (0.35, 0.08))

    def test_lower_crop_levels_remain_inside_the_image(self):
        n, fps = 50, 25
        times = np.linspace(-0.1, 2.1, 1100)
        angles = 0.1*np.sin(times*18)
        sampler = s.make_sampler(times, R.from_rotvec(np.stack([angles*0, angles, angles*0], 1)).as_quat())
        pts = np.arange(n)/fps
        telemetry = Telemetry(times, np.zeros((len(times), 3)), pts, pts, np.full(n, .04),
                              np.tile([800, 800], (n, 1)), 0x412, 2000)
        for level in ('light', 'balanced'):
            with self.subTest(level=level):
                smooth, crop = s.level_settings(level)
                frames, zoom, strength = s.plan(telemetry, sampler, 640, 360, fps, n, 0, 1, smooth, crop)
                self.assertTrue(np.all(1-1/zoom <= crop+1e-10))
                border = s.border_points(639, 359, 80)
                self.assertTrue(all(s.covers(f, border, z, 2.99) for f, z in zip(frames, zoom)))
                self.assertTrue(np.all(np.isfinite(strength)))

    def frame(self, width=160, height=96):
        k = np.array([[200., 0, width/2], [0, 200., height/2], [0, 0, 1]])
        return s.Frame(k, np.linalg.inv(k), width, height, np.eye(3), np.zeros((s.ROW_NODES, 3)))

    def test_identity_mesh_is_exact_including_edges(self):
        mx, my = s.dense_map(self.frame(), 1, 160, 96)
        x, y = np.meshgrid(np.arange(160), np.arange(96))
        np.testing.assert_allclose(mx, x, atol=1e-5)
        np.testing.assert_allclose(my, y, atol=1e-5)

    def test_resize_pixel_centers(self):
        mx, my = s.dense_map(self.frame(), 1, 80, 48)
        x, y = np.meshgrid(np.arange(80)*2+0.5, np.arange(48)*2+0.5)
        np.testing.assert_allclose(mx, x, atol=1e-5)
        np.testing.assert_allclose(my, y, atol=1e-5)

    def test_mesh_matches_analytic_homography(self):
        f = self.frame()
        f.D = R.from_euler('xyz', [0.02, -0.03, 0.01]).as_matrix()
        mx, my = s.dense_map(f, 1.15, 160, 96)
        x, y = np.meshgrid(np.arange(160), np.arange(96))
        exact = f.map(np.stack([x.ravel(), y.ravel()], 1), 1.15).reshape(96, 160, 2)
        self.assertLess(np.max(np.abs(mx-exact[:, :, 0])), 0.02)
        self.assertLess(np.max(np.abs(my-exact[:, :, 1])), 0.02)

    def test_real_time_integration(self):
        times = np.array([0, 0.001, 0.003, 0.007, 0.012])
        rates = np.tile([0, 90, 0], (len(times), 1))
        q = s.integrate(times, rates)
        np.testing.assert_allclose(R.from_quat(q).as_rotvec()[:, 1], times*np.pi/2, atol=1e-12)

    def test_sony_axes(self):
        np.testing.assert_array_equal(camera_rates(np.array([[10., 20., 30.]]), 0x412), [[-20, 10, 30]])
        with self.assertRaises(ValueError):
            camera_rates(np.zeros((1, 3)), 0x999)

    def test_pure_yaw_correction_direction(self):
        frame = self.frame()
        # Camera yaw +1 deg, target identity => output center must sample leftward.
        frame.D = R.from_euler('y', -1, degrees=True).as_matrix()
        p = frame.map(np.array([[80., 48.]]), 1)[0]
        self.assertAlmostEqual(p[0], 80-200*np.tan(np.pi/180), places=5)

    def test_strong_motion_never_exceeds_crop(self):
        n, fps = 100, 25
        times = np.linspace(-0.1, 4.1, 2000)
        angles = 0.15*np.sin(times*18)
        rotations = R.from_rotvec(np.stack([angles*0, angles, angles*0], 1))
        sampler = s.make_sampler(times, rotations.as_quat())
        pts = np.arange(n)/fps
        telemetry = Telemetry(times, np.zeros((len(times), 3)), pts, pts, np.full(n, .04),
                              np.tile([200, 200], (n, 1)), 0x412, 2000)
        frames, zoom, strength = s.plan(telemetry, sampler, 160, 96, fps, n, 0, 1, .4, .15)
        self.assertTrue(np.all(zoom <= 1/.85))
        border = s.border_points(159, 95, 80)
        self.assertTrue(all(s.covers(f, border, z, 2.99) for f, z in zip(frames, zoom)))
        self.assertLess(np.max(np.abs(np.diff(strength))), .15)

    def test_envelope_stays_below_budget(self):
        limits = np.ones(201)
        limits[100] = .02
        smooth = s.lower_envelope(limits, 3.5)
        self.assertTrue(np.all(smooth <= limits+1e-12))
        self.assertLess(np.max(np.abs(np.diff(smooth))), .12)

    def test_short_pipe_reads_do_not_skip_frames(self):
        class Chunked(io.BytesIO):
            def read(self, n):
                return super().read(min(n, 3))
        stream = Chunked(b'abcdefghijklmno')
        self.assertEqual(s.read_exact(stream, 10), b'abcdefghij')
        self.assertEqual(s.read_exact(stream, 5), b'klmno')
        self.assertEqual(s.read_exact(stream, 10), b'')
        with self.assertRaises(ValueError):
            s.read_exact(Chunked(b'abc'), 4)

    def test_truncated_metadata_rejected(self):
        header = b'\x00\x1c'+b'\0'*26
        key = b'\x06\x0e\x2b\x34'+b'\0'*12
        body = struct.pack('>HHI', 0xe435, 4, 2000)
        self.assertEqual(parse_tags(header+key+bytes([len(body)])+body)[0xe435], struct.pack('>I', 2000))
        with self.assertRaises(ValueError):
            parse_tags(header+key+bytes([len(body)+4])+body)


if __name__ == '__main__':
    unittest.main()
