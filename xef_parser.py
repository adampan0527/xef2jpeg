"""
XEF File Parser for Kinect V2 recordings

Parses .XEF files captured by Kinect V2 sensors by reading the actual
file structure: EVENTS1 header, stream descriptors, segment descriptors,
and interleaved event data segments.

XEF File Structure:
  1. File Header (44 bytes): EVENTS1 magic, stream count, timestamps
  2. Stream Descriptors: 0x3333-tagged entries describing each stream
  3. Event Data: Interleaved 32-byte segment descriptors + frame data
  4. Tail Index (last 8192 bytes): Optional seek table

Stream Types:
  1 = Body (skeleton)     - variable size
  2 = Calibration         - variable size
  3 = Depth               - 512x424 uint16 (434,176 bytes)
  4 = IR                  - 512x424 uint16 (434,176 bytes)
  5 = Opaque              - 640 bytes metadata
  6 = Telemetry           - 20 bytes sensor data
  7 = Color               - 1920x1080 BGRA (8,294,400 bytes)
"""

import struct
import logging
import numpy as np
from pathlib import Path
from PIL import Image, JpegImagePlugin

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

logger = logging.getLogger(__name__)

# Log file path (shared with xef2jpeg.py)
LOG_FILE = Path(__file__).parent / "xef2jpeg.log"


