#!/usr/bin/env python3
import sys
import os
import mmap
import cairocffi as cairo
import wayland.protocol
from wayland.client import MakeDisplay
from wayland.utils import AnonymousFile
import math

import select
import time
import logging

# See https://github.com/sde1000/python-xkbcommon for the following:
from xkbcommon import xkb

log = logging.getLogger(__name__)

shutdowncode = None

# List of future events; objects must support the nexttime attribute
# and alarm() method. nexttime should be the time at which the object
# next wants to be called, or None if the object temporarily does not
# need to be scheduled.
eventlist = []

# List of file descriptors to watch with handlers.  Expected to be objects
# with a fileno() method that returns the appropriate fd number, and methods
# called doread(), dowrite(), etc.
rdlist = []

# List of functions to invoke each time around the event loop.  These
# functions may do anything, including changing timeouts and drawing
# on the display.
ticklist = []

# List of functions to invoke before calling select.  These functions
# may not change timeouts or draw on the display.  They will typically
# flush queued output.
preselectlist = []

class time_guard(object):
    def __init__(self, name, max_time):
        self._name = name
        self._max_time = max_time
    def __enter__(self):
        self._start_time = time.time()
    def __exit__(self, type, value, traceback):
        t = time.time()
        time_taken = t - self._start_time
        if time_taken > self._max_time:
            log.info("time_guard: %s took %f seconds",self._name,time_taken)

tick_time_guard = time_guard("tick",0.5)
preselect_time_guard = time_guard("preselect",0.1)
doread_time_guard = time_guard("doread",0.5)
dowrite_time_guard = time_guard("dowrite",0.5)
doexcept_time_guard = time_guard("doexcept",0.5)
alarm_time_guard = time_guard("alarm",0.5)

def eventloop():
    global shutdowncode
    while shutdowncode is None:
        for i in ticklist:
            with tick_time_guard:
                i()
        # Work out what the earliest timeout is
        timeout = None
        t = time.time()
        for i in eventlist:
            nt = i.nexttime
            i.mainloopnexttime = nt
            if nt is None:
                continue
            if timeout is None or (nt - t) < timeout:
                timeout = nt - t
        for i in preselectlist:
            with preselect_time_guard:
                i()
        try:
            (rd, wr, ex) = select.select(rdlist, [], [], timeout)
        except KeyboardInterrupt:
            (rd, wr, ex) = [], [], []
            shutdowncode = 1
        for i in rd:
            with doread_time_guard:
                i.doread()
        for i in wr:
            with dowrite_time_guard:
                i.dowrite()
        for i in ex:
            with doexcept_time_guard:
                i.doexcept()
        # Process any events whose time has come
        t = time.time()
        for i in eventlist:
            if not hasattr(i, 'mainloopnexttime'):
                continue
            if i.mainloopnexttime and t >= i.mainloopnexttime:
                with alarm_time_guard:
                    i.alarm()

def ping_handler(thing, serial):
    """
    Respond to a 'ping' with a 'pong'.
    """
    thing.pong(serial)

class Window:
    def __init__(self, connection, width, height, title="Window",
                 class_="python-wayland-test", redraw=None, fullscreen=False):
        self.title = title
        self.orig_width = width
        self.orig_height = height
        self._w = connection
        if not self._w.shm_formats:
            raise RuntimeError("No suitable Shm formats available")
        self.is_fullscreen = fullscreen
        self.redraw_func = redraw
        self.surface = self._w.compositor.create_surface()
        self._w.surfaces[self.surface] = self
        self.xdg_surface = self._w.xdg_wm_base.get_xdg_surface(self.surface)
        self.xdg_toplevel = self.xdg_surface.get_toplevel()
        self.xdg_toplevel.set_title(title)
        self.xdg_toplevel.set_parent(None)
        self.xdg_toplevel.set_app_id(class_)
        self.xdg_toplevel.set_min_size(width, height)
        self.xdg_toplevel.set_max_size(width, height)

        if fullscreen:
            self.xdg_toplevel.set_fullscreen(None)

        self.wait_for_configure = True
        self.xdg_surface.dispatcher['ping'] = ping_handler
        self.xdg_surface.dispatcher['configure'] = \
            self._xdg_surface_configure_handler

        #self.xdg_toplevel.dispatcher['configure'] = lambda *x: None
        #self.xdg_toplevel.dispatcher['close'] = lambda *x: None

        self.buffer = None
        self.shm_data = None
        self.surface.commit()

    def close(self):
        if not self.surface.destroyed:
            self.surface.destroy()
            if self.buffer is not None:
                self.buffer.destroy()
                self.buffer = None
                self.shm_data.close()
                del self.s, self.shm_data

    def resize(self, width, height):
        # Drop previous buffer and shm data if necessary
        if self.buffer:
            self.buffer.destroy()
            self.shm_data.close()

        # Do not complete a resize until configure has been acknowledged
        if self.wait_for_configure:
            return

        wl_shm_format, cairo_shm_format = self._w.shm_formats[0]
        
        stride = cairo.ImageSurface.format_stride_for_width(
            cairo_shm_format, width)
        size = stride * height

        with AnonymousFile(size) as fd:
            self.shm_data = mmap.mmap(
                fd, size, prot=mmap.PROT_READ | mmap.PROT_WRITE,
                flags=mmap.MAP_SHARED)
            pool = self._w.shm.create_pool(fd, size)
            self.buffer = pool.create_buffer(
                0, width, height, stride, wl_shm_format)
            pool.destroy()
        self.s = cairo.ImageSurface(cairo_shm_format, width, height,
                                    data=self.shm_data, stride=stride)
        self.surface.attach(self.buffer, 0, 0)
        self.width = width
        self.height = height

        if self.redraw_func:
            # This should invoke `redraw` which then invokes `surface.commit`
            self.redraw_func(self)
        else:
            self.surface.commit()

    def redraw(self):
        """Copy the whole window surface to the display"""
        self.add_damage()
        self.surface.commit()

    def add_damage(self, x=0, y=0, width=None, height=None):
        if width is None:
            width = self.width
        if height is None:
            height = self.height
        self.surface.damage(x, y, width, height)

    def pointer_motion(self, seat, time, x, y):
        pass

    def _xdg_surface_configure_handler(
            self, the_xdg_surface, serial):
        the_xdg_surface.ack_configure(serial)

        self.wait_for_configure = False
        if not self.surface.destroyed:
            self.resize(self.orig_width, self.orig_height)

