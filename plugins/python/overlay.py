# Overlay
# Copyright (C) 2024-2025 Collabora Ltd.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Library General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Library General Public License for more details.
#
# You should have received a copy of the GNU Library General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor,
# Boston, MA 02110-1301, USA.


CAN_REGISTER_ELEMENT = True
try:
    import re
    import gi
    import cairo
    import numpy as np
    import json

    gi.require_version("Gst", "1.0")
    gi.require_version("GstBase", "1.0")
    gi.require_version("GstVideo", "1.0")
    gi.require_version("GLib", "2.0")
    gi.require_version("GstAnalytics", "1.0")
    from gi.repository import (
        Gst,
        GstBase,
        GstVideo,
        GstAnalytics,
        GLib,
        GObject,
    )  # noqa: E402
except ImportError as e:
    CAN_REGISTER_ELEMENT = False
    Gst.warning(f"The 'pyml_overlay' element will not be available. Error: {e}")

# Define video formats manually
VIDEO_FORMATS = "video/x-raw, format=(string){ RGBA, ARGB, BGRA, ABGR }"

# Create OBJECT_DETECTION_OVERLAY_CAPS
OBJECT_DETECTION_OVERLAY_CAPS = Gst.Caps.from_string(VIDEO_FORMATS)


class Overlay(GstBase.BaseTransform):
    __gstmetadata__ = (
        "Overlay",
        "Filter/Effect/Video",
        "Overlays object detection / tracking data on video",
        "Aaron Boxer <aaron.boxer@collabora.com>",
    )

    src_template = Gst.PadTemplate.new(
        "src",
        Gst.PadDirection.SRC,
        Gst.PadPresence.ALWAYS,
        OBJECT_DETECTION_OVERLAY_CAPS.copy(),
    )

    sink_template = Gst.PadTemplate.new(
        "sink",
        Gst.PadDirection.SINK,
        Gst.PadPresence.ALWAYS,
        OBJECT_DETECTION_OVERLAY_CAPS.copy(),
    )
    __gsttemplates__ = (src_template, sink_template)

    # Add the meta_path property
    meta_path = GObject.Property(
        type=str,
        default=None,
        nick="Metadata Path",
        blurb="Path to the JSON file containing frame metadata with bounding boxes and tracking data",
        flags=GObject.ParamFlags.READWRITE,
    )

    def __init__(self):
        super(Overlay, self).__init__()
        self.meta_path = None  # Initialize the frame_meta property
        self.preloaded_metadata = {}  # Dictionary to store frame-indexed metadata
        self.frame_counter = 0
        self.outline_color = skia.ColorWHITE
        self.width = 640
        self.height = 480
        self.history = []
        self.max_history_length = 5000
        # Define a color palette with 20 distinct colors
        self.color_palette = [
            skia.Color4f(1.0, 0.0, 0.0, 1.0),  # Red
            skia.Color4f(0.0, 1.0, 0.0, 1.0),  # Green
            skia.Color4f(0.0, 0.0, 1.0, 1.0),  # Blue
            skia.Color4f(1.0, 1.0, 0.0, 1.0),  # Yellow
            skia.Color4f(1.0, 0.0, 1.0, 1.0),  # Magenta
            skia.Color4f(0.0, 1.0, 1.0, 1.0),  # Cyan
            skia.Color4f(1.0, 0.5, 0.0, 1.0),  # Orange
            skia.Color4f(0.5, 0.0, 1.0, 1.0),  # Purple
            skia.Color4f(0.5, 1.0, 0.0, 1.0),  # Lime
            skia.Color4f(0.0, 0.5, 1.0, 1.0),  # Light Blue
            skia.Color4f(1.0, 0.3, 0.3, 1.0),  # Light Red
            skia.Color4f(0.3, 1.0, 0.3, 1.0),  # Light Green
            skia.Color4f(0.3, 0.3, 1.0, 1.0),  # Light Blue
            skia.Color4f(1.0, 1.0, 0.3, 1.0),  # Light Yellow
            skia.Color4f(1.0, 0.3, 1.0, 1.0),  # Pink
            skia.Color4f(0.3, 1.0, 1.0, 1.0),  # Aqua
            skia.Color4f(0.5, 0.2, 0.0, 1.0),  # Brown
            skia.Color4f(0.2, 0.5, 0.0, 1.0),  # Olive
            skia.Color4f(0.5, 0.5, 0.5, 1.0),  # Grey
            skia.Color4f(1.0, 0.6, 0.4, 1.0),  # Peach
        ]

        # Dictionary to store ID-to-color mapping
        self.id_color_map = {}

    def do_get_property(self, prop: GObject.ParamSpec):
        if prop.name == "meta-path":
            return self.meta_path
        else:
            raise AttributeError(f"Unknown property {prop.name}")

    def do_set_property(self, prop: GObject.ParamSpec, value):
        if prop.name == "meta-path":
            self.meta_path = value
            self.load_and_store_metadata()  # Load JSON data when property is set
        else:
            raise AttributeError(f"Unknown property {prop.name}")

    def do_start(self):
        # Setup trail surface for fading circles
        self.trail_surface = skia.Surface(self.width, self.height)
        Gst.info("Skia trail surface initialized.")
        return True

    def do_stop(self):
        # Cleanup Skia trail surface
        if self.trail_surface:
            del self.trail_surface
            self.trail_surface = None
        Gst.info("Overlay stopped, resources cleaned.")
        return True

    def do_set_caps(self, incaps, outcaps):
        video_info = GstVideo.VideoInfo.new_from_caps(incaps)
        self.width = video_info.width
        self.height = video_info.height
        Gst.info(f"Video caps set: width={self.width}, height={self.height}")
        return True

    def get_color_for_id(self, track_id):
        """Get a color for the given track ID."""
        if track_id not in self.id_color_map:
            # Assign the next color in the palette, cycling if necessary
            color_index = len(self.id_color_map) % len(self.color_palette)
            self.id_color_map[track_id] = self.color_palette[color_index]
        return self.id_color_map[track_id]

    def load_and_store_metadata(self):
        if self.preloaded_metadata:
            return
        """Load JSON data once and store it for fast indexing."""
        if not self.meta_path:
            Gst.error("Frame metadata file path not set.")
            return

        try:
            with open(self.meta_path, "r") as f:
                all_data = json.load(f)
                frame_data = all_data.get("frames", [])
                # Store metadata indexed by frame_index
                self.preloaded_metadata = {
                    frame.get("frame_index"): frame.get("objects", [])
                    for frame in frame_data
                }
            Gst.warning(f"Loaded metadata for {len(self.preloaded_metadata)} frames.")
        except FileNotFoundError:
            Gst.error(f"JSON file not found: {self.meta_path}")
        except json.JSONDecodeError as e:
            Gst.error(f"Failed to parse JSON file: {e}")
        except Exception as e:
            Gst.error(f"Unexpected error while loading metadata: {e}")

    def get_metadata_for_frame(self, frame_index):
        """Retrieve preloaded metadata for the given frame index."""
        return self.preloaded_metadata.get(frame_index, [])

    def extract_id_from_label(self, label):
        """Extracts the numeric ID from a label formatted as 'id_<number>'."""
        match = re.match(r"id_(\d+)", label)
        if match:
            track_id = int(match.group(1))
            return track_id
        else:
            print("No ID found in label")  # Optional debug message for unmatched format
            return None  # Return None if the ID format is not found

    def extract_metadata(self, buffer):
        metadata = []
        meta = GstAnalytics.buffer_get_analytics_relation_meta(buffer)
        if not meta:
            #Gst.warning("No GstAnalytics metadata found on buffer.")
            return metadata

        try:
            count = GstAnalytics.relation_get_length(meta)
            for index in range(count):
                ret, od_mtd = meta.get_od_mtd(index)
                if not ret or od_mtd is None:
                    continue

                label_quark = od_mtd.get_obj_type()
                label = GLib.quark_to_string(label_quark)
                track_id = self.extract_id_from_label(label)
                location = od_mtd.get_location()
                presence, x, y, w, h, loc_conf_lvl = location
                if presence:
                    metadata.append(
                        {
                            "label": label,
                            "track_id": track_id,
                            "confidence": loc_conf_lvl,
                            "box": {"x1": x, "y1": y, "x2": x + w, "y2": y + h},
                        }
                    )
        except Exception as e:
            Gst.error(f"Error while extracting metadata: {e}")
        return metadata

    def create_overlay_surface(self, map_info, width, height):
        image_data = np.frombuffer(map_info.data, dtype=np.uint8)
        image_data = image_data.reshape((height, width, 4))
        surface = skia.Surface.MakeRasterDirect(
            skia.ImageInfo.MakeN32Premul(width, height), image_data
        )
        return surface

    def do_transform_ip(self, buf):
        self.load_and_store_metadata()  # Load metadata if not already loaded
        metadata = self.extract_metadata(buf)
        frame_metadata = self.get_metadata_for_frame(self.frame_counter)

        # Combine metadata from buffer and preloaded JSON
        metadata.extend(frame_metadata)

        # Skip processing if no metadata exists for the current frame
        if not metadata:
            Gst.warning(f"No metadata found for frame {self.frame_counter}.")
            self.frame_counter += 1
            return Gst.FlowReturn.OK

        video_meta = GstVideo.buffer_get_video_meta(buf)
        if not video_meta:
            Gst.error("No video meta available, cannot proceed with overlay")
            return Gst.FlowReturn.ERROR

        success, map_info = buf.map(Gst.MapFlags.WRITE)
        if not success:
            return Gst.FlowReturn.ERROR

        cairo_surface = None
        cr = None

        try:
            # Create a Cairo surface for text rendering directly on buffer data
            cairo_surface = cairo.ImageSurface.create_for_data(
                map_info.data,
                cairo.FORMAT_ARGB32,
                self.width,
                self.height,
                self.width * 4,
            )
            cr = cairo.Context(cairo_surface)

            # Draw bounding boxes and labels on main surface
            for data in metadata:
                # Draw bounding box
                self.draw_bounding_box_with_cairo(
                    cr, data["box"]
                )

                # Draw label near the bounding box using Cairo
                self.draw_label_with_cairo(
                    cr, data["label"], data["box"]["x1"], data["box"]["y1"]
                )

            # Ensure Cairo operations are complete before unmapping
            cr.stroke()
            cairo_surface.finish()

        finally:
            # Cleanup resources
            if cairo_surface:
                del cairo_surface
            if cr:
                del cr
            buf.unmap(map_info)  # Unmap buffer after ensuring no references remain
            self.frame_counter += 1

        return Gst.FlowReturn.OK

    def draw_bounding_box_with_cairo(self, cr, box):
        """Draw a bounding box using Cairo."""
        cr.set_line_width(2.0)
        cr.set_source_rgb(1, 0, 0)  # Red color for bounding box
        cr.rectangle(box["x1"], box["y1"], box["x2"] - box["x1"], box["y2"] - box["y1"])
        cr.stroke()

    def draw_label_with_cairo(self, cr, label, x, y):
        """Draw a label using Cairo at the specified position."""
        cr.set_font_size(12)
        cr.set_source_rgb(1, 1, 1)  # White color for label
        cr.move_to(x, y - 10)  # Position the text above the bounding box
        cr.show_text(label)
        cr.stroke()

    def draw_bounding_box(self, canvas, box):
        paint = skia.Paint(
            Color=self.outline_color, StrokeWidth=2, Style=skia.Paint.kStroke_Style
        )
        canvas.drawRect(skia.Rect(box["x1"], box["y1"], box["x2"], box["y2"]), paint)

    def draw_trail_circle(self, canvas, center, opacity, color):
        """Draws a trail circle with the specified color."""
        paint = skia.Paint(
            Color=skia.Color4f(color.fR, color.fG, color.fB, opacity),
            Style=skia.Paint.kFill_Style,
        )
        canvas.drawCircle(center["x"], center["y"], 5, paint)

    def draw_label_with_cairo(self, cr, label, x, y):
        """Draws a label with Cairo at the specified position."""
        cr.set_font_size(12)
        cr.set_source_rgba(1, 1, 1, 1)  # White color
        cr.move_to(x, y - 10)  # Position the text above the bounding box
        cr.show_text(label)
        cr.stroke()

if CAN_REGISTER_ELEMENT:
    GObject.type_register(Overlay)
    __gstelementfactory__ = ("pyml_overlay", Gst.Rank.NONE, Overlay)
else:
    Gst.warning(
        "The 'pyml_overlay' element will not be registered because gst_object_detector module is missing."
    )
