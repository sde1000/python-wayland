from __future__ import print_function, unicode_literals

from lxml import etree
import struct
import os

def _int_or_none(i):
    if i is None:
        return
    return int(i)

def _description(d):
    assert d.tag == "description"
    return d.text, d.get('summary')

class NullArgumentException(Exception):
    """None was passed where a value was expected"""
    pass

class Proxy(object):
    def __init__(self, display, oid, queue):
        self._display = display
        self._oid = oid
        self.queue = queue
        self.dispatcher = {}
    def _marshal_request(self, request, *args):
        # args is a tuple when called; we make it a list so it's mutable,
        # because args are consumed in the 'for' loop
        args = list(args)
        al = []
        rval = None
        fl = []
        for a in request.args:
            b, r, fds = a.marshal(args, self._display)
            al.append(b)
            fl = fl + fds
            rval = rval or r
        al = bytes().join(al)
        b = struct.pack('II', self._oid, ((len(al) + 8) << 16) | request.opcode)
        self._display.queue_request(b + al, fl)
        return rval
    def _unmarshal_event(self, opcode, argdata, fd_source):
        event = self.interface.events_by_number[opcode]
        args = []
        for arg in event.args:
            v = arg.unmarshal(argdata, fd_source, self._display)
            args.append(v)
        return (self, event, args)
    def set_queue(self, new_queue):
        # Sets the queue for events received from this object
        self.queue = new_queue
    def dispatch_event(self, event, args):
        f = self.dispatcher.get(event.name, None)
        if f:
            f(self, *args)
    def __str__(self):
        return "{}({})".format(self.interface.name, self._oid)
    def __repr__(self):
        return "{}({})".format(self.__class__.__name__, self._oid)

class Arg(object):
    def __init__(self, parent, arg):
        self.parent = parent

        self.name = arg.get('name')
        self.type = arg.get('type')

        self.description = None
        self.summary = arg.get('summary', None)
        self.interface = arg.get('interface', None)
        self.allow_null = (arg.get('allow-null', None) == "true")

        for c in arg:
            if c.tag == "description":
                self.description, self.summary = _description(c)

    def marshal(self, args, objmap):
        """Marshal the argument

        args is the list of arguments still to marshal; this call
        removes the appropriate number of items from args

        objmap is an object that implements a get_new_oid() function
        and has an objects dictionary

        The return value is a tuple of (bytes, optional return value,
        list of fds to send)
        """
        print("Unimplemented marshal of {}".format(self.type))
        raise RuntimeError
    def unmarshal(self, argdata, fd_source, objmap):
        """Unmarshal the argument

        argdata is a file-like object providing access to the
        remaining marshalled arguments; this call will consume the
        appropriate number of bytes from this source

        fd_source is an iterator object supplying fds that have been
        received over the connection

        objmap is an object that implements a get_new_oid() function
        and has an objects dictionary

        The return value is the value of the argument
        """
        print("Unimplemented unmarshal of {}".format(self.type))
        raise RuntimeError

class Arg_int(Arg):
    """Signed 32-bit integer argument"""
    def marshal(self, args, objmap):
        v = args.pop(0)
        return struct.pack('i', v), None, []
    def unmarshal(self, argdata, fd_source, objmap):
        (v, ) = struct.unpack("i", argdata.read(4))
        return v

class Arg_uint(Arg):
    """Unsigned 32-bit integer argument"""
    def marshal(self, args, objmap):
        v = args.pop(0)
        return struct.pack('I', v), None, []
    def unmarshal(self, argdata, fd_source, objmap):
        (v, ) = struct.unpack("I", argdata.read(4))
        return v