class Seat:
    def __init__(self, obj, connection, global_name):
        self.c_enum = connection.interfaces['wl_seat'].enums['capability']
        self.s = obj
        self._c = connection
        self.global_name = global_name
        self.name = None
        self.capabilities = 0
        self.pointer = None
        self.keyboard = None
        self.s.dispatcher['capabilities'] = self._capabilities
        self.s.dispatcher['name'] = self._name
        self.tabsym = xkb.keysym_from_name("Tab")

    def removed(self):
        if self.pointer:
            self.pointer.release()
            self.pointer = None
        if self.keyboard:
            self.keyboard.release()
            del self.keyboard_state
            self.keyboard = None
        # ...that's odd, there's no request in the protocol to destroy
        # the seat proxy!  I suppose we just have to leave it lying
        # around.

    def _name(self, seat, name):
        print("Seat got name: {}".format(name))
        self.name = name

    def _capabilities(self, seat, c):
        print("Seat {} got capabilities: {}".format(self.name, c))
        self.capabilities = c
        pointer_available = c & self.c_enum['pointer']
        if pointer_available and not self.pointer:
            self.pointer = self.s.get_pointer()
            self.pointer.dispatcher['enter'] = self.pointer_enter
            self.pointer.dispatcher['leave'] = self.pointer_leave
            self.pointer.dispatcher['motion'] = self.pointer_motion
            self.pointer.silence['motion'] = True
            self.pointer.dispatcher['button'] = self.pointer_button
            self.pointer.dispatcher['axis'] = self.pointer_axis
            self.current_pointer_window = None
        if self.pointer and not pointer_available:
            self.pointer.release()
            self.current_pointer_window = None
            self.pointer = None
        keyboard_available = c & self.c_enum['keyboard']
        if keyboard_available and not self.keyboard:
            self.keyboard = self.s.get_keyboard()
            self.keyboard.dispatcher['keymap'] = self.keyboard_keymap
            self.keyboard.dispatcher['enter'] = self.keyboard_enter
            self.keyboard.dispatcher['leave'] = self.keyboard_leave
            self.keyboard.dispatcher['key'] = self.keyboard_key
            self.keyboard.dispatcher['modifiers'] = self.keyboard_modifiers
            self.current_keyboard_window = None
        if self.keyboard and not keyboard_available:
            self.keyboard.release()
            self.current_keyboard_window = None
            self.keyboard_state = None
            self.keyboard = None

    def pointer_enter(self, pointer, serial, surface, surface_x, surface_y):
        print("pointer_enter {} {} {} {}".format(
            serial, surface, surface_x, surface_y))
        self.current_pointer_window = self._c.surfaces.get(surface, None)
        pointer.set_cursor(serial, None, 0, 0)

    def pointer_leave(self, pointer, serial, surface):
        print("pointer_leave {} {}".format(serial, surface))
        self.current_pointer_window = None

    def pointer_motion(self, pointer, time, surface_x, surface_y):
        if not self.current_pointer_window:
            raise Exception("Pointer motion encountered even though there is not a matching window")
        self.current_pointer_window.pointer_motion(
            self, time, surface_x, surface_y)

    def pointer_button(self, pointer, serial, time, button, state):
        print("pointer_button {} {} {} {}".format(serial, time, button, state))
        if state == 1 and self.current_pointer_window:
            print("Seat {} starting shell surface move".format(self.name))
            self.current_pointer_window.xdg_toplevel.move(self.s, serial)

    def pointer_axis(self, pointer, time, axis, value):
        print("pointer_axis {} {} {}".format(time, axis, value))

    def keyboard_keymap(self, keyboard, format_, fd, size):
        print("keyboard_keymap {} {} {}".format(format_, fd, size))
        keymap_data = mmap.mmap(
            fd, size, prot=mmap.PROT_READ, flags=mmap.MAP_PRIVATE)
        os.close(fd)
        # The provided keymap appears to have a terminating NULL which
        # xkbcommon chokes on.  Specify length=size-1 to remove it.
        keymap = self._c.xkb_context.keymap_new_from_buffer(
            keymap_data, length=size - 1)
        keymap_data.close()
        self.keyboard_state = keymap.state_new()

    def keyboard_enter(self, keyboard, serial, surface, keys):
        print("keyboard_enter {} {} {}".format(serial, surface, keys))
        self.current_keyboard_window = self._c.surfaces.get(surface, None)

    def keyboard_leave(self, keyboard, serial, surface):
        print("keyboard_leave {} {}".format(serial, surface))
        self.current_keyboard_window = None

    def keyboard_key(self, keyboard, serial, time, key, state):
        print("keyboard_key {} {} {} {}".format(serial, time, key, state))
        sym = self.keyboard_state.key_get_one_sym(key + 8)
        if state == 1 and sym == self.tabsym:
            # Why did I put this in?!
            print("Saw a tab!")
        if state == 1:
            s = self.keyboard_state.key_get_string(key + 8)
            print("s={}".format(repr(s)))
            if s == "q":
                global shutdowncode
                shutdowncode = 0
            elif s == "c":
                # Close the window
                self.current_keyboard_window.close()
            elif s == "f":
                # Fullscreen toggle
                if self.current_keyboard_window.is_fullscreen:
                    self.current_keyboard_window.xdg_toplevel.unset_fullscreen()
                    self.current_keyboard_window.is_fullscreen = False
                    self.current_keyboard_window.resize(
                        self.current_keyboard_window.orig_width,
                        self.current_keyboard_window.orig_height)
                else:
                    self.current_keyboard_window.xdg_toplevel.set_fullscreen(None)
                    self.current_keyboard_window.is_fullscreen = True

    def keyboard_modifiers(self, keyboard, serial, mods_depressed,
                           mods_latched, mods_locked, group):
        print("keyboard_modifiers {} {} {} {} {}".format(
            serial, mods_depressed, mods_latched, mods_locked, group))
        self.keyboard_state.update_mask(mods_depressed, mods_latched,
                                        mods_locked, group, 0, 0)