class XEFParser:
    """Parser for Kinect V2 XEF event stream files."""

    # Stream type constants
    STREAM_BODY = 1
    STREAM_CALIBRATION = 2
    STREAM_DEPTH = 3
    STREAM_IR = 4
    STREAM_OPAQUE = 5
    STREAM_TELEMETRY = 6
    STREAM_COLOR = 7

    STREAM_NAMES = {
        1: 'body',
        2: 'calibration',
        3: 'depth',
        4: 'ir',
        5: 'opaque',
        6: 'telemetry',
        7: 'color',
    }

    # Kinect V2 depth/IR dimensions
    DEPTH_WIDTH = 512
    DEPTH_HEIGHT = 424
    DEPTH_FRAME_SIZE = DEPTH_WIDTH * DEPTH_HEIGHT * 2  # 434,176 bytes

    # Kinect V2 color dimensions
    COLOR_WIDTH = 1920
    COLOR_HEIGHT = 1080
    COLOR_FRAME_SIZE = COLOR_WIDTH * COLOR_HEIGHT * 4  # 8,294,400 bytes (BGRA)

    # Segment descriptor size (28 bytes: stream_type(4) + frame_size(4) +
    # session_id(8) + hash(4) + frame_size2(4) + event_index(4))
    SEGMENT_DESC_SIZE = 28

    # File header magic
    HEADER_MAGIC = b'EVENTS1\x00'

    # Stream descriptor marker
    STREAM_DESC_MARKER = 0x3333

    def __init__(self, filepath, max_frames=None, target_streams=None):
        """
        Initialize parser with XEF file path.

        Args:
            filepath: Path to .xef file
            max_frames: Max frames per stream type to extract (None = unlimited)
            target_streams: List of stream type IDs to extract (default: [3, 4])
        """
        self.filepath = Path(filepath)
        self.frames = []
        self.streams = {}  # stream_type -> {'name': ..., 'frame_count': ...}
        self.max_frames = max_frames
        self.target_streams = target_streams if target_streams is not None else [self.STREAM_DEPTH, self.STREAM_IR]

    def parse(self, use_tqdm=True, callback=None):
        """Parse XEF file and extract frames from target streams.

        Args:
            use_tqdm: Show tqdm progress bar in terminal (default True)
            callback: Optional callback(progress, message) for GUI updates
        """
        with open(self.filepath, 'rb') as f:
            # 1. Read and validate file header
            self._read_header(f)

            # 2. Read stream descriptors
            self._read_stream_descriptors(f)

            # 3. Find start of event data
            if callback:
                callback(0.05, "Scanning for frame data...")
            try:
                event_start = self._find_event_data_start(f)
            except ValueError:
                event_start = None

            # 4. Extract frames sequentially from event data
            if event_start is not None:
                self._extract_frames_sequential(f, event_start, use_tqdm=use_tqdm,
                                                callback=callback)

            # 5. If descriptor-based extraction found nothing, try raw scanning
            if not self.frames:
                logger.info("No frames from descriptors, trying raw frame scanning")
                if callback:
                    callback(0.1, "Scanning for raw frame data...")
                self._extract_frames_raw(f, use_tqdm=use_tqdm, callback=callback)

        return self.frames

    def _read_header(self, f):
        """Read and validate the 44-byte file header."""
        f.seek(0)
        header = f.read(44)

        if len(header) < 44:
            raise ValueError("File too small to be a valid XEF file")

        if not header.startswith(self.HEADER_MAGIC):
            raise ValueError(
                f"Invalid XEF file: expected '{self.HEADER_MAGIC}' magic, "
                f"got '{header[:8]}'"
            )

        magic, unknown1, stream_count, timestamp1, field_24, field_28, field_32, field_36, field_40 = \
            struct.unpack_from('<8sII Q I I I I I', header)

        self.header = {
            'magic': magic,
            'unknown1': unknown1,
            'stream_count': stream_count,
            'timestamp1': timestamp1,
            'field_24': field_24,
            'field_28': field_28,
            'field_32': field_32,
            'field_36': field_36,
            'field_40': field_40,
        }
        self.stream_count = stream_count

        logger.info("XEF header: streams=%d, unknown1=%d", stream_count, unknown1)

    def _read_stream_descriptors(self, f):
        """Read stream descriptors from the area after the file header."""
        # Stream descriptors are between 0x2C and the start of event data.
        # They are marked with 0x3333.
        f.seek(0x2C)

        # Read a generous chunk to find all stream descriptors
        chunk = f.read(0x2000)  # 8KB should be enough

        stream_types_found = []
        pos = 0
        marker_bytes = struct.pack('<H', self.STREAM_DESC_MARKER)

        while pos < len(chunk) - 2:
            pos = chunk.find(marker_bytes, pos)
            if pos < 0:
                break

            region = chunk[pos:pos + 60]
            if len(region) < 16:
                pos += 2
                continue

            # The 2-byte value after the marker determines the stream type offset
            marker_val = struct.unpack_from('<H', region, 2)[0]

            # For marker_val=0x0018 (24), the stream type is at offset 28
            # For other marker_val values (0, 4), the stream type is at offset 12
            if marker_val == 24:
                # Special case: longer descriptor format
                type_offset = 28
            else:
                # Standard descriptor format
                type_offset = 12

            if len(region) >= type_offset + 4:
                stream_type = struct.unpack_from('<I', region, type_offset)[0]
                if 1 <= stream_type <= 10:
                    if stream_type not in [s['type'] for s in stream_types_found]:
                        stream_types_found.append({
                            'type': stream_type,
                            'name': self.STREAM_NAMES.get(stream_type, f'unknown_{stream_type}'),
                            'offset': 0x2C + pos,
                        })

            pos += 2

        self.streams = stream_types_found

        # If no streams found from markers, check for default Kinect V2 streams
        if not stream_types_found:
            self.streams = [
                {'type': 1, 'name': 'body', 'offset': 0},
                {'type': 2, 'name': 'calibration', 'offset': 0},
                {'type': 3, 'name': 'depth', 'offset': 0},
                {'type': 4, 'name': 'ir', 'offset': 0},
                {'type': 5, 'name': 'opaque', 'offset': 0},
                {'type': 6, 'name': 'telemetry', 'offset': 0},
            ]

        stream_names = [s['name'] for s in self.streams]
        logger.info("Streams found: %s", ', '.join(stream_names))

    def _find_event_data_start(self, f):
        """
        Find the start of event (segment) data.

        The event data begins after stream descriptors and calibration metadata.
        XEF files have a large metadata/calibration section before the actual
        interleaved event frames (depth, IR, body, etc.).  We skip calibration
        (type 2) descriptors because they use a different format.
        """
        f.seek(0)
        file_size = f.seek(0, 2)

        # Stream descriptors and calibration metadata can extend well past 5 MB,
        # so search up to 200 MB for the first non-calibration event segment.
        scan_start = 0x1000
        search_limit = min(scan_start + 200 * 1024 * 1024, file_size)
        f.seek(scan_start)

        while f.tell() < search_limit - self.SEGMENT_DESC_SIZE:
            offset = f.tell()
            desc = f.read(self.SEGMENT_DESC_SIZE)
            if len(desc) < self.SEGMENT_DESC_SIZE:
                break

            if self._is_valid_segment(desc, file_size):
                stream_type = struct.unpack_from('<I', desc, 0)[0]
                # Skip calibration descriptors (type 2) — they use a larger
                # header format and are part of the metadata section.
                if stream_type != self.STREAM_CALIBRATION:
                    return offset

            f.seek(offset + 1)

        raise ValueError("Could not find event data in XEF file")

    def _is_valid_segment(self, desc, file_size):
        """Check if a 28-byte block looks like a valid segment descriptor.

        XEF segment descriptor layout (28 bytes):
          bytes  0-3:  stream_type (uint32)
          bytes  4-7:  frame_size (uint32)
          bytes  8-15: session_id (uint64)
          bytes 16-19: hash/unknown (uint32) — skipped
          bytes 20-23: frame_size2 (uint32) — must equal frame_size
          bytes 24-27: event_index (uint32)
        """
        if len(desc) < self.SEGMENT_DESC_SIZE:
            return False
        try:
            stream_type, frame_size = struct.unpack_from('<II', desc, 0)
            frame_size2 = struct.unpack_from('<I', desc, 20)[0]
        except struct.error:
            return False

        # Stream type must be in valid range
        if stream_type < 1 or stream_type > 10:
            return False

        # Frame size must be positive and match the duplicate
        if frame_size <= 0 or frame_size != frame_size2:
            return False

        # Frame size must be reasonable
        if frame_size > 20_000_000:  # 20 MB max (color frames are ~8.3MB)
            return False

        return True

    def _collect_segments(self, f):
        """Scan the file to collect all valid segment descriptors.

        Scans the first 200MB of the file (where segment descriptors are
        clustered) in 4MB chunks.  Returns a sorted list of all valid
        segment descriptors found.

        Returns:
            List of (offset, stream_type, frame_size, seg_counter, event_index)
            tuples, sorted by file offset.
        """
        f.seek(0)
        file_size = f.seek(0, 2)
        desc_size = self.SEGMENT_DESC_SIZE  # 28

        # Only scan first 200MB — segment descriptors are clustered there
        scan_limit = min(200 * 1024 * 1024, file_size)
        chunk_size = 4 * 1024 * 1024  # 4MB chunks

        segments = []
        for chunk_start in range(0, scan_limit, chunk_size):
            f.seek(chunk_start)
            read_size = min(chunk_size + desc_size, file_size - chunk_start)
            data = f.read(read_size)

            for i in range(len(data) - desc_size):
                st = struct.unpack_from('<I', data, i)[0]
                fs = struct.unpack_from('<I', data, i + 4)[0]
                fs2 = struct.unpack_from('<I', data, i + 20)[0]

                if st < 1 or st > 20:
                    continue
                if fs <= 0 or fs != fs2:
                    continue
                if fs > 20_000_000:  # 20 MB max
                    continue

                abs_offset = chunk_start + i
                # Skip the stream descriptor region (first ~0x2000 bytes)
                if abs_offset < 0x2000:
                    continue

                seg_counter = struct.unpack_from('<I', data, i + 20)[0]
                event_index = struct.unpack_from('<I', data, i + 24)[0]
                segments.append((abs_offset, st, fs, seg_counter, event_index))

        # Sort by offset and remove duplicates
        segments.sort(key=lambda x: x[0])
        return segments

    def _detect_target_streams(self, segments):
        """Auto-detect which stream types contain depth and IR data.

        Different XEF format versions use different stream type numbers.
        We identify depth/IR streams by their frame size (434,176 bytes =
        512×424×2 uint16) and classify by zero-pixel percentage:
          - Depth frames have many zeros (>10%) because 0 = no data
          - IR frames have few zeros (<5%) because IR sensor provides data
            for all pixels

        Returns:
            Dict mapping detected stream type to content type ('depth' or 'ir').
        """
        # Group segments by stream type and frame size
        type_frame_sizes = {}  # stream_type -> set of frame_sizes
        for _, st, fs, _, _ in segments:
            if st not in type_frame_sizes:
                type_frame_sizes[st] = set()
            type_frame_sizes[st].add(fs)

        depth_ir_types = {}
        for st, sizes in type_frame_sizes.items():
            if self.DEPTH_FRAME_SIZE in sizes:
                # This stream type has depth/IR-sized frames.
                # Sample the first frame to classify as depth or IR.
                for seg_off, seg_st, seg_fs, _, _ in segments:
                    if seg_st == st and seg_fs == self.DEPTH_FRAME_SIZE:
                        try:
                            with open(self.filepath, 'rb') as fh:
                                fh.seek(seg_off + self.SEGMENT_DESC_SIZE)
                                sample = fh.read(self.DEPTH_FRAME_SIZE)
                                if len(sample) == self.DEPTH_FRAME_SIZE:
                                    arr = np.frombuffer(sample, dtype='<u2')
                                    total = arr.size
                                    zero_count = int((arr == 0).sum())
                                    zero_pct = zero_count * 100.0 / total

                                    # Depth frames have many zero pixels (no data)
                                    # IR frames have very few zeros
                                    if zero_pct > 10:
                                        depth_ir_types[st] = 'depth'
                                    else:
                                        depth_ir_types[st] = 'ir'

                                    logger.info("Auto-detected stream type %d: %s "
                                                "(zero_pct=%.1f%%, frame_size=%d)",
                                                st, depth_ir_types[st],
                                                zero_pct, seg_fs)
                        except Exception as e:
                            logger.debug("Failed to classify stream type %d: %s", st, e)
                        break

        return depth_ir_types

    def _extract_frames_sequential(self, f, start_offset, use_tqdm=True, callback=None):
        """Extract frames by scanning all segment descriptors upfront.

        Collects all valid segment descriptors from the entire file, then
        extracts frame data from each matching target stream.  This approach
        handles both old and new XEF format variants correctly, including
        cases where segments have variable gaps between them.

        Args:
            f: Open file object
            start_offset: Starting offset (used as minimum scan position)
            use_tqdm: Show tqdm progress bar in terminal
            callback: Optional callback(progress, message) for GUI updates
        """
        f.seek(0)
        file_size = f.seek(0, 2)

        # Phase 1: Collect all valid segment descriptors
        if callback:
            callback(0.05, "Scanning for segment descriptors...")
        all_segments = self._collect_segments(f)
        logger.info("Found %d segment descriptors total", len(all_segments))

        if not all_segments:
            logger.info("No segment descriptors found")
            return

        # Phase 2: Auto-detect depth/IR stream types
        detected_types = self._detect_target_streams(all_segments)
        logger.info("Detected depth/IR stream types: %s", detected_types)

        # Build effective target stream list:
        # Combine explicitly requested streams with auto-detected ones
        effective_targets = set(self.target_streams)
        for st, content_type in detected_types.items():
            if content_type == 'depth' and self.STREAM_DEPTH in self.target_streams:
                effective_targets.add(st)
            elif content_type == 'ir' and self.STREAM_IR in self.target_streams:
                effective_targets.add(st)

        # Phase 3: Extract frames from matching segments
        # Map auto-detected types to standard types for _process_frame dispatch
        type_mapping = {}  # detected_type -> standard_type (STREAM_DEPTH or STREAM_IR)
        for st, content_type in detected_types.items():
            if content_type == 'depth':
                type_mapping[st] = self.STREAM_DEPTH
            elif content_type == 'ir':
                type_mapping[st] = self.STREAM_IR

        # Count frames per detected stream type (not per standard type).
        # This ensures max_frames applies per detected type, so multiple
        # IR sub-types (e.g., type 6 + type 7) each get the full quota.
        frame_counts = {}
        # Build initial counts from target_streams (standard types)
        for st in self.target_streams:
            frame_counts[st] = 0
        # Add auto-detected types that map into a target
        for st in detected_types:
            if st not in frame_counts:
                frame_counts[st] = 0
        max_per_stream = self.max_frames

        segments_found = 0
        target_mismatch_count = 0

        # Set up progress bar
        file_stem = self.filepath.stem
        pbar = None
        if use_tqdm and tqdm is not None:
            try:
                pbar = tqdm(
                    desc=f"[{file_stem}] Extracting frames",
                    unit=" frames",
                    dynamic_ncols=True,
                    leave=False
                )
            except Exception:
                pbar = None

        try:
            for seg_offset, stream_type, frame_size, seg_counter, event_index in all_segments:
                # Map to standard type for _process_frame dispatch
                std_type = type_mapping.get(stream_type, stream_type)

                # Check if we have enough frames for this detected type
                if max_per_stream is not None:
                    if frame_counts.get(stream_type, 0) >= max_per_stream:
                        continue

                # Skip non-target streams
                if stream_type not in effective_targets:
                    target_mismatch_count += 1
                    continue

                segments_found += 1
                data_start = seg_offset + self.SEGMENT_DESC_SIZE
                if data_start + frame_size > file_size:
                    continue

                f.seek(data_start)
                frame_data = f.read(frame_size)
                if len(frame_data) != frame_size:
                    continue

                # Use per-type counter for frame_index
                frame_idx = frame_counts.get(stream_type, 0)
                # Pass standard type to _process_frame (it only knows standard types 3, 4, 7)
                frame_info = self._process_frame(
                    std_type, frame_data, frame_idx,
                    seg_counter, event_index
                )
                if frame_info:
                    self.frames.append(frame_info)
                    frame_counts[stream_type] = frame_idx + 1

                    total = sum(frame_counts.values())
                    if pbar is not None:
                        pbar.update(1)
                        pbar.set_postfix({
                            self.STREAM_NAMES.get(st, str(st)): frame_counts.get(st, 0)
                            for st in self.target_streams
                        })

                    if callback and total % 10 == 0:
                        parts = [f"{frame_counts.get(st, 0)} {self.STREAM_NAMES.get(st, st)}"
                                 for st in self.target_streams if frame_counts.get(st, 0) > 0]
                        callback(0.5, f"Extracting: {', '.join(parts)}...")

        finally:
            if pbar is not None:
                pbar.close()

        total_extracted = sum(frame_counts.values())
        logger.info("Extraction summary: %d segments found, %d target mismatches, "
                    "%d frames extracted",
                    segments_found, target_mismatch_count, total_extracted)

    def _extract_frames_raw(self, f, use_tqdm=True, callback=None):
        """
        Fallback frame extraction for XEF files without segment descriptors.

        Some newer XEF files store raw frames without the standard 28-byte
        segment descriptors.  Frames are stored at contiguous offsets starting
        after the header/metadata region (0x100000).

        Depth and IR frames are both 512×424 uint16 (434,176 bytes) but have
        very different value distributions:
          - Depth: mean values typically 300–1500 (range 50–8000)
          - IR:    mean values typically 20000–40000 (range >10000)

        The file contains interleaved runs of depth and IR frames (typically
        2–3 depth frames followed by 11–12 IR frames per cycle).  We scan
        every frame sequentially, classify by mean value, group consecutive
        same-type frames into regions, then extract from matching regions.

        Args:
            f: Open file object
            use_tqdm: Show tqdm progress bar in terminal
            callback: Optional callback(progress, message) for GUI updates
        """
        f.seek(0)
        file_size = f.seek(0, 2)
        frame_size = self.DEPTH_FRAME_SIZE  # 434,176 bytes (same for depth & IR)

        # Determine which streams to look for
        want_depth = self.STREAM_DEPTH in self.target_streams
        want_ir = self.STREAM_IR in self.target_streams

        if not want_depth and not want_ir:
            return

        # Phase 1: Classify every frame by sampling from the middle of the
        # frame (where data is most representative) and computing the mean
        # uint16 value.  Depth frames have mean < 5000, IR frames >= 5000.
        #
        # We sample 4000 bytes from 3 points (start, middle, end) to handle
        # boundary frames that may contain mixed depth/IR data at the edges.
        sample_size = 1000  # bytes per sample point
        num_samples = 10  # evenly spaced samples across the full frame

        # Precompute sample offsets evenly distributed across the frame.
        # This gives a much better approximation of the full-frame mean
        # than the old 3-point approach, eliminating the ~16% misclassification
        # of boundary IR frames as depth frames.
        sample_offsets = []
        if frame_size > sample_size:
            step = (frame_size - sample_size) // max(num_samples - 1, 1)
            for i in range(num_samples):
                sample_offsets.append(i * step)
        sample_offsets = sorted(set(sample_offsets))  # deduplicate
        data_start = 0x100000  # Skip header/metadata region
        max_frame_idx = (file_size - data_start) // frame_size

        if callback:
            callback(0.1, "Classifying frames...")

        frame_types = []  # list of stream_type for each frame index

        # Set up classification progress bar
        cls_pbar = None
        if use_tqdm and tqdm is not None:
            try:
                cls_pbar = tqdm(
                    total=max_frame_idx,
                    desc=f"[{self.filepath.stem}] Classifying",
                    unit=" frames",
                    dynamic_ncols=True,
                    leave=False
                )
            except Exception:
                cls_pbar = None

        try:
            for i in range(max_frame_idx):
                offset = data_start + i * frame_size
                if offset + frame_size > file_size:
                    break

                # Sample from evenly spaced points across the frame.
                # The mean of all sample means approximates the full-frame
                # mean much better than the old median-of-3 approach.
                sample_means = []
                for so in sample_offsets:
                    pos = offset + so
                    if pos + sample_size > file_size:
                        continue
                    f.seek(pos)
                    sample = f.read(sample_size)
                    if len(sample) < sample_size:
                        continue
                    values = struct.unpack_from(f'<{sample_size // 2}H', sample)
                    sample_means.append(sum(values) / len(values))

                if not sample_means:
                    continue

                # Use the mean (not median) of all sample means for robust
                # classification. Depth: mean < 5000, IR: mean >= 5000.
                mean_val = sum(sample_means) / len(sample_means)

                # Depth: mean < 5000, IR: mean >= 5000
                if mean_val < 5000:
                    frame_types.append(self.STREAM_DEPTH)
                else:
                    frame_types.append(self.STREAM_IR)

                if cls_pbar is not None and (i + 1) % 1000 == 0:
                    cls_pbar.update(1000)
        finally:
            if cls_pbar is not None:
                cls_pbar.update(len(frame_types) - (cls_pbar.n or 0))
                cls_pbar.close()

        if not frame_types:
            logger.info("Raw scan: no valid frames found")
            return

        depth_count = frame_types.count(self.STREAM_DEPTH)
        ir_count = frame_types.count(self.STREAM_IR)
        logger.info("Raw scan classified %d frames: %d depth, %d IR",
                    len(frame_types), depth_count, ir_count)

        # Phase 2: Group consecutive same-type frames into regions.
        # Each region = (stream_type, start_frame_idx, count)
        regions = []
        i = 0
        while i < len(frame_types):
            st = frame_types[i]
            start = i
            while i < len(frame_types) and frame_types[i] == st:
                i += 1
            regions.append((st, start, i - start))

        # Filter to only regions matching target streams
        target_regions = [(st, s, c) for st, s, c in regions
                          if st in self.target_streams]
        logger.info("Found %d regions (%d matching target streams)",
                    len(regions), len(target_regions))

        if not target_regions:
            return

        # Phase 3: Extract frames from matching regions.
        frame_counts = {st: 0 for st in self.target_streams}
        max_per_stream = self.max_frames
        file_stem = self.filepath.stem

        pbar = None
        if use_tqdm and tqdm is not None:
            try:
                pbar = tqdm(
                    desc=f"[{file_stem}] Extracting raw frames",
                    unit=" frames",
                    dynamic_ncols=True,
                    leave=False
                )
            except Exception:
                pbar = None

        try:
            for stream_type, region_start, region_count in target_regions:
                if max_per_stream is not None and all(frame_counts[st] >= max_per_stream for st in self.target_streams):
                    break

                for j in range(region_count):
                    if max_per_stream is not None and all(frame_counts[st] >= max_per_stream for st in self.target_streams):
                        break

                    frame_idx = region_start + j
                    offset = data_start + frame_idx * frame_size

                    if offset + frame_size > file_size:
                        break

                    f.seek(offset)
                    raw = f.read(frame_size)
                    if len(raw) < frame_size:
                        break

                    # Clean mixed boundary frames
                    raw = self._clean_mixed_frame(stream_type, raw)

                    frame_info = self._process_frame(
                        stream_type, raw, frame_counts[stream_type], 0, 0
                    )
                    if frame_info:
                        self.frames.append(frame_info)
                        frame_counts[stream_type] += 1
                        total = sum(frame_counts.values())

                        if pbar is not None:
                            pbar.update(1)
                            pbar.set_postfix({
                                self.STREAM_NAMES.get(st, str(st)): frame_counts[st]
                                for st in self.target_streams
                                if frame_counts[st] > 0
                            })

                        if callback and total % 10 == 0:
                            parts = [f"{frame_counts[st]} {self.STREAM_NAMES.get(st, st)}"
                                     for st in self.target_streams if frame_counts[st] > 0]
                            callback(0.5, f"Extracting: {', '.join(parts)}...")

        finally:
            if pbar is not None:
                pbar.close()

        logger.info("Raw extraction: %s",
                     ", ".join(f"{frame_counts[st]} {self.STREAM_NAMES.get(st, st)}"
                              for st in self.target_streams))

    def _clean_mixed_frame(self, stream_type, raw_data):
        """Zero out cross-contamination in boundary frames.

        Some frames at the depth/IR boundary contain data from both types:
        e.g., depth pixels in the first N rows and IR data in the rest, or
        IR data at the top and depth at the bottom.  This method detects
        and zeros out contaminating rows in BOTH directions (top and bottom)
        so each frame contains only the target stream data.

        Args:
            stream_type: The classified stream type (STREAM_DEPTH or STREAM_IR)
            raw_data: Raw frame bytes (DEPTH_FRAME_SIZE bytes)

        Returns:
            Cleaned raw_data (same object if no cleaning needed)
        """
        width = self.DEPTH_WIDTH   # 512
        height = self.DEPTH_HEIGHT  # 424
        row_bytes = width * 2       # 1024

        _ir_contamination = struct.pack('H', 0xFFFF) * width

        if stream_type == self.STREAM_DEPTH:
            # --- Top contamination: IR data at the top of a depth frame ---
            first_row = struct.unpack_from(f'<{width}H', raw_data, 0)
            ffff_top = sum(1 for v in first_row if v == 0xFFFF)
            if ffff_top >= width // 4:
                # Binary search from top for first non-IR row (mean < 30000)
                lo, hi = 0, height - 1
                while lo < hi:
                    mid = (lo + hi) // 2
                    row = struct.unpack_from(f'<{width}H', raw_data, mid * row_bytes)
                    row_mean = sum(row) / len(row)
                    if row_mean > 30000:  # Still IR
                        lo = mid + 1
                    else:  # Depth starts
                        hi = mid
                if lo > 0:
                    mutable = bytearray(raw_data)
                    for r in range(0, lo):
                        start = r * row_bytes
                        mutable[start:start + row_bytes] = _ir_contamination
                    raw_data = bytes(mutable)
                    logger.debug("Cleaned depth frame: zeroed top rows 0-%d "
                                 "(IR contamination)", lo - 1)

            # --- Bottom contamination: IR data at the bottom ---
            last_row_offset = (height - 1) * row_bytes
            last_row = struct.unpack_from(f'<{width}H', raw_data, last_row_offset)
            ffff_count = sum(1 for v in last_row if v == 0xFFFF)
            if ffff_count < width // 4:  # Less than 25% 0xFFFF → clean bottom
                return raw_data

            # Binary search for the first IR row from top
            lo, hi = 0, height - 1
            while lo < hi:
                mid = (lo + hi) // 2
                row = struct.unpack_from(f'<{width}H', raw_data, mid * row_bytes)
                row_mean = sum(row) / len(row)
                if row_mean > 30000:  # IR data
                    hi = mid
                else:
                    lo = mid + 1

            if lo < height:
                mutable = bytearray(raw_data)
                for r in range(lo, height):
                    start = r * row_bytes
                    mutable[start:start + row_bytes] = _ir_contamination
                logger.debug("Cleaned depth frame: zeroed bottom rows %d-%d "
                             "(IR contamination)", lo, height - 1)
                return bytes(mutable)

        elif stream_type == self.STREAM_IR:
            # --- Top contamination: depth data at the top of an IR frame ---
            first_row = struct.unpack_from(f'<{width}H', raw_data, 0)
            first_row_mean = sum(first_row) / len(first_row)
            if first_row_mean < 5000:
                # Binary search from top for first IR row (mean >= 5000)
                lo, hi = 0, height - 1
                while lo < hi:
                    mid = (lo + hi) // 2
                    row = struct.unpack_from(f'<{width}H', raw_data, mid * row_bytes)
                    row_mean = sum(row) / len(row)
                    if row_mean < 5000:  # Still depth
                        lo = mid + 1
                    else:  # IR starts
                        hi = mid
                if lo > 0:
                    mutable = bytearray(raw_data)
                    for r in range(0, lo):
                        start = r * row_bytes
                        for k in range(start, start + row_bytes):
                            mutable[k] = 0
                    raw_data = bytes(mutable)
                    logger.debug("Cleaned IR frame: zeroed top rows 0-%d "
                                 "(depth contamination)", lo - 1)

            # --- Bottom contamination: depth data at the bottom ---
            last_row_offset = (height - 1) * row_bytes
            last_row = struct.unpack_from(f'<{width}H', raw_data, last_row_offset)
            last_row_mean = sum(last_row) / len(last_row)
            if last_row_mean > 5000:  # Still IR-level → clean bottom
                return raw_data

            # Binary search for the first depth row from top
            lo, hi = 0, height - 1
            while lo < hi:
                mid = (lo + hi) // 2
                row = struct.unpack_from(f'<{width}H', raw_data, mid * row_bytes)
                row_mean = sum(row) / len(row)
                if row_mean < 5000:  # Depth data
                    hi = mid
                else:
                    lo = mid + 1

            if lo < height:
                mutable = bytearray(raw_data)
                for r in range(lo, height):
                    start = r * row_bytes
                    for k in range(start, start + row_bytes):
                        mutable[k] = 0
                logger.debug("Cleaned IR frame: zeroed bottom rows %d-%d "
                             "(depth contamination)", lo, height - 1)
                return bytes(mutable)

        return raw_data

    # --- Quadrant-level quality checks for newer XEF format ---
    # In the newer XEF format (12 streams), each 434,176-byte frame is stored
    # as 4 sequential 256x212 sub-images (quadrants), NOT as a single 512x424
    # image.  Mixed transition frames contain both depth and IR quadrants.

    def _is_valid_ir_quadrant(self, raw_bytes, width, height):
        """Check if a single quadrant (256x212) contains a real IR image.

        Two checks distinguish real IR images from sensor telemetry:
        1. Adjacent pixel difference (adj_diff): telemetry has regular
           stripe patterns with high adj_diff; real images have smooth,
           spatially varying content with lower adj_diff.
        2. Column-to-column variation (col_std): telemetry stripes create
           high variation across columns; real IR images have low
           column-to-column variation because scene content is spatially
           coherent.

        Empirical thresholds from Kinect V2 data:
          Real IR images:  adj_diff < 1000, col_std < 200
          Telemetry:       adj_diff > 500,  col_std > 200

        Args:
            raw_bytes: Raw uint16 bytes for one quadrant
            width: Quadrant width (e.g. 256)
            height: Quadrant height (e.g. 212)

        Returns:
            True if the quadrant appears to contain a real IR image.
        """
        try:
            arr = np.frombuffer(raw_bytes, dtype=np.uint16).reshape(height, width)
        except ValueError:
            return False

        mean_val = float(arr.mean())

        # Must be IR-level values (not depth, not 0xFFFF)
        if mean_val < 5000 or mean_val > 65000:
            return False

        # Check for mostly-invalid data
        valid_mask = (arr > 0) & (arr < 0xFFFE)
        if np.count_nonzero(valid_mask) < arr.size * 0.02:
            return False

        # Check 1: Adjacent pixel difference
        row_diffs = np.abs(arr[:, 1:].astype(np.int32) - arr[:, :-1].astype(np.int32))
        col_diffs = np.abs(arr[1:, :].astype(np.int32) - arr[:-1, :].astype(np.int32))
        adj_diff = float((row_diffs.mean() + col_diffs.mean()) / 2)

        if adj_diff > 1000:
            return False

        # Check 2: Column-to-column variation.
        # Telemetry has regular stripe patterns → high col_std.
        # Real IR images have spatially coherent content → low col_std.
        col_means = arr.astype(np.float32).mean(axis=0)
        col_std = float(col_means.std())

        if col_std > 200:
            return False

        return True

    def _is_mixed_frame(self, raw_data):
        """Detect if a frame contains mixed depth/IR quadrants.

        In the newer XEF format, frames are stored as 4 sequential 256x212
        sub-images.  Mixed transition frames contain depth data in some
        quadrants and IR data in others.  This creates a visible cross
        pattern when rendered as a single 512x424 image.

        Args:
            raw_data: Raw frame bytes (434,176 bytes for depth/IR)

        Returns:
            Tuple of (is_mixed, quadrant_types) where quadrant_types is
            a list of 4 strings: 'depth', 'ir', '0xffff', or 'unknown'.
        """
        q_pixels = 256 * 212
        q_bytes = q_pixels * 2  # 108,544 bytes per quadrant

        if len(raw_data) < q_bytes * 4:
            return False, []

        q_means = []
        for qi in range(4):
            chunk = np.frombuffer(
                raw_data[qi * q_bytes:(qi + 1) * q_bytes], dtype=np.uint16
            )
            q_means.append(float(chunk.mean()))

        # Classify each quadrant
        q_types = []
        for m in q_means:
            if m < 5000:
                q_types.append('depth')
            elif m > 65000:
                q_types.append('0xffff')
            else:
                q_types.append('ir')

        # Mixed if we have both depth and non-depth quadrants
        has_depth = any(t == 'depth' for t in q_types)
        has_ir = any(t == 'ir' for t in q_types)
        has_ffff = any(t == '0xffff' for t in q_types)

        is_mixed = (has_depth and (has_ir or has_ffff)) or \
                   (has_ir and has_ffff and not has_depth)

        return is_mixed, q_types

    def _is_valid_ir_frame(self, raw_data):
        """Check if a full 512x424 frame contains a valid IR image.

        Used for non-mixed frames to filter out sensor telemetry that
        has been classified as IR based on mean value alone.

        Args:
            raw_data: Raw frame bytes (434,176 bytes)

        Returns:
            True if the frame appears to contain a real IR image.
        """
        try:
            arr = np.frombuffer(raw_data, dtype=np.uint16).reshape(
                self.DEPTH_HEIGHT, self.DEPTH_WIDTH
            )
        except ValueError:
            return False

        # Adjacent pixel difference: telemetry > 1000, real IR < 1000
        row_diffs = np.abs(arr[:, 1:].astype(np.int32) - arr[:, :-1].astype(np.int32))
        col_diffs = np.abs(arr[1:, :].astype(np.int32) - arr[:-1, :].astype(np.int32))
        adj_diff = float((row_diffs.mean() + col_diffs.mean()) / 2)

        return adj_diff < 1000

    def _process_frame(self, stream_type, raw_data, frame_index, seg_counter, event_index, width=None, height=None):
        """Process raw frame data into a normalized image array.

        Args:
            stream_type: STREAM_DEPTH, STREAM_IR, or STREAM_COLOR
            raw_data: Raw frame bytes
            frame_index: Frame sequence number
            seg_counter: Segment counter from descriptor
            event_index: Event index from descriptor
            width: Frame width (default: DEPTH_WIDTH for depth/IR, COLOR_WIDTH for color)
            height: Frame height (default: DEPTH_HEIGHT for depth/IR, COLOR_HEIGHT for color)
        """
        if stream_type == self.STREAM_DEPTH:
            return self._process_depth_frame(raw_data, frame_index, seg_counter, event_index, width, height)
        elif stream_type == self.STREAM_IR:
            return self._process_ir_frame(raw_data, frame_index, seg_counter, event_index, width, height)
        elif stream_type == self.STREAM_COLOR:
            return self._process_color_frame(raw_data, frame_index, seg_counter, event_index)
        return None

    def _process_depth_frame(self, raw_data, frame_index, seg_counter, event_index, width=None, height=None):
        """Process a depth frame (uint16) into a normalized 8-bit image.

        Supports both full 512x424 frames and 256x212 quadrants from
        newer XEF format mixed frames.
        """
        w = width or self.DEPTH_WIDTH
        h = height or self.DEPTH_HEIGHT
        try:
            depth_array = np.frombuffer(raw_data, dtype=np.uint16).reshape(h, w)
        except ValueError:
            return None

        # Normalize to 0-255 for visualization
        depth_normalized = np.zeros_like(depth_array, dtype=np.uint8)

        # Valid depth range for Kinect V2: typically 500-4500mm
        # Values of 0 typically indicate invalid/no data
        valid_mask = (depth_array > 0) & (depth_array < 8000)

        if np.any(valid_mask):
            valid_values = depth_array[valid_mask]
            vmin, vmax = valid_values.min(), valid_values.max()
            if vmax > vmin:
                depth_normalized[valid_mask] = (
                    (valid_values - vmin) * 255.0 / (vmax - vmin)
                ).astype(np.uint8)
            else:
                depth_normalized[valid_mask] = 128

        return {
            'type': 'depth',
            'stream_type': self.STREAM_DEPTH,
            'stream_name': 'depth',
            'data': depth_normalized,
            'raw_data': depth_array,
            'index': frame_index,
            'seg_counter': seg_counter,
            'event_index': event_index,
        }

    def _process_ir_frame(self, raw_data, frame_index, seg_counter, event_index, width=None, height=None):
        """Process an IR frame (uint16) into a normalized 8-bit image.

        Supports both full 512x424 frames and 256x212 quadrants from
        newer XEF format mixed frames.
        """
        w = width or self.DEPTH_WIDTH
        h = height or self.DEPTH_HEIGHT
        try:
            ir_array = np.frombuffer(raw_data, dtype=np.uint16).reshape(h, w)
        except ValueError:
            return None

        # --- Noise detection: skip frames with no discernible structure ---
        nonzero_mask = (ir_array > 0) & (ir_array < 0xFFFE)
        nonzero_count = np.count_nonzero(nonzero_mask)
        total_pixels = ir_array.size

        # Frame is mostly invalid — no meaningful IR data
        if nonzero_count < total_pixels * 0.02:
            logger.debug("IR frame %d: %.1f%% valid pixels, skipping as noise",
                        frame_index, nonzero_count / total_pixels * 100)
            return None

        # Check for flat/empty frames by measuring row-to-row variation.
        # A legitimate IR frame has spatial structure (brightness varies
        # across rows).  Pure noise has nearly uniform row means.
        valid_pixels = ir_array[nonzero_mask]
        row_stds = []
        for r in range(h):
            row = ir_array[r]
            row_valid = row[(row > 0) & (row < 0xFFFE)]
            if len(row_valid) > 100:
                row_stds.append(float(row_valid.std()))
        if row_stds:
            mean_row_std = sum(row_stds) / len(row_stds)
            # If the typical within-row std is very low, the frame is flat noise
            if mean_row_std < 20:
                logger.debug("IR frame %d: mean_row_std=%.1f, skipping as noise",
                            frame_index, mean_row_std)
                return None

        # Normalize to 0-255 for visualization
        # IR values are typically 0-65535, with real data in a smaller range
        ir_normalized = np.zeros_like(ir_array, dtype=np.uint8)

        # Filter out obvious invalid values (0xFFFF and 0 are typically invalid)
        valid_mask = (ir_array > 0) & (ir_array < 0xFFFE)

        if np.any(valid_mask):
            valid_values = ir_array[valid_mask]
            vmin, vmax = np.percentile(valid_values, [2, 98])
            if vmax > vmin:
                # Clip to percentile range and normalize
                clipped = np.clip(ir_array, vmin, vmax)
                ir_normalized = ((clipped - vmin) * 255.0 / (vmax - vmin)).astype(np.uint8)

        return {
            'type': 'ir',
            'stream_type': self.STREAM_IR,
            'stream_name': 'ir',
            'data': ir_normalized,
            'raw_data': ir_array,
            'index': frame_index,
            'seg_counter': seg_counter,
            'event_index': event_index,
        }

    def _process_color_frame(self, raw_data, frame_index, seg_counter, event_index):
        """Process a color frame (1920x1080 BGRA) into an RGB image array."""
        expected_size = self.COLOR_WIDTH * self.COLOR_HEIGHT * 4
        if len(raw_data) != expected_size:
            # Try alternative color resolutions
            # Some XEF files may use different resolutions
            total_pixels = len(raw_data) // 4
            if total_pixels <= 0:
                return None
            # Try common Kinect V2 color resolutions
            for w, h in [(1920, 1080), (1280, 720), (960, 540), (640, 480)]:
                if total_pixels == w * h:
                    try:
                        bgra_array = np.frombuffer(raw_data[:w * h * 4], dtype=np.uint8).reshape(
                            (h, w, 4)
                        )
                        # Convert BGRA to RGB
                        rgb_array = bgra_array[:, :, [2, 1, 0]].copy()
                        return {
                            'type': 'color',
                            'stream_type': self.STREAM_COLOR,
                            'stream_name': 'color',
                            'data': rgb_array,
                            'raw_data': bgra_array,
                            'index': frame_index,
                            'seg_counter': seg_counter,
                            'event_index': event_index,
                        }
                    except (ValueError, IndexError):
                        continue
            return None

        try:
            bgra_array = np.frombuffer(raw_data, dtype=np.uint8).reshape(
                (self.COLOR_HEIGHT, self.COLOR_WIDTH, 4)
            )
        except ValueError:
            return None

        # Convert BGRA to RGB
        rgb_array = bgra_array[:, :, [2, 1, 0]].copy()

        return {
            'type': 'color',
            'stream_type': self.STREAM_COLOR,
            'stream_name': 'color',
            'data': rgb_array,
            'raw_data': bgra_array,
            'index': frame_index,
            'seg_counter': seg_counter,
            'event_index': event_index,
        }

    def save_frames_to_jpeg(self, output_dir, base_name=None, quality=95):
        """
        Save extracted frames as JPEG files.

        Depth frames are saved to output_dir/Depth/
        IR frames are saved to output_dir/IR/
        Color frames are saved to output_dir/Color/

        Naming format: {basename}_{stream_name}_{index:04d}.jpg

        Returns:
            List of saved file paths.
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        if base_name is None:
            base_name = self.filepath.stem

        # Stream type to folder name mapping
        STREAM_FOLDER_NAMES = {
            'depth': 'Depth',
            'ir': 'IR',
            'color': 'Color',
            'body': 'Body',
            'calibration': 'Calibration',
            'opaque': 'Opaque',
            'telemetry': 'Telemetry',
        }

        # Create stream-specific subdirectories
        subdirs = {}
        saved_files = []
        # Global counter per stream name to avoid filename collisions
        # when multiple detected types (e.g., type 6 + type 7) map to
        # the same output stream (e.g., 'ir').
        stream_frame_counter = {}

        for frame in self.frames:
            stream_name = frame.get('stream_name', frame['type'])

            # Ensure subdirectory exists for this stream type
            if stream_name not in subdirs:
                folder_name = STREAM_FOLDER_NAMES.get(stream_name, stream_name.capitalize())
                subdir_path = output_path / folder_name
                subdir_path.mkdir(parents=True, exist_ok=True)
                subdirs[stream_name] = subdir_path

            frame_num = stream_frame_counter.get(stream_name, 0)
            stream_frame_counter[stream_name] = frame_num + 1
            filename = f"{base_name}_{stream_name}_{frame_num:04d}.jpg"
            filepath = subdirs[stream_name] / filename

            if stream_name == 'color':
                # Save color frames as RGB JPEG with EXIF orientation
                img = Image.fromarray(frame['data'], 'RGB')
                img = self._add_exif_orientation(img)
                img.save(str(filepath), 'JPEG', quality=quality)
            else:
                # Save depth/IR frames as grayscale
                img = Image.fromarray(frame['data'], 'L')
                img.save(str(filepath), 'JPEG', quality=quality)

            saved_files.append(str(filepath))

        return saved_files

    def _add_exif_orientation(self, img, orientation=1):
        """Add EXIF orientation tag to a PIL Image.

        Args:
            img: PIL Image object
            orientation: EXIF orientation value (1=normal, 3=180deg, etc.)

        Returns:
            PIL Image with EXIF data set.
        """
        try:
            img.getexif()[0x0112] = orientation
        except (AttributeError, Exception):
            pass
        return img

    def get_stream_info(self):
        """Return information about streams found in the file."""
        return [
            {'type': s['type'], 'name': s['name']}
            for s in self.streams
        ]

    def has_stream(self, stream_type):
        """Check if the file has a specific stream type."""
        return any(s['type'] == stream_type for s in self.streams)


def convert_xef_to_jpeg(xef_path, output_dir, max_frames=None, target_streams=None, callback=None, quality=95, use_tqdm=True):
    """
    Convert XEF file to JPEG images.

    Creates a timestamped folder inside output_dir:
        {output_dir}/{YYYY_MM_DD_HH_MM_SS}/
            Depth/   (if depth frames)
            IR/      (if IR frames)
            Color/   (if color frames)

    Args:
        xef_path: Path to input .xef file
        output_dir: Path to output directory (created if not exists)
        max_frames: Maximum number of frames per stream type (None = unlimited)
        target_streams: List of stream types to extract (default: [3, 4] = depth, ir)
        callback: Optional callback function(progress, message)
        quality: JPEG quality (1-100, default 95)
        use_tqdm: Show tqdm progress bar in terminal (default True)

    Returns:
        Tuple of (stream_types_found, saved_file_paths, output_folder_path)
    """
    from datetime import datetime

    # Create timestamped output folder
    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    base_output = Path(output_dir)
    session_folder = base_output / timestamp
    session_folder.mkdir(parents=True, exist_ok=True)

    logger.info("Converting XEF file: %s", xef_path)
    logger.info("Output folder: %s", session_folder)

    if callback:
        callback(0.0, "Validating XEF file...")

    parser = XEFParser(xef_path, max_frames=max_frames, target_streams=target_streams)
    parser.parse(use_tqdm=use_tqdm, callback=callback)

    if callback:
        stream_info = parser.get_stream_info()
        available = [s['name'] for s in stream_info]
        callback(0.1, f"Found streams: {', '.join(available)}")

    frames = parser.frames

    if not frames:
        # Clean up empty timestamp folder
        try:
            session_folder.rmdir()
        except OSError:
            pass

        stream_info = parser.get_stream_info()
        available_names = [s['name'] for s in stream_info]
        target_names = [parser.STREAM_NAMES.get(s, f'unknown_{s}') for s in (target_streams or [])]
        logger.warning("No frames extracted. Available streams: %s",
                       ', '.join(available_names) if available_names else 'none')
        raise ValueError(
            f"No frames extracted from target streams.\n\n"
            f"Requested: {', '.join(target_names)}\n"
            f"Available: {', '.join(available_names) if available_names else 'none'}\n\n"
            f"Check the log file for diagnostic details:\n{LOG_FILE}"
        )

    if callback:
        callback(0.5, f"Found {len(frames)} frames, converting to JPEG...")

    logger.info("Found %d frames, saving to JPEG (quality=%d)", len(frames), quality)

    saved_files = parser.save_frames_to_jpeg(session_folder, quality=quality)

    if callback:
        callback(1.0, "Conversion complete")

    frame_types = list(set(f['stream_name'] for f in frames))
    logger.info("Conversion complete: %d files saved", len(saved_files))
    return frame_types, saved_files, str(session_folder)
