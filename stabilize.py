#!/usr/bin/env python3
"""Stabilize Sony XAVC using embedded gyro, with optional visual translation cleanup."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter1d, maximum_filter1d, minimum_filter1d
from scipy.spatial.transform import Rotation as Rot
from scipy.optimize import minimize_scalar

from sony_metadata import probe, read_xml, load_telemetry


STABILIZATION_LEVELS = {
    'light': {'smooth': 0.25, 'max_crop': 0.06},
    'balanced': {'smooth': 0.35, 'max_crop': 0.10},
    'strong': {'smooth': 0.50, 'max_crop': 0.20},
}


def level_settings(level, smooth=None, max_crop=None):
    """Presets set the motion/crop tradeoff; explicit numeric options take priority."""
    defaults = STABILIZATION_LEVELS[level]
    return (defaults['smooth'] if smooth is None else smooth,
            defaults['max_crop'] if max_crop is None else max_crop)


def make_sampler(ts, q):
    from scipy.spatial.transform import Slerp
    sl = Slerp(ts, Rot.from_quat(q))
    lo, hi = ts[0], ts[-1]
    return lambda t: sl(np.clip(t, lo, hi))


def smooth_orientations(R, sigma_frames):
    q = R.as_quat()
    for i in range(1, len(q)):                # keep a continuous hemisphere
        if q[i] @ q[i - 1] < 0:
            q[i] = -q[i]
    qs = gaussian_filter1d(q, sigma_frames, axis=0, mode="nearest")
    qs /= np.linalg.norm(qs, axis=1, keepdims=True)
    return Rot.from_quat(qs)


# --------------------------------------------------------- crop / zoom
ROW_NODES = 33


def rodrigues(rv):
    """rv (...,3) rotation vectors -> (...,3,3) matrices."""
    th = np.linalg.norm(rv, axis=-1, keepdims=True)
    k = rv / np.maximum(th, 1e-12)
    th = th[..., None]
    kx = np.zeros(rv.shape[:-1] + (3, 3))
    kx[..., 0, 1], kx[..., 0, 2] = -k[..., 2], k[..., 1]
    kx[..., 1, 0], kx[..., 1, 2] = k[..., 2], -k[..., 0]
    kx[..., 2, 0], kx[..., 2, 1] = -k[..., 1], k[..., 0]
    I = np.eye(3)
    return I + np.sin(th) * kx + (1 - np.cos(th)) * (kx @ kx)


class Frame:
    """Everything needed to map output pixels of one frame to input pixels."""

    def __init__(self, K, Kinv, W, H, D, rows_rv):
        self.K, self.Kinv, self.W, self.H = K, Kinv, W, H
        self.D = D                      # raw -> stabilized correction (3x3)
        self.rv = rows_rv               # (ROW_NODES,3) in-frame orientation drift
        self.vn = np.linspace(0, H, ROW_NODES)
        self.post = (0.0, 0.0, 0.0)     # visual refinement: dx, dy, roll (rad)

    def map(self, P, z, iters=3):
        """P (N,2) output pixels -> (N,2) input pixels."""
        W, H = self.W, self.H
        c = np.array([W / 2, H / 2])
        q = c + (P - c) / z
        dx, dy, th = self.post
        if dx or dy or th:
            ct, st = np.cos(th), np.sin(th)
            qc = q - c
            q = c + np.stack([ct * qc[:, 0] - st * qc[:, 1],
                              st * qc[:, 0] + ct * qc[:, 1]], 1) + [dx, dy]
        x = np.concatenate([q, np.ones((len(q), 1))], 1) @ self.Kinv.T
        a = x @ self.D.T                # rays in the un-drifted raw camera
        v = (a[:, 1] / np.maximum(a[:, 2], 1e-6)) * self.K[1, 1] + self.K[1, 2]
        for _ in range(iters):
            vc = np.clip(v, 0, H)
            rv = np.stack([np.interp(vc, self.vn, self.rv[:, k]) for k in range(3)], 1)
            Rm = rodrigues(-rv)         # E(v)^T
            cvec = np.einsum("nij,nj->ni", Rm, a)
            p = cvec @ self.K.T
            p = p[:, :2] / np.maximum(p[:, 2:3], 1e-6)
            v = p[:, 1]
        ok = a[:, 2] > 1e-6
        p[~ok] = -1e6
        return p


def border_points(w, h, k=24):
    t = np.linspace(0, 1, k)
    top = np.stack([t * w, np.zeros(k)], 1)
    bot = np.stack([t * w, np.full(k, h)], 1)
    lef = np.stack([np.zeros(k), t * h], 1)
    rig = np.stack([np.full(k, w), t * h], 1)
    return np.concatenate([top, bot, lef, rig])


def covers(fr, pts, z, margin=1.0):
    p = fr.map(pts, z)
    return bool((p[:, 0] >= margin).all() and (p[:, 0] <= fr.W - margin).all()
                and (p[:, 1] >= margin).all() and (p[:, 1] <= fr.H - margin).all())


def required_zoom(fr, pts, zmax):
    if covers(fr, pts, 1.0):
        return 1.0
    if not covers(fr, pts, zmax):
        return np.inf
    lo, hi = 1.0, zmax
    for _ in range(16):
        mid = (lo + hi) / 2
        if covers(fr, pts, mid):
            hi = mid
        else:
            lo = mid
    return hi


def row_drift(src, t_c, R_i, readout, H):
    """Orientation of every row-node relative to the frame-centre orientation."""
    vn = np.linspace(0, H, ROW_NODES)
    t = t_c + (vn / H - 0.5) * readout
    return (R_i.inv() * src(t)).as_rotvec()



def integrate(ts, rates):
    """Trapezoidal integration on actual sample times; q[0] is identity."""
    increments = np.radians((rates[:-1] + rates[1:])/2) * np.diff(ts)[:, None]
    q = np.empty((len(ts), 4))
    q[0] = [0, 0, 0, 1]
    current = Rot.identity()
    for i, inc in enumerate(Rot.from_rotvec(increments), 1):
        current = current * inc
        q[i] = current.as_quat()
    return q


def read_exact(stream, count):
    parts, left = [], count
    while left:
        data = stream.read(left)
        if not data:
            break
        parts.append(data)
        left -= len(data)
    result = b''.join(parts)
    if result and len(result) != count:
        raise ValueError(f'Truncated decoded frame ({len(result)} of {count} bytes)')
    return result


def gray_frames(path, width, height, count):
    proc = subprocess.Popen(['ffmpeg', '-v', 'error', '-noautorotate', '-i', str(path),
        '-map', '0:v:0', '-frames:v', str(count), '-vf', f'scale={width}:{height}',
        '-fps_mode', 'passthrough', '-pix_fmt', 'gray', '-f', 'rawvideo', '-'], stdout=subprocess.PIPE)
    completed = False
    try:
        for _ in range(count):
            data = read_exact(proc.stdout, width*height)
            if not data:
                raise ValueError('Video ended before the expected frame count')
            yield np.frombuffer(data, np.uint8).reshape(height, width)
        completed = True
    finally:
        proc.stdout.close()
        if not completed:
            proc.terminate()
        code = proc.wait()
    if code:
        raise RuntimeError(f'Video decoder failed ({code})')


def measure_motion(prev, cur, mask=None):
    """Background motion with forward/backward tracking and spatial consensus."""
    pts = cv2.goodFeaturesToTrack(prev, 600, 0.01, 10, mask=mask)
    if pts is None or len(pts) < 20:
        return np.zeros(3), 0.0
    nxt, status, _ = cv2.calcOpticalFlowPyrLK(prev, cur, pts, None)
    if nxt is None:
        return np.zeros(3), 0.0
    back, status2, _ = cv2.calcOpticalFlowPyrLK(cur, prev, nxt, None)
    if back is None:
        return np.zeros(3), 0.0
    good = ((status[:, 0] == 1) & (status2[:, 0] == 1)
            & (np.linalg.norm(back[:, 0]-pts[:, 0], axis=1) < 0.8))
    h, w = cur.shape
    good &= ((nxt[:, 0, 0] >= 0) & (nxt[:, 0, 0] < w)
             & (nxt[:, 0, 1] >= 0) & (nxt[:, 0, 1] < h))
    a, b = pts[good, 0], nxt[good, 0]
    if len(a) < 20:
        return np.zeros(3), 0.0
    matrix, inliers = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC,
        ransacReprojThreshold=1.0, maxIters=2000, confidence=0.99)
    if matrix is None:
        return np.zeros(3), 0.0
    accepted = inliers[:, 0].astype(bool)
    fraction = accepted.mean()
    scale = np.linalg.norm(matrix[:, 0])
    spread = np.ptp(a[accepted], axis=0) if accepted.any() else [0, 0]
    if fraction < 0.5 or accepted.sum() < 20 or min(spread/[w, h]) < 0.2 or not 0.94 < scale < 1.06:
        return np.zeros(3), 0.0
    center = np.array([w/2, h/2])
    dx, dy = matrix[:, :2] @ center + matrix[:, 2] - center
    angle = np.arctan2(matrix[1, 0], matrix[0, 0])
    return np.array([dx, dy, angle]), float(fraction)


def track_frames(path, count, width, height):
    motion, confidence = np.zeros((count, 3)), np.zeros(count)
    previous = None
    for i, frame in enumerate(gray_frames(path, width, height, count)):
        if previous is not None:
            motion[i], confidence[i] = measure_motion(previous, frame)
        previous = frame
        if i and i % 200 == 0:
            print(f'  tracking {i}/{count}', flush=True)
    return motion, confidence


def calibrate(telemetry, sampler, motion, confidence, scale, fps, count):
    """Refine the recorded sync against image motion; keep physical lens geometry."""
    focal = telemetry.focal[:count] * scale
    centers = telemetry.centers[:count]
    observed = motion[1:, :2]
    valid = (confidence[1:] > 0.5) & (np.linalg.norm(observed, axis=1) < 80)
    # Ignore the last frame, whose exposure may extend beyond recorded IMU data.
    valid[-2:] = False
    if valid.sum() < 30:
        raise ValueError('Too few reliable image tracks for gyro sync; use --offset-ms 0 to use metadata timing')
    sigma = max(1, fps*0.15)
    obs_hp = observed - gaussian_filter1d(observed, sigma, axis=0)

    def fit(offset):
        rotations = sampler(centers + offset)
        delta = (rotations[:-1].inv()*rotations[1:]).as_rotvec()
        predicted = np.stack([-delta[:, 1]*focal[1:, 0], delta[:, 0]*focal[1:, 1]], 1)
        pred_hp = predicted - gaussian_filter1d(predicted, sigma, axis=0)
        x, y = pred_hp[valid], obs_hp[valid]
        factor = np.clip(np.sum(x*y)/max(np.sum(x*x), 1e-9), 0.85, 1.15)
        residual = np.linalg.norm(y-factor*x, axis=1)
        # Trim worst 15% (moving subject, occlusion, parallax); never fit focal from scratch.
        keep = residual <= np.quantile(residual, 0.85)
        loss = np.mean((y[keep]-factor*x[keep])**2)
        r2 = 1 - loss/max(np.mean(y[keep]**2), 1e-9)
        return loss, float(factor), float(r2)

    grid = np.linspace(-0.025, 0.025, 51)
    best = min(grid, key=lambda t: fit(t)[0])
    result = minimize_scalar(lambda t: fit(t)[0], bounds=(best-0.001, best+0.001), method='bounded')
    _, factor, r2 = fit(result.x)
    if r2 < 0.4:
        raise ValueError(f'Gyro/image fit is unreliable (R²={r2:.2f}); refusing to apply a bad warp')
    return float(result.x), factor, r2


def lower_envelope(values, sigma):
    """Smooth values without ever increasing any point's allowed correction."""
    if sigma <= 0:
        return values.copy()
    radius = int(4*sigma + 0.5)
    return gaussian_filter1d(minimum_filter1d(values, 2*radius+1, mode='nearest'),
                             sigma, mode='nearest', truncate=4.0)


