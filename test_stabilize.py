"""Regression checks for geometry, timing, crop continuity, and safe video IO."""
import io
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from scipy.spatial.transform import Rotation as R

import stabilize as s
from sony_metadata import camera_rates, load_telemetry, motion_table, parse_tags, Telemetry


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

    def test_high422_bitrate_and_profile(self):
        source = {'bit_rate': '400000000', 'level': 51, 'profile': 'High 4:2:2',
                  'codec_name': 'h264', 'pix_fmt': 'yuv422p10le'}
        s.validate_video_format(source)
        rate = s.encoding_rate(source)
        self.assertEqual(rate[rate.index('-b:v')+1], '400000000')
        self.assertGreaterEqual(int(rate[rate.index('-maxrate')+1]), 400000000)
        opts = s.compatibility_options(source, '30000/1001')
        self.assertEqual(opts[opts.index('-profile:v')+1], 'high422')
        intra = {**source, 'profile': 'High 4:2:2 Intra'}
        self.assertEqual(s.encoding_rate(intra)[1], '400000000')
        intra_opts = s.compatibility_options(intra, '30000/1001')
        self.assertEqual(intra_opts[intra_opts.index('-g')+1], '1')
        self.assertEqual(intra_opts[intra_opts.index('-bf')+1], '0')
        with self.assertRaisesRegex(ValueError, 'pixel formats'):
            s.validate_video_format({**source, 'pix_fmt': 'yuv420p10le'})

    def test_hevc_422_options_preserve_source_level_and_bitrate(self):
        source = {'codec_name': 'hevc', 'pix_fmt': 'yuv422p10le', 'profile': 'Rext',
                  'level': 153, 'bit_rate': '200000000'}
        s.validate_video_format(source)
        self.assertEqual(s.encoding_rate(source), ['-b:v', '200000000'])
        opts = s.compatibility_options(source, '60000/1001')
        self.assertEqual(opts[opts.index('-profile:v')+1], 'main422-10')
        self.assertIn('level-idc=5.1', opts[opts.index('-x265-params')+1])

    def test_10bit_422_identity_warp_keeps_all_samples(self):
        width, height = 64, 32
        y = np.arange(width*height, dtype='<u2').reshape(height, width) % 1024
        u = np.full((height, width//2), 713, dtype='<u2')
        v = np.full((height, width//2), 411, dtype='<u2')
        mx, my = s.dense_map(self.frame(width, height), 1, width, height)
        data = y.tobytes() + u.tobytes() + v.tobytes()
        self.assertEqual(s.remap_yuv_frame(data, 'yuv422p10le', width, height, mx, my), data)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is unavailable')
    def test_ffmpeg_render_keeps_source_pixel_format(self):
        with tempfile.TemporaryDirectory() as directory:
            for pix_fmt, profile, ffmpeg_profile, intra in (
                    ('yuv420p', 'High', 'high', False),
                    ('yuv422p10le', 'High 4:2:2', 'high422', False),
                    ('yuv422p10le', 'High 4:2:2 Intra', 'high422', True)):
                with self.subTest(pix_fmt=pix_fmt, intra=intra):
                    name = f'{pix_fmt}-intra' if intra else pix_fmt
                    source = Path(directory) / f'{name}-source.mov'
                    output = Path(directory) / f'{name}-output.mov'
                    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                                    'testsrc2=size=64x64:rate=24', '-frames:v', '2',
                                    '-pix_fmt', pix_fmt, '-c:v', 'libx264',
                                    '-profile:v', ffmpeg_profile, '-level:v', '3.0',
                                    '-g', '1' if intra else '12', str(source)], check=True)
                    stream = next(x for x in s.probe(source)['streams'] if x['codec_type'] == 'video')
                    self.assertEqual(stream['pix_fmt'], pix_fmt)
                    frames = [self.frame(64, 64) for _ in range(2)]
                    s.render(source, output, frames, np.ones(2), stream, [], '24/1',
                             64, 64, None, 'medium', False)
                    encoded = next(x for x in s.probe(output)['streams'] if x['codec_type'] == 'video')
                    self.assertEqual((encoded['pix_fmt'], encoded['profile'], encoded['level']),
                                     (pix_fmt, profile, 30))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is unavailable')
    def test_hevc_422_ffmpeg_render_preserves_codec_and_sample_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.mov'
            output = Path(directory) / 'output.mov'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=64x64:rate=24', '-frames:v', '2',
                            '-pix_fmt', 'yuv422p10le', '-c:v', 'libx265',
                            '-profile:v', 'main422-10', '-tag:v', 'hvc1',
                            '-x265-params', 'pools=none:frame-threads=1:log-level=error:'
                                            'level-idc=5.1:colorprim=bt709:transfer=bt709:'
                                            'colormatrix=bt709:range=limited',
                            '-color_primaries', 'bt709', '-color_trc', 'bt709',
                            '-colorspace', 'bt709', str(source)], check=True)
            stream = next(x for x in s.probe(source)['streams'] if x['codec_type'] == 'video')
            self.assertEqual((stream['codec_name'], stream['profile'], stream['pix_fmt'], stream['level']),
                             ('hevc', 'Rext', 'yuv422p10le', 153))
            stream['bit_rate'] = '200000000'  # typical high-bitrate camera setting
            frames = [self.frame(64, 64) for _ in range(2)]
            s.render(source, output, frames, np.ones(2), stream, [], '24/1',
                     64, 64, None, 'ultrafast', False)
            encoded = next(x for x in s.probe(output)['streams'] if x['codec_type'] == 'video')
            for key in ('codec_name', 'profile', 'pix_fmt', 'level', 'codec_tag_string',
                        'color_primaries', 'color_transfer', 'color_space', 'color_range'):
                self.assertEqual(encoded[key], stream[key], key)

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

    def test_ibis_tables_validate_lengths_and_signed_samples(self):
        tags = {0xe40f: struct.pack('>IIiiii', 1, 16, -25, -78000, 181000, 0)}
        np.testing.assert_array_equal(motion_table(tags, 0xe40f, 16, '>iiii'),
                                      [[-25, -78000, 181000, 0]])
        tags[0xe40f] = tags[0xe40f][:-1]
        with self.assertRaisesRegex(ValueError, 'motion table'):
            motion_table(tags, 0xe40f, 16, '>iiii')

    def test_ibis_shift_and_roll_are_applied_at_source_row(self):
        frame = self.frame()
        rows = np.zeros((s.ROW_NODES, 3))
        rows[:, 0] = np.linspace(0, 8, s.ROW_NODES)
        rows[:, 1] = -3
        frame.ibis = rows
        p = frame.map(np.array([[80., 0.], [80., 96.]]), 1)
        self.assertGreater(p[1, 0] - p[0, 0], 7.5)
        self.assertTrue(np.all(p[:, 1] < np.array([0., 96.])))
        baseline = frame.map(np.array([[120., 48.]]), 1)[0, 1]
        rows[:, 2] = np.radians(1)
        self.assertGreater(frame.map(np.array([[120., 48.]]), 1)[0, 1], baseline)

    def test_a7iv_example_has_usable_ibis_samples(self):
        clip = Path(__file__).parent / 'example' / 'C1394.MP4'
        if not clip.exists():
            self.skipTest('A7 IV camera sample is not installed')
        telemetry = load_telemetry(clip, 2, 1920, 1080)
        self.assertEqual(len(telemetry.pts), 420)
        self.assertEqual(len(telemetry.ibis_times), 1680)
        self.assertTrue(np.all(np.diff(telemetry.ibis_times) > 0))
        self.assertTrue(np.all(np.isfinite(telemetry.ibis)))
        self.assertAlmostEqual(telemetry.ibis_times[0], 47 / 1_000_000)
        np.testing.assert_allclose(telemetry.ibis[0],
                                  [78000 / 5120 * 1920 / 6140,
                                   181000 / 5120 * 1080 / 3454,
                                   -np.deg2rad(812 / 1000)])

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
