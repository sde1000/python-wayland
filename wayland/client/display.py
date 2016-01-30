import wayland.protocol
import wayland.utils
import os
import socket
import select
import struct
import array
import io
import logging

log = logging.getLogger(__name__)

class ServerDisconnected(Exception):
    """The server disconnected unexpectedly"""
    pass

class ProtocolError(Exception):
    """The server sent data that could not be decoded"""
    pass

class UnknownObjectError(Exception):
    """The server sent an event for an object we don't know about"""
    def __init__(self, oid):
        self.oid = oid
    def __str__(self):
        return "UnknownObjectError({})".format(self.oid)

class DisplayError(Exception):
    """The server sent a fatal error event

    This error can be raised during dispatching of the default queue.
    """
    def __init__(self, obj, code, codestr, message):
        self.obj = obj
        self.code = code
        self.codestr = codestr
        self.message = message
    def __str__(self):
        return "DisplayError({}, {} (\"{}\"), {})".format(
            obj, code, codestr, message)

class Display(wayland.protocol.wayland.interfaces['wl_display'].proxy_class):
    def __init__(self, name_or_fd=None):
        self._f = None
        self._oids = iter(range(1, 0xff000000))
        self._reusable_oids = []
        self._default_queue = []
        super(Display, self).__init__(self, self.get_new_oid(),
                                      self._default_queue)
        if hasattr(name_or_fd, 'fileno'):
            self._f = name_or_fd
            log.info("connected to existing fd %d", self._f)
        else:
            xdg_runtime_dir = os.getenv('XDG_RUNTIME_DIR')
            if not xdg_runtime_dir:
                raise wayland.utils.NoXDGRuntimeDir()
            if not name_or_fd:
                display = os.getenv('WAYLAND_DISPLAY')
                if not display:
                    display = "wayland-0"
            path = os.path.join(xdg_runtime_dir, display)
            self._f = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
            self._f.connect(path)
            log.info("connected to %s", path)

        self._f.setblocking(0)

        # Partial event left from last read
        self._read_partial_event = b''
        self._incoming_fds = []

        self.objects = {1: self}
        self._send_queue = []

        self.dispatcher['delete_id'] = self._delete_id
        self.silence['delete_id'] = True
        self.dispatcher['error'] = self._error_event

    def __del__(self):
        self.disconnect()

    def connect(self):
        pass # For compatibility with pywayland

    def disconnect(self):
        if self._f:
            self._f.close()
            self._f = None

    def get_fd(self):
        return self._f.fileno()

    def get_new_oid(self):
        if self._reusable_oids:
            return self._reusable_oids.pop()
        return next(self._oids)

    def _delete_id(self, display, id_):
        log.info("deleting %s", self.objects[id_])
        del self.objects[id_]
        self._reusable_oids.append(id_)

    def _error_event(self, obj, code, message):
        # XXX look up string for error code in enum
        raise DisplayError(obj, code, "", message)

    def queue_request(self, r, fds=[]):
        log.debug("queueing to send: %s with fds %s", r, fds)
        self._send_queue.append((r,fds))

    def flush(self):
        """Send buffered requests to the display server.

        Will send as many requests as possible to the display server.
        Will not block; if sendmsg() would block, will leave events in
        the queue.

        Returns True if the queue was emptied.
        """
        while self._send_queue:
            b, fds = self._send_queue.pop(0)
            try:
                self._f.sendmsg([b], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                       array.array("i", fds))])
                for fd in fds:
                    os.close(fd)
            except socket.error as e:
                if socket.errno == 11:
                    # Would block.  Return the data to the head of the queue
                    # and try again later!
                    log.debug("flush would block; returning data to queue")
                    self._send_queue.insert(0, (b, fds))
                    return
                raise
        return True

    def recv(self):
        # Receive as much data as is available.  Returns True if any
        # data was received.
        data = None
        try:
            fds = array.array("i")
            data, ancdata, msg_flags, address = self._f.recvmsg(
                1024, socket.CMSG_SPACE(16*fds.itemsize))
            for cmsg_level, cmsg_type, cmsg_data in ancdata:
                if (cmsg_level == socket.SOL_SOCKET and
                    cmsg_type == socket.SCM_RIGHTS):
                    fds.fromstring(cmsg_data[
                        :len(cmsg_data) - (len(cmsg_data) % fds.itemsize)])
            self._incoming_fds.extend(fds)
            if data:
                self.decode(data)
                return True
            else:
                raise ServerDisconnected()
        except socket.error as e:
            if e.errno == 11:
                # No data available; would otherwise block
                return
            raise

    def dispatch(self):
        # Dispatch the default event queue.

        # If the queue is empty, block until events are available and
        # dispatch them.
        self.flush()
        while not self._default_queue:
            select.select([self._f], [], [])
            self.recv()
        self.dispatch_pending()

    def dispatch_pending(self):
        # Dispatch pending events from the default event queue,
        # without reading any further events from the socket.
        while self._default_queue:
            e = self._default_queue.pop(0)
            if isinstance(e, Exception):
                raise e
            proxy, event, args = e
            proxy.dispatch_event(event, args)

    def roundtrip(self):
        # Block until all pending requests are processed by the server
        ready = False
        def set_ready(callback, x):
            nonlocal ready
            ready = True
        l = self.sync()
        l.dispatcher['done'] = set_ready
        while not ready:
            self.dispatch()

    def decode(self, data):
        # There may be partial event data already received; add to it
        # if it's there
        if self._read_partial_event:
            data = self._read_partial_event + data
        while len(data) > 8:
            oid, sizeop = struct.unpack("II", data[0:8])
            
            size = sizeop >> 16
            op = sizeop & 0xffff

            if len(data) < size:
                log.debug("partial event received: %d byte event, "
                          "%d bytes available", size, len(data))
                break

            argdata = io.BytesIO(data[8:size])
            data = data [size:]

            obj = self.objects.get(oid, None)
            if obj:
                with argdata:
                    e = obj._unmarshal_event(op, argdata, self._incoming_fds)
                    log.debug("queueing event: %s(%d) %s %s",
                              e[0].interface.name, e[0]._oid, e[1].name, e[2])
                    obj.queue.append(e)
            else:
                obj.queue.append(UnknownObjectError(obj))
        self._read_partial_event = data