def plan(telemetry, sampler, width, height, fps, count, offset, focal_scale,
         smooth, max_crop, readout_ms=None):
    centers = telemetry.centers[:count] + offset
    original = sampler(centers)
    target = smooth_orientations(original, smooth*fps)
    relative = (original.inv()*target).as_rotvec()
    readout = telemetry.readout[:count] if readout_ms is None else np.full(count, readout_ms/1000)
    row_times = centers[:, None] + np.linspace(-0.5, 0.5, ROW_NODES)*readout[:, None]
    drift = (Rot.from_quat(np.repeat(original.as_quat(), ROW_NODES, axis=0)).inv()
             * sampler(row_times.ravel())).as_rotvec().reshape(count, ROW_NODES, 3)
    border = border_points(width-1, height-1, 48)
    zmax = 1/(1-max_crop)
    matrices = []
    for fx, fy in telemetry.focal[:count]*focal_scale:
        matrices.append(np.array([[fx, 0, width/2], [0, fy, height/2], [0, 0, 1]]))
    def build(i, strength):
        k = matrices[i]
        return Frame(k, np.linalg.inv(k), width, height,
                     Rot.from_rotvec(relative[i]*strength).as_matrix(), drift[i]*strength)
    limits = np.ones(count)
    for i in range(count):
        if not covers(build(i, 1), border, zmax, 3):
            lo, hi = 0.0, 1.0
            for _ in range(16):
                mid = (lo+hi)/2
                if covers(build(i, mid), border, zmax, 3):
                    lo = mid
                else:
                    hi = mid
            limits[i] = lo
    strength = lower_envelope(limits, max(1, fps*0.12))
    frames = [build(i, strength[i]) for i in range(count)]
    zoom = []
    for fr in frames:
        lo, hi = 1.0, zmax
        for _ in range(16):
            mid = (lo+hi)/2
            if covers(fr, border, mid, 3):
                hi = mid
            else:
                lo = mid
        if not covers(fr, border, hi, 3):
            raise ValueError('Crop planning failed; no valid mapping at the crop limit')
        zoom.append(hi)
    return frames, np.array(zoom), strength


