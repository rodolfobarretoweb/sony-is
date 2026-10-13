"""Sony XAVC rtmd decoding. Tag definitions cross-checked against telemetry-parser.
https://github.com/AdrianEddy/telemetry-parser/blob/master/src/sony/rtmd_tags.rs
All times are seconds, rates are degrees/second, camera axes right/down/forward.
"""
import json
import struct
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import numpy as np


def probe(path):
    return json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))


def read_xml(path):
    if path is None:
        return None
    root = ET.parse(path).getroot()
    nodes = {el.tag.rsplit('}', 1)[-1]: el for el in root.iter()}
    return {
        'frames': int(nodes['Duration'].get('value')),
        'model': nodes.get('Device', ET.Element('Device')).get('modelName'),
        'gyro': any(el.get('name') == 'Gyroscope' for el in root.iter()),
    }


def parse_tags(packet):
    """Read the 28-byte rtmd header and BER-sized SMPTE local sets, with bounds checks."""
    if len(packet) < 28 or packet[:2] != b'\x00\x1c':
        raise ValueError('Invalid Sony rtmd packet header')
    tags, pos = {}, 28
    while pos + 17 <= len(packet) and packet[pos:pos+4] == b'\x06\x0e\x2b\x34':
        length = packet[pos+16]
        start = pos + 17
        if length & 128:
            n = length & 127
            if not 1 <= n <= 4 or start+n > len(packet):
                raise ValueError('Invalid rtmd BER length')
            length = int.from_bytes(packet[start:start+n], 'big')
            start += n
        end = start + length
        if end > len(packet):
            raise ValueError('Truncated rtmd local set')
        j = start
        while j < end:
            if j + 4 > end:
                raise ValueError('Truncated rtmd tag header')
            tag, size = struct.unpack_from('>HH', packet, j)
            j += 4
            if j + size > end:
                raise ValueError(f'Truncated rtmd tag {tag:#x}')
            tags[tag] = packet[j:j+size]
            j += size
        pos = end
    return tags


def number(tags, tag, fmt, default=None):
    body = tags.get(tag)
    if body is None:
        if default is None:
            raise ValueError(f'Required Sony metadata tag {tag:#x} is missing')
        return default
    if len(body) != struct.calcsize(fmt):
        raise ValueError(f'Invalid Sony metadata tag {tag:#x}')
    values = struct.unpack(fmt, body)
    return values[0] if len(values) == 1 else values


def camera_rates(raw, orientation):
    # Sony's nibble order describes image yaw, negative pitch, roll.
    # Convert to the right-handed pinhole camera axes: x right, y down, z forward.
    axes = [(orientation >> shift) & 15 for shift in (0, 4, 8)]
    if any(a > 5 for a in axes) or len({a // 2 for a in axes}) != 3:
        raise ValueError(f'Invalid gyro orientation {orientation:#x}')
    oriented = np.stack([raw[:, a // 2] * (-1 if a % 2 else 1) for a in axes], 1)
    return oriented * [-1, -1, 1]


@dataclass
class Telemetry:
    times: np.ndarray
    rates: np.ndarray
    pts: np.ndarray
    centers: np.ndarray
    readout: np.ndarray
    focal: np.ndarray
    orientation: int
    frequency: float


def load_telemetry(path, index, width, height):
    packets = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', str(index), '-show_packets',
        '-show_entries', 'packet=pts_time,pos,size', '-of', 'json', str(path)]))['packets']
    times, rates, pts, centers, readouts, focals = [], [], [], [], [], []
    with open(path, 'rb') as video:
        for packet in packets:
            video.seek(int(packet['pos']))
            tags = parse_tags(video.read(int(packet['size'])))
            body = tags.get(0xe43b, b'')
            if len(body) < 8:
                raise ValueError('Missing gyro packet; refusing to interpolate across missing motion')
            count, size = struct.unpack_from('>II', body)
            if size != 6 or count == 0 or len(body) != 8 + count*size:
                raise ValueError('Invalid gyro sample table')
            raw = np.frombuffer(body, '>i2', count*3, 8).reshape(-1, 3).astype(float)
            scale = number(tags, 0xe439, '>f')
            hz = number(tags, 0xe435, '>i')
            unit = number(tags, 0xe436, '>i', 1_000_000)
            if scale <= 0 or hz <= 0 or unit <= 0:
                raise ValueError('Invalid gyro scale/frequency/time unit')
            if number(tags, 0xe43e, '>H', 0) & 0x8000:
                raw -= number(tags, 0xe43d, '>hhh')
            raw /= scale
            if number(tags, 0xe438, '>B', 0):
                raw = np.degrees(raw)
            orientation = number(tags, 0xe43a, '>H')
            p = float(packet['pts_time'])
            times.append(p + number(tags, 0xe437, '>i')/unit + np.arange(count)/hz)
            # Orientation 0x412 in this ZV-E10 is YxZ: [-raw_y, raw_x, raw_z].
            rates.append(camera_rates(raw, orientation))
            pts.append(p)
            sensor = np.array(number(tags, 0xe405, '>HH'), dtype=float)
            crop_unit = number(tags, 0xe408, '>i', 1)
            origin = np.array(number(tags, 0xe409, '>II'), dtype=float)/crop_unit
            crop = np.array(number(tags, 0xe40a, '>II'), dtype=float)/crop_unit
            sensor = np.maximum(sensor, crop + 2*origin)
            readout = number(tags, 0xe40e, '>i')/1e6
            centers.append(p + number(tags, 0xe40c, '>i')/1e6
                           - number(tags, 0xe40d, '>i')/2e6
                           + readout*(origin[1]+crop[1]/2)/sensor[1])
            readouts.append(readout*crop[1]/sensor[1])
            pitch = np.array(number(tags, 0xe407, '>hh'), dtype=float)
            pitch /= number(tags, 0xe406, '>i', 1_000_000_000)
            focal_m = number(tags, 0xe410, '>iii')[2]/1e9
            if focal_m <= 0 or np.any(pitch <= 0) or np.any(crop <= 0):
                raise ValueError('Invalid lens/sensor geometry')
            focals.append(focal_m / (pitch*crop/[width, height]))
            # This version intentionally fails closed for active optical/sensor stabilization.
            for tag in (0xe40f, 0xe450, 0xe416):
                if tag in tags and int.from_bytes(tags[tag][:4], 'big') > 0:
                    raise ValueError('Active IBIS/OSS metadata is not supported by this script')
    if not times:
        raise ValueError('No gyro samples found')
    t, w = np.concatenate(times), np.concatenate(rates)
    order = np.argsort(t, kind='stable')
    t, w = t[order], w[order]
    unique = np.r_[True, np.diff(t) > 1e-9]
    t, w = t[unique], w[unique]
    if np.max(np.diff(t)) > 0.01:
        raise ValueError('Gap in gyro samples exceeds 10 ms')
    return Telemetry(t, w, np.array(pts), np.array(centers), np.array(readouts),
                     np.array(focals), orientation, hz)
