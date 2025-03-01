import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstBase", "1.0")
from gi.repository import Gst, GLib, GstBase
import struct

# Initialize GStreamer
Gst.init(None)

class Metadata:
    SIGNATURE = b"CUSTOM-META"

    def __init__(self, format_string: str):
        self.format_string = format_string
        self.struct_size = struct.calcsize(format_string)
        self.meta_bytes = None

    def add_to_buffer(self, buffer: Gst.Buffer, *values) -> None:
        """Add metadata as a Gst.Memory chunk at the end of the buffer."""
        try:
            data_bytes = struct.pack(self.format_string, *values)
            self.meta_bytes = self.SIGNATURE + struct.pack("I", len(self.format_string)) + \
                             self.format_string.encode("utf-8") + data_bytes
            meta_memory = Gst.Memory.new_wrapped(
                Gst.MemoryFlags.READONLY,
                self.meta_bytes,
                len(self.meta_bytes),
                0,
                len(self.meta_bytes),
                None
            )
            buffer.append_memory(meta_memory)
            print(f"Metadata added, size: {len(self.meta_bytes)} bytes")
        except struct.error as e:
            raise ValueError(f"Failed to pack values: {str(e)}") from e

    def read_from_buffer(self, buffer: Gst.Buffer) -> tuple:
        """Read metadata from the last memory chunk."""
        n_mem = buffer.n_memory()
        if n_mem < 2:
            raise ValueError(f"No metadata memory chunk found (expected at least 2 chunks, got {n_mem})")
        meta_memory = buffer.peek_memory(n_mem - 1)
        with meta_memory.map(Gst.MapFlags.READ) as map_info:
            data_bytes = bytes(map_info.data)
            if not data_bytes.startswith(self.SIGNATURE):
                raise ValueError(f"Invalid metadata signature (expected {self.SIGNATURE.hex()}, got {data_bytes[:len(self.SIGNATURE)].hex()})")
            offset = len(self.SIGNATURE)
            fmt_len = struct.unpack("I", data_bytes[offset:offset + 4])[0]
            offset += 4
            fmt_str = data_bytes[offset:offset + fmt_len].decode("utf-8")
            offset += fmt_len
            if fmt_str != self.format_string:
                raise ValueError(f"Format mismatch: expected {self.format_string}, got {fmt_str}")
            return struct.unpack(self.format_string, data_bytes[offset:offset + self.struct_size])

class MetadataAdder(GstBase.BaseTransform):
    __gstmetadata__ = (
        "MetadataAdder",
        "Transform",
        "Adds custom metadata to video buffers",
        "Your Name"
    )

    # Define static pad templates
    _srctemplate = Gst.PadTemplate.new(
        "src",
        Gst.PadDirection.SRC,
        Gst.PadPresence.ALWAYS,
        Gst.Caps.from_string("video/x-raw")
    )
    _sinktemplate = Gst.PadTemplate.new(
        "sink",
        Gst.PadDirection.SINK,
        Gst.PadPresence.ALWAYS,
        Gst.Caps.from_string("video/x-raw")
    )

    __gsttemplates__ = (_srctemplate, _sinktemplate)

    def __init__(self):
        super().__init__()
        self.metadata = Metadata("if")
        self.frame_count = 0
        self.set_in_place(True)  # Transform in-place

    def do_set_caps(self, in_caps, out_caps):
        """Accept and forward caps."""
        print(f"Setting caps: {in_caps.to_string()} -> {out_caps.to_string()}")
        return True

    def do_transform_ip(self, buffer):
        """Transform in-place: add metadata to each buffer."""
        self.frame_count += 1
        self.metadata.add_to_buffer(buffer, self.frame_count, 3.14)
        print(f"Frame {self.frame_count}: Buffer size: {buffer.get_size()}, Chunks: {buffer.n_memory()}")
        return Gst.FlowReturn.OK

# Register the custom element
Gst.Element.register(None, "metadataadder", Gst.Rank.NONE, MetadataAdder)

# Create the pipeline
def create_pipeline():
    pipeline = Gst.Pipeline.new("video-test-pipeline")
    src = Gst.ElementFactory.make("videotestsrc", "source")
    transform = Gst.ElementFactory.make("metadataadder", "transform")
    sink = Gst.ElementFactory.make("glimagesink", "sink")

    if not all([src, transform, sink]):
        raise RuntimeError("Failed to create pipeline elements")

    pipeline.add(src)
    pipeline.add(transform)
    pipeline.add(sink)

    # Link elements with debug
    if not src.link(transform):
        raise RuntimeError("Failed to link videotestsrc to metadataadder")
    if not transform.link(sink):
        raise RuntimeError("Failed to link metadataadder to glimagesink")

    # Configure videotestsrc
    src.set_property("num-buffers", 100)  # Limit to 100 frames
    src.set_property("pattern", "smpte")  # SMPTE color bars

    return pipeline

# Bus message handler
def on_bus_message(bus, message, loop):
    t = message.type
    if t == Gst.MessageType.EOS:
        print("End of stream")
        loop.quit()
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print(f"Error: {err}, Debug: {debug}")
        loop.quit()
    return True

# Probe to check metadata
def sink_pad_probe(pad, info, metadata):
    buffer = info.get_buffer()
    print(f"Sink probe: Buffer size: {buffer.get_size()}, Chunks: {buffer.n_memory()}")
    try:
        values = metadata.read_from_buffer(buffer)
        print(f"Sink metadata: {values}")
    except ValueError as e:
        print(f"Sink metadata error: {e}")
    return Gst.PadProbeReturn.OK

# Main function
def main():
    pipeline = create_pipeline()
    metadata = Metadata("if")

    # Add probe on sink pad
    sink = pipeline.get_by_name("sink")
    sink_pad = sink.get_static_pad("sink")
    sink_pad.add_probe(Gst.PadProbeType.BUFFER, sink_pad_probe, metadata)

    # Set up bus
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    loop = GLib.MainLoop()
    bus.connect("message", on_bus_message, loop)

    # Start pipeline
    pipeline.set_state(Gst.State.PLAYING)

    # Run the main loop
    try:
        loop.run()
    except KeyboardInterrupt:
        print("Interrupted")

    # Clean up
    pipeline.set_state(Gst.State.NULL)

if __name__ == "__main__":
    main()