def dense_map(frame, zoom, width, height):
    """Interpolate a displacement mesh, preserving exact pixel coordinates.

    Resizing an absolute-coordinate grid with cv2.resize shifts its origin by
    half a cell. Interpolate at x/step,y/step instead, including the final node.
    """
    step = 16
    xs = np.arange(0, frame.W+step, step, dtype=float)
    ys = np.arange(0, frame.H+step, step, dtype=float)
    gx, gy = np.meshgrid(xs, ys)
    points = np.stack([gx.ravel(), gy.ravel()], 1)
    mapped = frame.map(points, zoom)
    # Pixel centers when scaling the output, not arbitrary crop/slice offsets.
    x = (np.arange(width, dtype=np.float32)+0.5)*frame.W/width-0.5
    y = (np.arange(height, dtype=np.float32)+0.5)*frame.H/height-0.5
    ix, iy = np.meshgrid(x/step, y/step)
    # cv2.remap quantizes interpolation coordinates to 1/32; interpolate displacements
    # so that its grid quantization cannot introduce a sawtooth in an identity mapping.
    displacement = mapped-points
    dx = cv2.remap(displacement[:, 0].reshape(gx.shape).astype(np.float32), ix, iy,
                   cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    dy = cv2.remap(displacement[:, 1].reshape(gx.shape).astype(np.float32), ix, iy,
                   cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return dx+x[None, :], dy+y[:, None]


def visual_refine(path, frames, width, height, fps, smooth, max_crop):
    lw = min(960, width)
    lh = round(height*lw/width/2)*2
    motion = np.zeros((len(frames), 3))
    confidence = np.zeros(len(frames))
    prev, prev_valid = None, None
    for i, raw in enumerate(gray_frames(path, lw, lh, len(frames))):
        mx, my = dense_map(frames[i], 1, lw, lh)
        mx = (mx+0.5)*lw/width-0.5
        my = (my+0.5)*lh/height-0.5
        valid = ((mx > 12) & (mx < lw-13) & (my > 12) & (my < lh-13))
        current = cv2.remap(raw, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        if prev is not None:
            mask = (valid & prev_valid).astype(np.uint8)*255
            flow, conf = measure_motion(prev, current, mask)
            # Refine only small residual translations. Never chase large pans or a new subject.
            if conf > 0.6 and np.linalg.norm(flow[:2]) < lw*0.035:
                motion[i, :2] = flow[:2]*[width/lw, height/lh]
                confidence[i] = conf
        prev, prev_valid = current, valid
        if i and i % 200 == 0:
            print(f'  visual cleanup {i}/{len(frames)}', flush=True)
    # Missing tracks carry no new displacement. Smoothing is local; no unbounded roll integration.
    path_xy = np.cumsum(motion[:, :2], axis=0)
    correction = path_xy-gaussian_filter1d(path_xy, smooth*fps, axis=0, mode='nearest')
    correction = np.tanh(correction/(0.025*width))*(0.025*width)
    border = border_points(width-1, height-1, 48)
    zmax = 1/(1-max_crop)
    limits = np.ones(len(frames))
    for i, fr in enumerate(frames):
        lo, hi = 0.0, 1.0
        fr.post = (*correction[i], 0)
        if not covers(fr, border, zmax, 3):
            for _ in range(14):
                mid = (lo+hi)/2
                fr.post = (*(correction[i]*mid), 0)
                if covers(fr, border, zmax, 3):
                    lo = mid
                else:
                    hi = mid
            limits[i] = lo
    gain = lower_envelope(limits, max(1, fps*0.12))
    for i, fr in enumerate(frames):
        fr.post = (*(correction[i]*gain[i]), 0)
    return {'accepted_pairs': int(np.count_nonzero(confidence)), 'pairs': len(frames)-1,
            'translation_rms_px': float(np.sqrt(np.mean((correction*gain[:, None])**2)))}


def encoding_rate(stream, crf=None):
    """Use the video's own average bitrate, excluding audio/container overhead."""
    if crf is not None:
        return ['-crf', str(crf)]
    bitrate = int(stream.get('bit_rate') or 0)
    if bitrate <= 0:
        raise ValueError('Source video bitrate is unavailable; refusing to guess a lower bitrate')
    # Keep VBV within the source H.264 level, rather than letting an oversized
    # buffer auto-select Level 6.1 for a Level 5.1 camera original.
    limits = {30: (10000, 10000), 31: (14000, 14000), 32: (20000, 20000),
              40: (20000, 25000), 41: (50000, 62500), 42: (50000, 62500),
              50: (135000, 135000), 51: (240000, 240000), 52: (240000, 240000),
              60: (240000, 240000), 61: (480000, 480000), 62: (800000, 800000)}
    level = int(stream.get('level', 51))
    if level not in limits:
        raise ValueError(f'Unsupported H.264 level for bitrate preservation: {level}')
    factor = 1250 if stream.get('profile', 'High') == 'High' else 1000
    max_rate, max_buffer = (x*factor for x in limits[level])
    if bitrate > max_rate:
        raise ValueError('Source bitrate exceeds its declared H.264 level')
    return ['-b:v', str(bitrate), '-maxrate', str(min(round(bitrate*1.5), max_rate)),
            '-bufsize', str(min(bitrate*2, max_buffer))]


def compatibility_options(stream, fps_text):
    profiles = {'High': 'high', 'Main': 'main', 'Baseline': 'baseline',
                'Constrained Baseline': 'baseline'}
    profile = profiles.get(stream.get('profile'))
    level = int(stream.get('level') or 0)
    if profile is None or level < 10:
        raise ValueError('Missing or unsupported source H.264 profile/level')
    gop = max(1, round(float(eval_fraction(fps_text))/2))
    return ['-profile:v', profile, '-level:v', f'{level//10}.{level%10}',
            '-refs', '2', '-bf', '0' if profile == 'baseline' else '1',
            '-b-pyramid', 'none', '-g', str(gop), '-keyint_min', str(gop), '-sc_threshold', '0']


def validate_audio_container(out, audio):
    if Path(out).suffix.lower() == '.mp4' and any(a['codec_name'].startswith('pcm_') for a in audio):
        raise ValueError('Use a .mov output to preserve original PCM audio with compatible packaging; '
                         'MP4 would change its format tag to ipcm')


def render(path, out, frames, zoom, stream, audio, fps_text, width, height, crf, preset, overwrite):
    if Path(out).exists() and not overwrite:
        raise ValueError(f'Output exists: {out}; choose another path or pass --overwrite')
    source_w, source_h = int(stream['width']), int(stream['height'])
    validate_audio_container(out, audio)
    rate_options = encoding_rate(stream, crf)
    compatibility = compatibility_options(stream, fps_text)
    # Encode to a sibling temporary file; a failed run never replaces a valid export.
    fd, temporary = tempfile.mkstemp(prefix='.stabilizing-', suffix=Path(out).suffix, dir=Path(out).parent)
    os.close(fd)
    cmd = ['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
           '-s', f'{width}x{height}', '-framerate', fps_text, '-i', '-', '-i', str(path),
           '-map', '0:v:0']
    for i in range(len(audio)):
        cmd += ['-map', f'1:a:{i}']
    cmd += ['-map_metadata', '1', '-c:v', 'libx264', '-preset', preset,
            '-pix_fmt', 'yuv420p'] + rate_options + compatibility
    for key, option in [('color_space', '-colorspace'), ('color_transfer', '-color_trc'),
                        ('color_primaries', '-color_primaries'), ('color_range', '-color_range')]:
        if stream.get(key) not in (None, 'unknown', 'unspecified'):
            cmd += [option, stream[key]]
    # Some FFmpeg/libx264 builds let untagged raw-frame properties override codec
    # options. Set the H.264 VUI explicitly as well as the container color tags.
    vui = []
    for key, option in [('color_space', 'colormatrix'), ('color_transfer', 'transfer'),
                        ('color_primaries', 'colorprim'), ('color_range', 'range')]:
        if stream.get(key) not in (None, 'unknown', 'unspecified'):
            vui.append(f'{option}={stream[key]}')
    if vui:
        cmd += ['-x264-params', ':'.join(vui)]
    # Preserve encoded audio samples; never silently fall back to transcoding.
    # MOV preserves this Sony camera's original 'twos' PCM packaging. MP4
    # would instead use the newer 'ipcm' sample entry, so it is rejected above.
    cmd += ['-c:a', 'copy']
    if stream.get('tags', {}).get('timecode'):
        cmd += ['-timecode', stream['tags']['timecode']]
    if len(frames) < int(stream['nb_frames']):
        duration = len(frames)/float(eval_fraction(fps_text))
        cmd += ['-t', f'{duration:.9f}']
    cmd += ['-movflags', '+faststart', temporary]
    dec = enc = None
    success = False
    try:
        dec = subprocess.Popen(['ffmpeg', '-v', 'error', '-noautorotate', '-i', str(path),
            '-map', '0:v:0', '-frames:v', str(len(frames)), '-fps_mode', 'passthrough',
            '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-'], stdout=subprocess.PIPE)
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        ysize, csize = source_w*source_h, source_w*source_h//4
        for i, fr in enumerate(frames):
            data = read_exact(dec.stdout, ysize+2*csize)
            if not data:
                raise ValueError(f'Decoder stopped at frame {i}; expected {len(frames)}')
            mx, my = dense_map(fr, zoom[i], width, height)
            y = np.frombuffer(data, np.uint8, ysize).reshape(source_h, source_w)
            u = np.frombuffer(data, np.uint8, csize, ysize).reshape(source_h//2, source_w//2)
            v = np.frombuffer(data, np.uint8, csize, ysize+csize).reshape(source_h//2, source_w//2)
            # Left-sited 4:2:0 chroma: x=2u,y=2v+0.5 on the luma grid.
            cmx = mx[0::2, 0::2]/2
            cmy = (my[0::2, 0::2]+my[1::2, 0::2])/4-0.25
            for plane, xmap, ymap in [(y, mx, my), (u, cmx, cmy), (v, cmx, cmy)]:
                output = cv2.remap(plane, xmap, ymap, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
                enc.stdin.write(output.tobytes())
            if i % 50 == 0:
                print(f'  rendering {i}/{len(frames)}', flush=True)
        dec.stdout.close()
        enc.stdin.close()
        if dec.wait() != 0 or enc.wait() != 0:
            raise RuntimeError('FFmpeg decode or encode failed')
        check = probe(temporary)
        video = next(s for s in check['streams'] if s['codec_type'] == 'video')
        if int(video.get('nb_frames', 0)) != len(frames):
            raise RuntimeError('Encoded frame count does not match input')
        if video.get('level') != stream.get('level') or video.get('profile') != stream.get('profile'):
            raise RuntimeError('Encoder changed the source H.264 profile or level')
        output_audio = [s for s in check['streams'] if s['codec_type'] == 'audio']
        if len(output_audio) != len(audio):
            raise RuntimeError('Output audio track count does not match input')
        for original, copied in zip(audio, output_audio):
            for key in ('codec_name', 'sample_rate', 'channels', 'bits_per_sample'):
                if original.get(key) != copied.get(key):
                    raise RuntimeError(f'Audio {key} was changed')
        for key in ('color_space', 'color_transfer', 'color_primaries', 'color_range'):
            if stream.get(key) not in (None, 'unknown', 'unspecified') and video.get(key) != stream[key]:
                raise RuntimeError(f'Encoder did not preserve {key}: {stream[key]}')
        os.replace(temporary, out)
        success = True
        return {'mode': 'source_bitrate' if crf is None else 'crf',
                'source_video_bitrate': int(stream.get('bit_rate') or 0),
                'output_video_bitrate': int(video.get('bit_rate') or 0),
                'h264_profile': video['profile'], 'h264_level': video['level'],
                'audio': 'stream_copy', 'output_size_bytes': int(check['format']['size'])}
    finally:
        for process in (dec, enc):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait()
        if not success:
            Path(temporary).unlink(missing_ok=True)


def eval_fraction(value):
    from fractions import Fraction
    return Fraction(value)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('video', type=Path)
    ap.add_argument('-x', '--xml', type=Path)
    ap.add_argument('-o', '--output', type=Path)
    ap.add_argument('--level', choices=STABILIZATION_LEVELS, default='strong',
                    help='light: up to 6%% crop; balanced: 10%%; strong: 20%% (default, previous behavior)')
    ap.add_argument('--smooth', type=float, help='Override the level: path smoothing in seconds')
    ap.add_argument('--max-crop', type=float,
                    help='Override the level: maximum total width/height removed, not per edge (e.g. 0.08)')
    ap.add_argument('--readout-ms', type=float, help='Override the recorded sensor readout (0 disables)')
    ap.add_argument('--offset-ms', type=float, help='Additional gyro sync offset; skips automatic sync')
    ap.add_argument('--focal-px', type=float, help='Override the recorded focal length in source pixels')
    ap.add_argument('--refine', action='store_true', help='Also remove small visual translation residuals')
    ap.add_argument('--dynamic-zoom', action='store_true', help='Recover field of view with slowly varying crop')
    ap.add_argument('--max-frames', type=int, help='Analyze and render only the first N frames')
    ap.add_argument('--width', type=int, help='Output width for a smaller preview (default: original)')
    ap.add_argument('--crf', type=float,
                    help='Explicitly opt into CRF compression; default preserves the source video bitrate')
    ap.add_argument('--preset', default='medium', choices=['fast', 'medium', 'slow'])
    ap.add_argument('--analyze-only', action='store_true')
    ap.add_argument('--report', type=Path, help='Write JSON diagnostics')
    ap.add_argument('--overwrite', action='store_true')
    args = ap.parse_args(argv)
    args.smooth, args.max_crop = level_settings(args.level, args.smooth, args.max_crop)
    if not 0 < args.smooth <= 5 or not 0.01 <= args.max_crop <= 0.5:
        ap.error('--smooth must be in (0,5]; --max-crop must be in [0.01,0.5]')
    if args.max_frames is not None and args.max_frames < 3:
        ap.error('--max-frames must be at least 3')
    for name in ('smooth', 'max_crop', 'readout_ms', 'offset_ms', 'focal_px', 'crf'):
        value = getattr(args, name)
        if value is not None and not np.isfinite(value):
            ap.error(f'--{name.replace("_", "-")} must be finite')
    if args.focal_px is not None and args.focal_px <= 0:
        ap.error('--focal-px must be positive')
    if args.readout_ms is not None and not 0 <= args.readout_ms <= 100:
        ap.error('--readout-ms must be in [0,100]')
    if args.crf is not None and not 0 <= args.crf <= 51:
        ap.error('--crf must be in [0,51]')
    suffix = 'v2' if args.level == 'strong' else args.level
    out = args.output or args.video.with_name(args.video.stem+f'_stabilized_{suffix}.mov')
    if out.resolve() == args.video.resolve():
        ap.error('Output must differ from the original video')
    if args.report and args.report.resolve() in {args.video.resolve(), out.resolve()}:
        ap.error('--report must differ from both the video and the output')
    if out.suffix.lower() not in ('.mp4', '.mov'):
        ap.error('Output must be .mp4 or .mov')
    if out.exists() and not args.overwrite and not args.analyze_only:
        ap.error(f'Output exists: {out}; use another filename or --overwrite')
    xml = args.xml or next((p for p in [args.video.with_name(args.video.stem+'M01.XML'),
                                       args.video.with_name(args.video.stem+'M01.xml')] if p.exists()), None)
    sidecar = read_xml(xml)
    info = probe(args.video)
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    audio = [s for s in info['streams'] if s['codec_type'] == 'audio']
    validate_audio_container(out, audio)
    data = next((s for s in info['streams'] if s.get('codec_tag_string') == 'rtmd'), None)
    if data is None:
        raise ValueError('No Sony rtmd track. The XML alone does not contain gyro samples; use the camera original')
    if data.get('tags', {}).get('timecode'):
        video.setdefault('tags', {})['timecode'] = data['tags']['timecode']
    if video['pix_fmt'] != 'yuv420p':
        raise ValueError('This renderer supports 8-bit YUV420 Sony originals only; refusing a silent bit-depth conversion')
    if video['codec_name'] != 'h264':
        raise ValueError('This renderer preserves H.264 sources only; refusing a silent codec change')
    encoding_rate(video, args.crf)
    width, height = int(video['width']), int(video['height'])
    fps_text = video['avg_frame_rate']
    fps = float(eval_fraction(fps_text))
    count = int(video['nb_frames'])
    if sidecar:
        if sidecar['frames'] != count:
            raise ValueError('XML duration does not match this video; use the matching sidecar')
        print(f"XML: {sidecar['model']}, {count} frames, gyro declared: {sidecar['gyro']}", flush=True)
    if args.max_frames:
        count = min(count, args.max_frames)
    print(f'Video: {width}x{height}, {fps:.6f} fps, processing {count} frames', flush=True)
    print(f'Level: {args.level}; smoothing {args.smooth:g}s; '
          f'crop limit {args.max_crop*100:g}% total ({args.max_crop*50:g}% each edge)', flush=True)
    print((f'Encoding: source video bitrate {int(video["bit_rate"])/1e6:.3f} Mbps'
           if args.crf is None else f'Encoding: explicitly selected CRF {args.crf:g}')
          + '; original audio copied', flush=True)
    telemetry = load_telemetry(args.video, data['index'], width, height)
    if len(telemetry.pts) < count or not np.allclose(np.diff(telemetry.pts), 1/fps, atol=2e-6):
        raise ValueError('Requires continuous constant-frame-rate camera metadata')
    if args.focal_px:
        telemetry.focal[:] = args.focal_px
    sample = make_sampler(telemetry.times, integrate(telemetry.times, telemetry.rates))
    print(f'Gyro: {len(telemetry.times)} samples; recorded rate {telemetry.frequency:g} Hz; '
          f'readout {np.median(telemetry.readout)*1000:.3f} ms', flush=True)
    factor, r2 = 1.0, None
    if args.offset_ms is None:
        print('Checking gyro sync against reliable image tracks...', flush=True)
        lw = min(width, 640)
        lh = round(height*lw/width/2)*2
        motion, confidence = track_frames(args.video, count, lw, lh)
        offset, factor, r2 = calibrate(telemetry, sample, motion, confidence, lw/width, fps, count)
        print(f'  sync adjustment {offset*1000:+.2f} ms; focal scale {factor:.3f}; fit R²={r2:.3f}', flush=True)
    else:
        offset = args.offset_ms/1000
    frames, zoom, strength = plan(telemetry, sample, width, height, fps, count, offset, factor,
                                  args.smooth, args.max_crop, args.readout_ms)
    refinement = None
    if args.refine:
        print('Removing small residual image translations...', flush=True)
        refinement = visual_refine(args.video, frames, width, height, fps, args.smooth, args.max_crop)
        # Recompute zoom after the final, smoothly limited correction, with the same margin.
        border = border_points(width-1, height-1, 48)
        for i, fr in enumerate(frames):
            lo, hi = 1.0, 1/(1-args.max_crop)
            for _ in range(16):
                mid = (lo+hi)/2
                if covers(fr, border, mid, 3):
                    hi = mid
                else:
                    lo = mid
            if not covers(fr, border, hi, 3):
                raise ValueError('Refinement exceeded the crop budget')
            zoom[i] = hi
    if args.dynamic_zoom:
        sigma = fps*0.75
        upper = maximum_filter1d(zoom, 2*int(4*sigma+0.5)+1, mode='nearest')
        zoom = gaussian_filter1d(upper, sigma, mode='nearest')
    else:
        zoom[:] = np.max(zoom)
    report = {'input': str(args.video), 'output': str(out), 'frames': count, 'fps': fps,
              'level': args.level, 'smooth_seconds': args.smooth, 'max_crop': args.max_crop,
              'gyro_samples': len(telemetry.times), 'gyro_frequency_hz': telemetry.frequency,
              'sensor_readout_ms': float(np.median(telemetry.readout)*1000),
              'sync_adjustment_ms': offset*1000, 'focal_scale': factor, 'sync_r2': r2,
              'focal_px': (np.median(telemetry.focal, axis=0)*factor).tolist(),
              'crop_total_fraction': float(np.max(1-1/zoom)),
              'min_strength': float(strength.min()), 'mean_strength': float(strength.mean()),
              'refinement': refinement, 'rendered': False}
    print(f'Crop: {report["crop_total_fraction"]*100:.1f}% total dimension '
          f'({report["crop_total_fraction"]*50:.1f}% each edge); mean strength {strength.mean():.2f}', flush=True)
    if not args.analyze_only:
        ow = args.width or width
        if ow < 64 or ow > width or ow % 2:
            ap.error('--width must be even, at least 64, and no greater than the source width')
        oh = round(height*ow/width/2)*2
        report['encoding'] = render(args.video, out, frames, zoom, video, audio, fps_text,
                                    ow, oh, args.crf, args.preset, args.overwrite)
        report['rendered'] = True
        print(f'Done: {out} ({count} frames verified)', flush=True)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