class Arg_new_id(Arg):
    """Newly created object argument"""
    def marshal(self, args, objmap):
        nid = objmap.get_new_oid()
        if self.parent.creates:
            # The interface type and version are determined by the
            # request
            npc = self.parent.interface.protocol.interfaces[self.parent.creates].proxy_class
            b = struct.pack('I', nid)
        else:
            # The interface and version are supplied by the caller,
            # and the argument is marshalled as string,uint32,uint32
            interface = args.pop(0)
            version = args.pop(0)
            npc = interface.proxy_class
            iname = interface.name.encode('utf-8')
            parts = (struct.pack('I',len(iname)+1),
                     iname,
                     b'\x00'*(4-(len(iname) % 4)),
                     struct.pack('II',version,nid))
            b = b''.join(parts)
        # XXX this works in a client, but I assume we need to do
        # something different if we are marshalling this type when the
        # server is sending an event to a client?
        new_proxy = npc(objmap, nid, objmap._default_queue)
        objmap.objects[nid] = new_proxy
        return b, new_proxy, []

class Arg_string(Arg):
    """String argument"""
    def marshal(self, args, objmap):
        estr = args.pop(0).encode('utf-8')
        parts = (struct.pack('I',len(estr)+1),
                 estr,
                 b'\x00'*(4-(len(estr) % 4)))
        return b''.join(parts), None, []
    def unmarshal(self, argdata, fd_source, objmap):
        # The length includes the terminating null byte
        (l, ) = struct.unpack("I", argdata.read(4))
        assert l > 0
        l = l-1
        s = argdata.read(l).decode('utf-8')
        argdata.read(4 - (l % 4))
        return s

class Arg_object(Arg):
    """Existing object argument"""
    def marshal(self, args, objmap):
        v = args.pop(0)
        if v:
            oid = v._oid
        else:
            if self.allow_null:
                oid = 0
            else:
                raise NullArgumentException()
        return struct.pack("I", oid), None, []
    def unmarshal(self, argdata, fd_source, objmap):
        (v, ) = struct.unpack("I", argdata.read(4))
        return objmap.objects[v]

class Arg_fd(Arg):
    """File descriptor argument"""
    def marshal(self, args, objmap):
        v = args.pop(0)
        fd = os.dup(v)
        return b'', None, [fd]
    def unmarshal(self, argdata, fd_source, objmap):
        return next(fd_source)

class Arg_fixed(Arg):
    """Signed 24.8 decimal number argument"""
    # XXX not completely sure I've understood the format here - in
    # particular, is it (as the protocol description says) a sign bit
    # followed by 23 bits of integer precision and 8 bits of decimal
    # precision, or is it 24 bits of 2's complement integer precision
    # followed by 8 bits of decimal precision?  I've assumed the
    # latter because it seems to work!
    def marshal(self, args, objmap):
        v = args.pop(0)
        if isinstance(v, int):
            m = v << 8
        else:
            m = (int(v) << 8) + int((v % 1.0) * 256)
        return struct.pack("i",m), None, []
    def unmarshal(self, argdata, fd_source, objmap):
        b = argdata.read(4)
        (m, ) = struct.unpack("i",b)
        return float(m >> 8) + ((m & 0xff) / 256.0)

class Arg_array(Arg):
    """Array argument"""
    # This appears to be very similar to a string, except without any
    # zero termination.  Interpretation of the contents of the array
    # is request- or event-dependent.
    def marshal(self, args, objmap):
        v = args.pop(0)
        # v should be bytes
        parts = (struct.pack('I',len(v)),
                 estr,
                 b'\x00'*(3 - ((len(v) - 1) % 4)))
        return b''.join(parts), None, []
    def unmarshal(self, argdata, fd_source, objmap):
        (l, ) = struct.unpack("I", argdata.read(4))
        v = argdata.read(l)
        pad = 3 - ((l - 1) % 4)
        if pad:
            argdata.read(pad)
        return v

def make_arg(parent, tag):
    t = tag.get("type")
    c = "Arg_"+tag.get("type")
    return globals()[c](parent, tag)

