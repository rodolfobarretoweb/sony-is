# Sony gyro stabilization

Python stabilization for original Sony XAVC MP4s with an `rtmd` metadata track. Tested with the included **ZV-E10, 3840×2160, 24000/1001 fps** recording.

The `C0033M01.XML` sidecar identifies the camera, duration, and presence of gyro data. It **does not contain X/Y motion samples**. Those live inside `C0033.MP4`. Gyro values describe angular velocity, not pixel coordinates: this script converts them to camera orientation, then projects pixels using the lens and sensor geometry.

## Desktop app

The native desktop window lets you choose the camera original, its **Sony XML sidecar**, **Light**, **Balanced**, or **Strong** stabilization, and where to save the result. It shows processing progress and errors. The XML field fills automatically when the matching sidecar is beside the video, or you can browse for it elsewhere; it is optional because the gyro samples are in the original video. The app defaults to a new MOV file beside the original, **Balanced** stabilization, and visual refinement. The renderer preserves the source dimensions, targets its video bitrate, and copies its audio. Existing files are never replaced.

The installable packages bundle Python, the stabilization libraries, FFmpeg, and ffprobe. No browser or separate Python installation is needed on the destination computer.

- **macOS:** build `Sony-Video-Stabilizer-macos-<architecture>.dmg` with `./build-macos.sh`, open the DMG, and drag **Sony Video Stabilizer.app** to Applications. When disk-image creation is unavailable, the script produces a ZIP containing the same `.app`; extract it and move the app to Applications. Build separately on Apple Silicon and Intel if both architectures are needed.
- **Windows (64-bit):** build `Sony-Video-Stabilizer-Setup.exe` with `./build-windows.ps1`, then run the installer. It creates a Start menu entry and can create a desktop shortcut.

