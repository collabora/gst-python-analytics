import re
import gi
import skia
import cairo
import numpy as np

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

    def __init__(self):
        super(Overlay, self).__init__()
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

    def do_start(self):
        # Setup trail surface for fading circles
        self.trail_surface = skia.Surface(self.width, self.height)
        Gst.info("Skia trail surface initialized.")
        return True

    def do_stop(self):
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
            Gst.warning("No GstAnalytics metadata found on buffer.")
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
        metadata = self.extract_metadata(buf)

        video_meta = GstVideo.buffer_get_video_meta(buf)
        if not video_meta:
            Gst.error("No video meta available, cannot proceed with overlay")
            return Gst.FlowReturn.ERROR

        success, map_info = buf.map(Gst.MapFlags.WRITE)
        if not success:
            return Gst.FlowReturn.ERROR

        try:
            # Create Skia surface for the main canvas
            surface = self.create_overlay_surface(map_info, self.width, self.height)
            canvas = surface.getCanvas()

            # Fade out existing circles on trail surface
            self.trail_surface.getCanvas().drawColor(
                skia.Color4f(0, 0, 0, 0.1), skia.BlendMode.kDstIn
            )

            # Fade and draw circles from history
            for past_point in self.history:
                self.draw_trail_circle(
                    self.trail_surface.getCanvas(),
                    past_point["center"],
                    past_point["opacity"],
                    past_point["color"],
                )
                # Gradually fade the opacity of each point
                past_point["opacity"] *= 0.9

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
                self.draw_bounding_box(canvas, data["box"])
                track_id = data.get(
                    "track_id"
                )  # Assumes `track_id` is available in metadata
                color = self.get_color_for_id(track_id)

                # Adjust the center point to be lower toward the bottom of the bounding box
                center = {
                    "x": (data["box"]["x1"] + data["box"]["x2"]) / 2,
                    "y": (
                        data["box"]["y1"] * 0.25 + data["box"]["y2"] * 0.75
                    ),  # Adjusts y to be lower
                }

                self.draw_trail_circle(
                    self.trail_surface.getCanvas(), center, 1.0, color
                )

                # Add new point to history with color and opacity information
                self.history.append({"center": center, "opacity": 1.0, "color": color})

                # Draw label near the bounding box using Cairo for faster text rendering
                self.draw_label_with_cairo(
                    cr, data["label"], data["box"]["x1"], data["box"]["y1"]
                )

            if len(self.history) > self.max_history_length:
                self.history.pop(0)

            # Composite trail surface onto main surface
            paint = skia.Paint()
            paint.setColor(
                skia.Color4f(1, 1, 1, 0.9)
            )  # Set color with alpha for blending
            canvas.drawImage(self.trail_surface.makeImageSnapshot(), 0, 0, paint)

            # Ensure Cairo operations are complete before unmapping
            cr.stroke()
            cairo_surface.finish()
            cr = None
            cairo_surface = None  # Explicitly release Cairo references

        finally:
            buf.unmap(map_info)  # Unmap buffer after ensuring no references remain

        return Gst.FlowReturn.OK

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


# Register the element with GStreamer
GObject.type_register(Overlay)
__gstelementfactory__ = ("overlay_pyml", Gst.Rank.NONE, Overlay)