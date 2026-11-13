#!/usr/bin/env python3
import os
import signal
import sys
import struct
import threading
import time
import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf


class IntercomWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="ThinkSmart Intercom")
        self.set_default_size(1280, 800)
        self.set_decorated(False)
        self.fullscreen()
        self.get_style_context().add_class("main-win")

        self.start_ts = time.monotonic()
        self.last_remote_ts = None
        self.last_elapsed = -1
        # Orientation state automatically synced from server (ftype 3)
        self.flip_h = True
        self.rot_deg = 0
        self.sensor_rot = 0
        self.frame_lock = threading.Lock()
        self.latest_f1 = None
        self.latest_f2 = None
        self.last_f1 = None
        self.last_f2 = None
        self.last_pb1 = None
        self.last_pb2 = None
        self.f1_fresh = False
        self.flush_scheduled = False

        overlay = Gtk.Overlay()
        self.add(overlay)

        # Center area: stack or box showing either remote video or audio-call avatar
        self.bg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.bg_box.set_size_request(1280, 800)
        self.bg_box.set_valign(Gtk.Align.FILL)
        self.bg_box.set_halign(Gtk.Align.FILL)
        self.bg_box.get_style_context().add_class("bg-box")
        overlay.add(self.bg_box)

        # Placeholder avatar box when phone camera is off or connecting
        self.avatar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.avatar_box.set_valign(Gtk.Align.CENTER)
        self.avatar_box.set_halign(Gtk.Align.CENTER)

        avatar_icon = Gtk.Label(label="📞")
        avatar_icon.get_style_context().add_class("avatar-circle")
        self.avatar_box.pack_start(avatar_icon, False, False, 0)

        avatar_title = Gtk.Label(label="手机远程可视对讲已接通")
        avatar_title.get_style_context().add_class("avatar-title")
        self.avatar_box.pack_start(avatar_title, False, False, 0)

        self.bg_box.pack_start(self.avatar_box, True, True, 0)

        # Fullscreen remote video image overlay
        self.remote_img = Gtk.Image()
        self.remote_img.set_valign(Gtk.Align.CENTER)
        self.remote_img.set_halign(Gtk.Align.CENTER)
        overlay.add_overlay(self.remote_img)
        overlay.set_overlay_pass_through(self.remote_img, True)

        # Top status pill
        top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top_box.set_valign(Gtk.Align.START)
        top_box.set_halign(Gtk.Align.CENTER)
        top_box.set_margin_top(24)
        top_box.get_style_context().add_class("status-pill")

        dot_lbl = Gtk.Label(label="●")
        dot_lbl.get_style_context().add_class("live-dot")
        top_box.pack_start(dot_lbl, False, False, 0)

        self.timer_lbl = Gtk.Label(label="可视对讲通话中  00:00")
        self.timer_lbl.get_style_context().add_class("status-text")
        top_box.pack_start(self.timer_lbl, False, False, 0)

        overlay.add_overlay(top_box)
        overlay.set_overlay_pass_through(top_box, True)

        # Bottom-right local PiP preview
        self.pip_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.pip_frame.set_valign(Gtk.Align.END)
        self.pip_frame.set_halign(Gtk.Align.END)
        self.pip_frame.set_margin_end(28)
        self.pip_frame.set_margin_bottom(28)
        self.pip_frame.get_style_context().add_class("pip-frame")

        self.local_img = Gtk.Image()
        self.pip_frame.pack_start(self.local_img, False, False, 0)
        overlay.add_overlay(self.pip_frame)
        overlay.set_overlay_pass_through(self.pip_frame, True)

        # Bottom center Hangup button
        bottom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        bottom_box.set_valign(Gtk.Align.END)
        bottom_box.set_halign(Gtk.Align.CENTER)
        bottom_box.set_margin_bottom(32)

        btn = Gtk.Button(label="  挂断通话  ")
        btn.set_size_request(240, 64)
        btn.get_style_context().add_class("hangup-btn")
        btn.connect("clicked", self.on_hangup)
        bottom_box.pack_start(btn, False, False, 0)
        overlay.add_overlay(bottom_box)

        css = b"""
        .main-win, .bg-box {
            background-color: #0b0f19;
        }
        .avatar-circle {
            background-color: #2563eb;
            color: #ffffff;
            font-size: 56px;
            border-radius: 999px;
            padding: 36px 44px;
            border: 4px solid rgba(59, 130, 246, 0.45);
        }
        .avatar-title {
            color: #f8fafc;
            font-size: 28px;
            font-weight: bold;
        }
        .status-pill {
            background-color: rgba(15, 23, 42, 0.82);
            border-radius: 999px;
            padding: 10px 24px;
            border: 1px solid rgba(255, 255, 255, 0.18);
        }
        .live-dot {
            color: #10b981;
            font-size: 20px;
        }
        .status-text {
            color: #ffffff;
            font-size: 20px;
            font-weight: bold;
        }
        .pip-frame {
            background-color: #000000;
            border: 2px solid rgba(255, 255, 255, 0.35);
            border-radius: 8px;
            padding: 2px;
        }
        .hangup-btn {
            background-image: none;
            background-color: #ef4444;
            color: #ffffff;
            font-size: 22px;
            font-weight: bold;
            border-radius: 32px;
            border: 2px solid rgba(255, 255, 255, 0.25);
            padding: 12px 32px;
        }
        .hangup-btn:active {
            background-color: #dc2626;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        GLib.timeout_add(500, self.on_tick)

    def on_hangup(self, _widget):
        try:
            sys.stdout.write("HANGUP\n")
            sys.stdout.flush()
        except Exception:
            pass
        Gtk.main_quit()

    def on_tick(self):
        now = time.monotonic()
        elapsed = int(now - self.start_ts)
        if elapsed != self.last_elapsed:
            self.last_elapsed = elapsed
            hh, rem = divmod(max(0, elapsed), 3600)
            mm, ss = divmod(rem, 60)
            ts = f"{hh}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
            self.timer_lbl.set_text(f"可视对讲通话中  {ts}")
        timed_out = False
        with self.frame_lock:
            if not self.f1_fresh and self.last_remote_ts is not None and (now - self.last_remote_ts) > 3.5:
                self.last_remote_ts = None
                self.last_f1 = None
                self.last_pb1 = None
                self.latest_f1 = None
                self.f1_fresh = False
                timed_out = True
        if timed_out:
            self.remote_img.clear()
            self.avatar_box.show()
        return True

    @staticmethod
    def rotate_cw(pb, deg):
        deg = deg % 360
        if deg == 90:
            return pb.rotate_simple(GdkPixbuf.PixbufRotation.CLOCKWISE)
        elif deg == 180:
            return pb.rotate_simple(GdkPixbuf.PixbufRotation.UPSIDEDOWN)
        elif deg == 270:
            return pb.rotate_simple(GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE)
        return pb

    def apply_orientation(self, pb, is_local_pip=False, rot_deg=None, flip_h=None, sensor_rot=None):
        rot_deg = self.rot_deg if rot_deg is None else rot_deg
        flip_h = self.flip_h if flip_h is None else flip_h
        sensor_rot = self.sensor_rot if sensor_rot is None else sensor_rot
        if is_local_pip:
            if flip_h:
                pb = pb.flip(True)
                pb = self.rotate_cw(pb, (sensor_rot - rot_deg) % 360)
            else:
                pb = self.rotate_cw(pb, (rot_deg + sensor_rot) % 360)
        else:
            pb = self.rotate_cw(pb, sensor_rot)
        return pb

    def decode_jpg(self, data):
        loader = GdkPixbuf.PixbufLoader.new_with_type('jpeg')
        try:
            loader.write(data)
        finally:
            try:
                loader.close()
            except Exception:
                pass
        return loader.get_pixbuf()

    def flush_frames(self):
        with self.frame_lock:
            f1 = self.latest_f1 if (self.f1_fresh or self.last_remote_ts is not None) else None
            f2 = self.latest_f2
            f1_fresh = self.f1_fresh
            self.latest_f1 = None
            self.latest_f2 = None
            self.f1_fresh = False
            self.flush_scheduled = False
            rot_deg = self.rot_deg
            flip_h = self.flip_h
            sensor_rot = self.sensor_rot

        if f1 is not None:
            try:
                pb = self.last_pb1[1] if (self.last_pb1 and self.last_pb1[0] is f1) else self.decode_jpg(f1)
                if pb and pb.get_width() > 0 and pb.get_height() > 0:
                    self.last_pb1 = (f1, pb)
                    with self.frame_lock:
                        if f1_fresh:
                            self.last_remote_ts = time.monotonic()
                        active_remote = self.last_remote_ts is not None
                        if active_remote:
                            self.last_f1 = f1
                        rot_deg, flip_h, sensor_rot = self.rot_deg, self.flip_h, self.sensor_rot
                    pb = self.apply_orientation(pb, is_local_pip=False, rot_deg=rot_deg, flip_h=flip_h, sensor_rot=sensor_rot)
                    pw, ph = pb.get_width(), pb.get_height()
                    if pw > 0 and ph > 0:
                        scale = min(1280.0 / pw, 800.0 / ph)
                        sw = max(1, int(pw * scale))
                        sh = max(1, int(ph * scale))
                        scaled = pb if (sw == pw and sh == ph) else pb.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR)
                        if active_remote:
                            self.remote_img.set_from_pixbuf(scaled)
                            if self.avatar_box.get_visible():
                                self.avatar_box.hide()
            except Exception:
                pass

        if f2 is not None:
            ok2 = False
            try:
                if self.last_pb2 and self.last_pb2[0] is f2:
                    scaled_pip = self.last_pb2[1]
                    ok2 = True
                else:
                    pb = self.decode_jpg(f2)
                    scaled_pip = None
                    if pb:
                        pw, ph = pb.get_width(), pb.get_height()
                        if pw > 0 and ph > 0:
                            tw, th = (256, 192) if pw >= ph else (192, 256)
                            scaled_pip = pb if (pw == tw and ph == th) else pb.scale_simple(tw, th, GdkPixbuf.InterpType.BILINEAR)
                            self.last_pb2 = (f2, scaled_pip)
                            ok2 = True
                if ok2 and scaled_pip:
                    with self.frame_lock:
                        self.last_f2 = f2
                        rot_deg, flip_h, sensor_rot = self.rot_deg, self.flip_h, self.sensor_rot
                    scaled_pip = self.apply_orientation(scaled_pip, is_local_pip=True, rot_deg=rot_deg, flip_h=flip_h, sensor_rot=sensor_rot)
                    self.local_img.set_from_pixbuf(scaled_pip)
            except Exception:
                pass
        return False

    def enqueue_frame(self, ftype, data):
        with self.frame_lock:
            if ftype == 3:
                if len(data) < 2:
                    return
                new_rot = (int(data[0]) & 3) * 90
                new_flip = bool(data[1])
                new_srot = (int(data[2]) & 3) * 90 if len(data) >= 3 else self.sensor_rot
                if (new_rot, new_flip, new_srot) == (self.rot_deg, self.flip_h, self.sensor_rot):
                    return
                self.rot_deg, self.flip_h, self.sensor_rot = new_rot, new_flip, new_srot
                if self.latest_f1 is None and self.last_f1 is not None and self.last_remote_ts is not None:
                    self.latest_f1 = self.last_f1
                if self.latest_f2 is None and self.last_f2 is not None:
                    self.latest_f2 = self.last_f2
                if self.latest_f1 is None and self.latest_f2 is None:
                    return
            elif ftype == 1:
                self.latest_f1 = data
                self.f1_fresh = True
            elif ftype == 2:
                self.latest_f2 = data
            else:
                return
            if not self.flush_scheduled:
                self.flush_scheduled = True
                GLib.idle_add(self.flush_frames)


def _read_exact(fd, n):
    chunk = fd.read(n)
    if not chunk:
        return None
    if len(chunk) == n:
        return chunk
    buf = bytearray(chunk)
    while len(buf) < n:
        more = fd.read(n - len(buf))
        if not more:
            return None
        buf.extend(more)
    return bytes(buf)


def stdin_reader(win):
    fd = sys.stdin.buffer
    try:
        while True:
            hdr = _read_exact(fd, 5)
            if not hdr:
                break
            ftype = hdr[0]
            flen = struct.unpack("<I", hdr[1:5])[0]
            if flen == 0 or flen > 5_000_000:
                break
            data = _read_exact(fd, flen)
            if data is None:
                break
            win.enqueue_frame(ftype, data)
    except Exception:
        pass
    GLib.idle_add(Gtk.main_quit)


if __name__ == "__main__":
    win = IntercomWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, _sig, lambda *_: (Gtk.main_quit(), False)[1])
    t = threading.Thread(target=stdin_reader, args=(win,), daemon=True)
    t.start()
    Gtk.main()
    sys.stdout.flush()
    os._exit(0)