class Output:
    def __init__(self, obj, connection, global_name):
        self.o = obj
        self._c = connection
        self.global_name = global_name
        self.o.dispatcher['geometry'] = self._geometry
        self.o.dispatcher['mode'] = self._mode
        self.o.dispatcher['done'] = self._done

    def _geometry(self, output, x, y, phy_width, phy_height, subpixel,
                  make, model, transform):
        print("Ouput: got geometry: x={}, y={}, phy_width={}, phy_height={},"
              "make={}, model={}".format(x, y, phy_width, phy_height,
                                         make, model))

    def _mode(self, output, flags, width, height, refresh):
        print("Output: got mode: flags={}, width={}, height={}, refresh={}" \
              .format(flags, width, height, refresh))

    def _done(self, output):
        print("Output: done for now")

class WaylandConnection:
    def __init__(self, wp_base, *other_wps):
        self.wps = (wp_base,) + other_wps
        self.interfaces = {}
        for wp in self.wps:
            for k,v in wp.interfaces.items():
                self.interfaces[k] = v
        
        # Create the Display proxy class from the protocol
        Display = MakeDisplay(wp_base)
        self.display = Display()

        self.registry = self.display.get_registry()
        self.registry.dispatcher['global'] = self.registry_global_handler
        self.registry.dispatcher['global_remove'] = \
            self.registry_global_remove_handler

        self.xkb_context = xkb.Context()

        # Dictionary mapping surface proxies to Window objects
        self.surfaces = {}

        self.compositor = None
        self.xdg_wm_base = None
        self.shm = None
        self.shm_formats = []
        self.seats = []
        self.outputs = []

        # Bind to the globals that we're interested in. NB we won't
        # pick up things like shm_formats at this point; after we bind
        # to wl_shm we need another roundtrip before we can be sure to
        # have received them.
        self.display.roundtrip()

        if not self.compositor:
            raise RuntimeError("Compositor not found")
        if not self.xdg_wm_base:
            raise RuntimeError("xdg_wm_base not found")
        if not self.shm:
            raise RuntimeError("Shm not found")

        # Pick up shm formats
        self.display.roundtrip()

        rdlist.append(self)
        preselectlist.append(self._preselect)

    def fileno(self):
        return self.display.get_fd()

    def disconnect(self):
        self.display.disconnect()

    def doread(self):
        self.display.recv()
        self.display.dispatch_pending()

    def _preselect(self):
        self.display.flush()

    def registry_global_handler(self, registry, name, interface, version):
        print("registry_global_handler: {} is {} v{}".format(
            name, interface, version))
        if interface == "wl_compositor":
            # We know up to and require version 3
            self.compositor = registry.bind(
                name, self.interfaces['wl_compositor'], 3)
        elif interface == "xdg_wm_base":
            # We know up to and require version 1
            self.xdg_wm_base = registry.bind(
                name, self.interfaces['xdg_wm_base'], 1)
        elif interface == "wl_shm":
            # We know up to and require version 1
            self.shm = registry.bind(
                name, self.interfaces['wl_shm'], 1)
            self.shm.dispatcher['format'] = self.shm_format_handler
        elif interface == "wl_seat":
            # We know up to and require version 4
            self.seats.append(Seat(registry.bind(
                name, self.interfaces['wl_seat'], 4), self, name))
        elif interface == "wl_output":
            # We know up to and require version 2
            self.outputs.append(Output(registry.bind(
                name, self.interfaces['wl_output'], 2), self, name))

    def registry_global_remove_handler(self, registry, name):
        # Haven't been able to get weston to send this event!
        print("registry_global_remove_handler: {} gone".format(name))
        for s in self.seats:
            if s.global_name == name:
                print("...it was a seat!  Releasing seat resources.")
                s.removed()

    def shm_format_handler(self, shm, format_):
        f = shm.interface.enums['format']
        if format_ == f.entries['argb8888'].value:
            self.shm_formats.append((format_, cairo.FORMAT_ARGB32))
        elif format_ == f.entries['xrgb8888'].value:
            self.shm_formats.append((format_, cairo.FORMAT_RGB24))
        elif format_ == f.entries['rgb565'].value:
            self.shm_formats.append((format_, cairo.FORMAT_RGB16_565))

