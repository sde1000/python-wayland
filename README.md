Pure Python 3 Wayland protocol implementation
=============================================

Very much a work in progress; do not use, the API is almost certainly
going to change, there's no test suite and no documentation!

Doesn't wrap libwayland, instead reads Wayland protocol description
XML files and speaks the Wayland wire protocol directly.

Requires python 3, because python 2 doesn't have socket.sendmsg() and
socket.recvmsg() which are required for fd passing.

See also: https://github.com/flacjacket/pywayland - I may aim for API
compatibility with this.