class Request(object):
    def __init__(self, interface, opcode, request):
        self.interface = interface
        self.opcode = opcode
        assert request.tag == "request"

        self.name = request.get('name')
        self.type = request.get('type', None)
        self.since = _int_or_none(request.get('since', None))

        self.is_destructor = (self.type == "destructor")

        self.description = None
        self.summary = None

        self.creates = None
        
        self.args = []

        for c in request:
            if c.tag == "description":
                self.description, self.summary = _description(c)
            elif c.tag == "arg":
                a = make_arg(self, c)
                if a.type == "new_id":
                    self.creates = a.interface
                self.args.append(a)

    def __str__(self):
        return "{}.{}".format(self.interface.name,self.name)

    def __call__(self, proxy, *args):
        r = proxy._marshal_request(self, *args)
        print("Request {}({}): {}{} returns {}".format(
            proxy.interface.name, proxy._oid, self.name, args, str(r)))
        return r

class Event(object):
    def __init__(self, interface, event):
        self.interface = interface
        assert event.tag == "event"

        self.name = event.get('name')
        self.since = _int_or_none(event.get('since', None))
        self.args = []
        self.description = None
        self.summary = None

        for c in event:
            if c.tag == "description":
                self.description, self.summary = _description(c)
            elif c.tag == "arg":
                self.args.append(make_arg(self, c))
    def __str__(self):
        return "Event {} on {}".format(self.name, self.interface)

class Entry(object):
    def __init__(self, enum, entry):
        self.enum = enum
        assert entry.tag == "entry"

        self.name = entry.get('name')
        self.value = int(entry.get('value'), base=0)
        self.description = None
        self.summary = entry.get('summary', None)
        self.since = _int_or_none(entry.get('since', None))

        for c in entry:
            if c.tag == "description":
                self.description, self.summary = _description(c)

class Enum(object):
    def __init__(self, interface, enum):
        self.interface = interface
        assert enum.tag == "enum"

        self.name = enum.get('name')
        self.since = _int_or_none(enum.get('since', None))
        self.entries = {}
        self.description = None
        self.summary = None

        for c in enum:
            if c.tag == "description":
                self.description, self.summary = _description(c)
            elif c.tag == "entry":
                e = Entry(self, c)
                self.entries[e.name] = e

class Interface(object):
    def __init__(self, protocol, interface):
        self.protocol = protocol
        assert interface.tag == "interface"

        self.name = interface.get('name')
        self.version = int(interface.get('version'))
        self.requests = {}
        self.events_by_name = {}
        self.events_by_number = []
        self.enums = {}

        for c in interface:
            if c.tag == "description":
                self.description, self.summary = _description(c)
            elif c.tag == "request":
                e = Request(self, len(self.requests), c)
                self.requests[e.name] = e
            elif c.tag == "event":
                e = Event(self, c)
                self.events_by_name[e.name] = e
                self.events_by_number.append(e)
            elif c.tag == "enum":
                e = Enum(self, c)
                self.enums[e.name] = e

        def add_proxy_arg(x):
            def call_request(*args):
                return x(*args)
            return call_request
        d = {
            '__doc__': self.description,
            'interface': self,
            }
        for r in self.requests.values():
            d[r.name] = add_proxy_arg(r)
        self.proxy_class = type(str(self.name+'_proxy'), (Proxy,), d)

class Protocol(object):
    def __init__(self, filename, dtdfile=None):
        parser = etree.XMLParser(dtd_validation=True)
        dtd = None
        if dtdfile:
            with open(dtdfile) as f:
                dtd = etree.DTD(f)
        tree = etree.ElementTree(file=filename)
        if dtd:
            dtd.validate(tree)

        protocol = tree.getroot()
        assert protocol.tag == "protocol"
        
        self.copyright = None
        self.interfaces = {}

        self.name = protocol.get('name')

        for c in protocol:
            if c.tag == "copyright":
                self.copyright = c.text
            elif c.tag == "interface":
                i = Interface(self, c)
                self.interfaces[i.name] = i
        
wayland = Protocol("/usr/share/wayland/wayland.xml",
                   "/usr/share/wayland/wayland.dtd")