def draw_in_window(w):
    ctx = cairo.Context(w.s)
    ctx.set_source_rgba(0,0,0,0)
    ctx.set_operator(cairo.OPERATOR_SOURCE)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)
    ctx.scale(w.width, w.height)
    pat = cairo.LinearGradient(0.0, 0.0, 0.0, 1.0)
    pat.add_color_stop_rgba(1, 0.7, 0, 0, 0.5)
    pat.add_color_stop_rgba(0, 0.9, 0.7, 0.2, 1)

    ctx.rectangle(0, 0, 1, 1)
    ctx.set_source(pat)
    ctx.fill()

    del pat

    ctx.translate(0.1, 0.1)

    ctx.move_to(0, 0)
    ctx.arc(0.2, 0.1, 0.1, -math.pi/2, 0)
    ctx.line_to(0.5, 0.1)
    ctx.curve_to(0.5, 0.2, 0.5, 0.4, 0.2, 0.8)
    ctx.close_path()

    ctx.set_source_rgb(0.3, 0.2, 0.5)
    ctx.set_line_width(0.02)
    ctx.stroke()

    ctx.select_font_face("monospace")
    ctx.set_font_size(0.05)
    ctx.set_source_rgb(1.0, 1.0, 1.0)
    ctx.move_to(0.2, 0.2)
    ctx.show_text("{} {} x {}".format(w.title, w.width, w.height))

    del ctx

    w.s.flush()
    w.redraw()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Load the main Wayland protocol.
    wp_base = wayland.protocol.Protocol("/usr/share/wayland/wayland.xml")
    wp_xdg_shell = wayland.protocol.Protocol("/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml")

    try:
        conn = WaylandConnection(wp_base, wp_xdg_shell)
    except FileNotFoundError as e:
        if e.errno == 2:
            print("Unable to connect to the compositor - "
                  "is one running?")
            sys.exit(1)
        raise
    w1 = Window(conn, 640, 480, title="Window 1", redraw=draw_in_window)
    w2 = Window(conn, 320, 240, title="Window 2", redraw=draw_in_window)
    w3 = Window(conn, 160, 120, title="Window 3", redraw=draw_in_window)

    eventloop()

    w1.close()
    w2.close()
    w3.close()

    conn.display.roundtrip()
    conn.disconnect()
    print("About to exit with code {}".format(shutdowncode))

    logging.shutdown()
    sys.exit(shutdowncode)