To build a package, use Python **3.13 or 3.14** on macOS or Python **3.13** on Windows, plus [FFmpeg](https://ffmpeg.org/download.html) and ffprobe on the build machine. The macOS script detects a Tk-enabled python.org installation if present; with Homebrew Python it installs the matching `python-tk` formula when needed. Set `PYTHON_BIN=/path/to/python` to choose a different interpreter. The macOS build also uses `hdiutil`; the Windows build uses [Inno Setup 6](https://jrsoftware.org/isinfo.php). Build on each operating system: PyInstaller cannot cross-compile. The DMG and Windows installer produced by these scripts are unsigned; broad distribution requires platform signing and, on macOS, notarization.

The included [desktop build workflow](.github/workflows/build-desktop.yml) can create separate Apple Silicon, Intel Mac, and Windows installer artifacts when this project is hosted on GitHub and the workflow is run manually.

For development from source, install `requirements.txt` and run `python app.py` using a Python that includes Tk. With Homebrew Python 3.14, install `python-tk@3.14` first. The command-line stabilizer remains available below.

## Command line

Requires Python 3.10+, FFmpeg and ffprobe on your PATH.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python stabilize.py example/C0033.MP4 --refine
```

The matching XML is detected automatically. The default output is `example/C0033_stabilized_v2.mov`. MOV keeps the original PCM audio packaging for playback compatibility. Existing files are protected; choose `-o another.mov` or explicitly use `--overwrite` to replace an export. The original cannot be used as the output.

By default, encoding targets the **original video stream's bitrate** (about 98 Mbps for this clip), and **copies every original audio track without re-encoding**. There is no default CRF compression or AAC conversion. Stabilization requires decoding, warping, and re-encoding the video; it cannot leave the video bitstream unchanged. The original H.264 codec, 4K dimensions, frame rate, pixel format, color tags, and timecode are retained. Average bitrate can vary slightly with encoder rate control; the JSON report records both the source and measured output bitrate. The original gyro track is not copied onto altered images.

Encoding now also preserves the original **H.264 profile and level**, keeps the bitrate buffer within that level's limits, and inserts a closed keyframe every half-second. The first source-bitrate exports accidentally selected Level 6.1 instead of the camera's Level 5.1 because their buffer setting was oversized. Software decoding succeeded, but that alone did not establish compatibility with hardware playback. The corrected exports use High Level 5.1, two references, one B-frame, and no B-pyramid. They retain the source bitrate and original audio. MOV packaging avoids the newer `ipcm` audio sample entry introduced in the previous exports.

For less crop, choose a stabilization level:

```sh
.venv/bin/python stabilize.py example/C0033.MP4 --refine --level balanced
.venv/bin/python stabilize.py example/C0033.MP4 --refine --level light
```

| Level | Maximum total crop | Crop at each edge | Smoothing | Default output suffix |
| --- | --- | --- | --- | --- |
| `light` | 6% | 3% | 0.25 s | `_stabilized_light.mov` |
| `balanced` | 10% | 5% | 0.35 s | `_stabilized_balanced.mov` |
| `strong` (default) | 20% | 10% | 0.50 s | `_stabilized_v2.mov` |

Start with `balanced` to keep more of the original composition. `light` preserves the most field of view but leaves more camera movement. All levels ease correction when the crop budget is exhausted and keep a fixed zoom by default. The crop limit is a ceiling; clips with less movement may need less crop. Resolution stays at 4K unless `--width` is set: the retained picture is enlarged to fill the output.

For a custom limit, numeric options override the selected level. For example, this keeps balanced smoothing but allows at most 8% total crop:

```sh
.venv/bin/python stabilize.py example/C0033.MP4 --refine --level balanced --max-crop 0.08 -o example/C0033_crop8.mov
```

For another clip:

```sh
.venv/bin/python stabilize.py /path/CLIP.MP4 --refine -o /path/CLIP_smooth.mov
```

A faster, smaller preview:

```sh
.venv/bin/python stabilize.py example/C0033.MP4 --refine --width 1280 -o example/preview.mov
```

Analyze without rendering, and save diagnostics:

```sh
.venv/bin/python stabilize.py example/C0033.MP4 --refine --analyze-only --report analysis.json
```

## Controls

- `--level light|balanced|strong`: choose the smoothing and crop budget together. `strong` retains the previous default behavior. This does not automatically enable visual refinement; use `--refine` as in the examples above.
- `--smooth 0.5`: override the level's path smoothing in seconds. Increase to `0.8` for a more floating look; decrease to `0.3` to follow pans more closely. Larger values can require more crop or reduced correction on fast moves.
- `--max-crop 0.20`: override the level's crop limit. This means at most 20% of the total width/height removed, equivalent to 10% at each edge for a centered crop. The old script mislabeled total crop as “per side.” Use `0.08` for at most 8% total crop.
- `--refine`: remove small remaining image translations after gyro correction. Useful for this handheld clip. Tracking requires forward/backward agreement and background consensus; roll is never accumulated from visual estimates. Omit it for gyro-only stabilization or scenes dominated by independently moving subjects.
- Fixed zoom is the default. `--dynamic-zoom` slowly varies the zoom to recover field of view, at the cost of some visible reframing.
- `--max-frames 240`: process only the first 240 frames, including trimming the audio. Automatic sync needs at least 30 reliable frame pairs.
- Omit `--crf` to target the source video bitrate. `--crf 18` explicitly opts into a different compression mode and may produce a much smaller file; it is no longer the default. `--preset slow` trades encoding time for compression efficiency.
- `--offset-ms 0`: skip optical sync and use recorded metadata timing. Any supplied number is an **additional** offset in milliseconds, not the old script's frame offset. Normally leave this unset.
- `--readout-ms 0`: disable rolling-shutter correction. Normally leave this unset to use the recorded readout time.
- `--focal-px`: override the lens-derived focal length in source-resolution pixels. Normally unnecessary.

Audio is always copied, including the camera's original PCM audio. MOV is now the default container: it preserves this camera's `twos` PCM format tag, while FFmpeg's MP4 muxer changes it to `ipcm`. A successful software decode did not establish hardware/player compatibility for that newer packaging. The script rejects MP4 output with PCM audio and asks for MOV instead; it does not convert the audio to AAC. Explicit MP4 output remains available for sources whose audio is already compatible, such as AAC. Full exports retain the complete audio; explicit `--max-frames` previews trim it to the selected duration. This renderer accepts only 8-bit YUV420 H.264 originals to avoid silently changing the codec or bit depth of other footage.

## What changed

- Decode the gyro's frequency, signed per-packet time offset, units, scale, and axis orientation instead of evenly distributing samples across each frame.
- Use the recorded exposure timing and sensor readout, with a small image-based synchronization adjustment. Read focal geometry from the lens and sensor metadata; constrain any fitted scale adjustment.
- Integrate actual time intervals, with trapezoidal angular rates.
- Fix mesh interpolation: resizing an absolute-coordinate mesh shifted its origin by half a grid cell. The new mesh interpolates displacement at the correct pixel coordinates.
- Limit gyro and rolling-shutter correction together, with a smooth conservative envelope. A failed crop fit can no longer silently reset zoom to 1 or exceed the requested crop.
- Use a fixed crop to eliminate zoom pulsing. Optional visual cleanup only corrects small, confidently tracked translations, with smoothly limited strength.
- Decode and write frames sequentially, handle short pipe reads, reject truncated frames, check FFmpeg exit status, and verify encoded frame count. Failed renders do not replace a completed output.
- Stream decoded frames instead of loading an entire clip into RAM.

The previous Python script is preserved at `tmp/stabilize_previous.py`; the previous video export is untouched.

## Included results

- `example/C0033_balanced_compatible.mov`: corrected 4K High Level 5.1 export targeting the original bitrate, with copied PCM audio and 10% total crop.
- `example/C0033_light_compatible.mov`: corrected 4K High Level 5.1 export targeting the original bitrate, with copied PCM audio and 6% total crop.
- The earlier `*_source_bitrate.mp4` files have the Level 6.1 compatibility issue and are superseded by `*_compatible.mov`.
- The older `C0033_stabilized_v2.mp4`, `C0033_stabilized_balanced.mp4`, and `C0033_stabilized_light.mp4` exports used CRF 18 and AAC audio. They remain available for comparison; changing the defaults does not modify an existing file.
- `example/C0033_comparison.mp4`: original on the **left**, new result on the **right**.
- `example/C0033_stabilized_v2.json`: timing, crop, tracking and render diagnostics.
- `validation.json`: motion measurements on matching, reliably tracked 640×360 analysis frames, without zoom normalization, with methodology.

The motion measurements are proxies for visible shake, not a guarantee of perceptual quality. Pans, parallax, independent subject movement, and tracking failures affect them. Stabilization cannot remove blur already recorded during an exposure or reconstruct hidden image borders. The rapid movements in this clip therefore still look less clean than its stationary sections.

This version targets original, continuous, constant-frame-rate Sony clips. It rejects active IBIS/OSS tables and does not implement Sony's full lens distortion or electronic stabilization mesh. Edited footage, other camera axis layouts, or in-camera stabilization modes need separate validation. At clip boundaries, orientation sampling clamps to the available gyro interval.

## Verification

```sh
.venv/bin/python -m unittest -v test_stabilize test_app
```

Tests cover identity and homography mapping, scaled pixel centers, gyro direction, irregular sample integration, crop limits during strong motion, smooth correction limits, malformed metadata, short frame reads, and the interface's path safety and processing status.

Metadata references used to check tag definitions and timing conventions:

- [telemetry-parser Sony tags](https://github.com/AdrianEddy/telemetry-parser/blob/master/src/sony/rtmd_tags.rs)
- [Gyroflow Sony metadata timing](https://github.com/gyroflow/gyroflow/blob/master/src/core/gyro_source/sony.rs)

The implementation here retains and revises the existing local geometry code; it does not vendor either reference implementation.
