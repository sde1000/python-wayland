import wayland.protocol
import wayland.utils
import os
import socket
import queue
import struct
import array
import io

class Display(wayland.protocol.wayland.interfaces['wl_display'].proxy_class):
    def __init__(self, name_or_fd=None):
        self._f = None
        self._oids = iter(range(1, 0xff000000))
        self._reusable_oids = []
        self._default_queue = queue.Queue()
        super(Display, self).__init__(self, self.get_new_oid(),
                                      self._default_queue)
        if hasattr(name_or_fd, 'fileno'):
            self._f = name_or_fd
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

        self.objects = {1: self}
        self._send_queue = queue.Queue()

        self.dispatcher['delete_id'] = self._delete_id

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
        print("Deleting id {} ({})".format(id_, self.objects[id_]))
        del self.objects[id_]
        self._reusable_oids.append(id_)

    def queue_request(self, r, fds=[]):
        #print("Queueing to send: {} with fds {}".format(repr(r), fds))
        self._send_queue.put((r,fds))

    def dispatch(self):
        # Dispatch the default event queue
        # If the queue is empty, block until events are available
        self.flush() # really?
        self.recv()
        self.dispatch_pending()

    def dispatch_pending(self):
        try:
            while True:
                proxy, event, args = self._default_queue.get(False)
                proxy.dispatch_event(event, args)
        except queue.Empty:
            pass
        
    def flush(self):
        try:
            while True:
                b, fds = self._send_queue.get(False)
                self._f.sendmsg([b], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                       array.array("i", fds))])
                for fd in fds:
                    os.close(fd)
        except queue.Empty:
            pass

    def roundtrip(self):
        # Block until all pending requests are processed by the server
        ready = False
        def set_ready(callback, x):
            nonlocal ready
            ready = True
        l = self.sync()
        l.dispatcher['done'] = set_ready
        self.flush()
        while not ready:
            self.flush()
            self.dispatch()

    def recv(self):
        # Isn't this a blocking call?
        fds = array.array("i")
        data, ancdata, msg_flags, address = self._f.recvmsg(
            1024, socket.CMSG_SPACE(16*fds.itemsize))
        #print("Received: data={} ancdata={}".format(repr(data),repr(ancdata)))
        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            if (cmsg_level == socket.SOL_SOCKET and
                cmsg_type == socket.SCM_RIGHTS):
                fds.fromstring(cmsg_data[
                    :len(cmsg_data) - (len(cmsg_data) % fds.itemsize)])
        fds = list(fds)
        if data:
            self.decode(data, fds)
        else:
            raise RuntimeError

    def decode(self, data, fds):
        # Quick and dirty unpack for now - update to use buffer later
        fd_source = iter(fds)
        while len(data) > 8:
            # We assume the data starts with a message
            oid, sizeop = struct.unpack("II", data[0:8])
            
            size = sizeop >> 16
            op = sizeop & 0xffff

            if len(data) < size:
                print("Partial event received: {} byte event, "
                      "{} bytes available".format(size, len(data)))
                return

            argdata = io.BytesIO(data[8:size])
            data = data [size:]

            obj = self.objects.get(oid, None)
            if not obj:
                print("Received event for unknown oid {}".format(oid))
                continue

            with argdata:
                e = obj._unmarshal_event(op, argdata, fd_source)
                print("Decoded event: {}({}) {} {}".format(
                    e[0].interface.name, e[0]._oid, e[1].name, e[2]))
                obj.queue.put(e)
