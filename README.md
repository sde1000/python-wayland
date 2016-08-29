Pure Python 3 Wayland protocol implementation
=============================================

Very much a work in progress; do not use, the API is almost certainly
going to change, there's no test suite and no documentation!

Doesn't wrap libwayland, instead reads Wayland protocol description
XML files and speaks the Wayland wire protocol directly.

Requires python 3, because python 2 doesn't have
```socket.sendmsg()``` and ```socket.recvmsg()``` which are required
for fd passing.

See also
========

https://github.com/flacjacket/pywayland - I am not aiming for API
compatibility with this because I expect the libraries to be used in
different circumstances.  Use this one if you want to use the Wayland
protocol with as few external dependencies as possible, and if you
want to keep control of your event loop: it should integrate well with
async libraries like https://twistedmatrix.com/

pywayland is a more appropriate choice if you're integrating with
other libraries that expect to see a C ```struct wl_display *```
