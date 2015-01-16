#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright (c) 2014 clowwindy
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# from ssloop
# https://github.com/clowwindy/ssloop

from __future__ import absolute_import, division, print_function, \
    with_statement

import os
import socket
import select
import errno
import logging
from collections import defaultdict


__all__ = ['EventLoop', 'POLL_NULL', 'POLL_IN', 'POLL_OUT', 'POLL_ERR',
           'POLL_HUP', 'POLL_NVAL', 'EVENT_NAMES']

POLL_NULL = 0x00
POLL_IN = 0x01
POLL_OUT = 0x04
POLL_ERR = 0x08
POLL_HUP = 0x10
POLL_NVAL = 0x20


EVENT_NAMES = {
    POLL_NULL: 'POLL_NULL',
    POLL_IN: 'POLL_IN',
    POLL_OUT: 'POLL_OUT',
    POLL_ERR: 'POLL_ERR',
    POLL_HUP: 'POLL_HUP',
    POLL_NVAL: 'POLL_NVAL',
}

# epoll is a Linux kernel system call, a scalable I/O event notification mechanism.
# It first introduced in Linux kernel 2.5.44
# So we can only use it under Linux.
class EpollLoop(object):

    def __init__(self):
        self._epoll = select.epoll()

    def poll(self, timeout):
        return self._epoll.poll(timeout)

    def add_fd(self, fd, mode):
        self._epoll.register(fd, mode)

    def remove_fd(self, fd):
        self._epoll.unregister(fd)

    def modify_fd(self, fd, mode):
        self._epoll.modify(fd, mode)

# Kqueue is a scalable event notification interface introduced in FreeBSD 4.1,
# It also supported in NetBSD, OpenBSD, DragonflyBSD, and Mac OS X.
# So we can only use it under Unix System.
class KqueueLoop(object):

    MAX_EVENTS = 1024

    def __init__(self):
        self._kqueue = select.kqueue()
        self._fds = {}

    def _control(self, fd, mode, flags):
        events = []
        if mode & POLL_IN:
            events.append(select.kevent(fd, select.KQ_FILTER_READ, flags))
        if mode & POLL_OUT:
            events.append(select.kevent(fd, select.KQ_FILTER_WRITE, flags))
        for e in events:
            self._kqueue.control([e], 0)

    def poll(self, timeout):
        if timeout < 0:
            timeout = None  # kqueue behaviour
        events = self._kqueue.control(None, KqueueLoop.MAX_EVENTS, timeout)
        results = defaultdict(lambda: POLL_NULL)
        for e in events:
            fd = e.ident
            if e.filter == select.KQ_FILTER_READ:
                results[fd] |= POLL_IN
            elif e.filter == select.KQ_FILTER_WRITE:
                results[fd] |= POLL_OUT
        return results.items()

    def add_fd(self, fd, mode):
        self._fds[fd] = mode
        self._control(fd, mode, select.KQ_EV_ADD)

    def remove_fd(self, fd):
        self._control(fd, self._fds[fd], select.KQ_EV_DELETE)
        del self._fds[fd]

    def modify_fd(self, fd, mode):
        self.remove_fd(fd)
        self.add_fd(fd, mode)


#rlist -- wait until ready for reading
#wlist -- wait until ready for writing
#xlist -- wait for an ``exceptional condition''

class SelectLoop(object):

    def __init__(self):
        # set is unordered and the elements in it can't be repeated
        self._r_list = set()
        self._w_list = set()
        self._x_list = set()

    def poll(self, timeout):
        r, w, x = select.select(self._r_list, self._w_list, self._x_list,
                                timeout)
        results = defaultdict(lambda: POLL_NULL)
        for p in [(r, POLL_IN), (w, POLL_OUT), (x, POLL_ERR)]:
            for fd in p[0]:
                results[fd] |= p[1]
        return results.items()

    def add_fd(self, fd, mode):
        if mode & POLL_IN:
            self._r_list.add(fd)
        if mode & POLL_OUT:
            self._w_list.add(fd)
        if mode & POLL_ERR:
            self._x_list.add(fd)

    def remove_fd(self, fd):
        if fd in self._r_list:
            self._r_list.remove(fd)
        if fd in self._w_list:
            self._w_list.remove(fd)
        if fd in self._x_list:
            self._x_list.remove(fd)

    def modify_fd(self, fd, mode):
        self.remove_fd(fd)
        self.add_fd(fd, mode)


class EventLoop(object):
    def __init__(self):
        self._iterating = False
        # look for the attribute of object.
        # There are 3 different implement of eventloop in this file, just select one of them ~
        if hasattr(select, 'epoll'):
            self._impl = EpollLoop()
            model = 'epoll'
        elif hasattr(select, 'kqueue'):
            self._impl = KqueueLoop()
            model = 'kqueue'
        elif hasattr(select, 'select'):
            self._impl = SelectLoop()
            model = 'select'
        else:
            raise Exception('can not find any available functions in select '
                            'package')

        self._fd_to_f = {} # It's dictionary, just think abort a set of key-value elements
        self._handlers = [] # The list contains all of handlers.
        self._ref_handlers = [] # The list contains all ref_handlers.
        self._handlers_to_remove = [] # The list contains the handlers that we want to remove.

        logging.debug('using event model: %s', model)

    def poll(self, timeout=None):
        events = self._impl.poll(timeout)
        return [(self._fd_to_f[fd], fd, event) for fd, event in events]

    def add(self, f, mode):
        # we can think fd is the id of "f" --- the file that we want to listen
        # Since "Every think is a file", so we use fileno() to get the id of "f"
        fd = f.fileno()
        self._fd_to_f[fd] = f
        self._impl.add_fd(fd, mode)

    def remove(self, f):
        fd = f.fileno()
        del self._fd_to_f[fd]
        self._impl.remove_fd(fd)

    def modify(self, f, mode):
        fd = f.fileno()
        self._impl.modify_fd(fd, mode)

    # There are two kind of handler in the case.
    # One should be the instance of handler, the other is the reference of handler
    def add_handler(self, handler, ref=True):
        self._handlers.append(handler)
        if ref:
            # when all ref handlers are removed, loop stops
            self._ref_handlers.append(handler)

    def remove_handler(self, handler):
        # If the handler is one of the reference of handlers, we should remove it at once.
        if handler in self._ref_handlers:
            self._ref_handlers.remove(handler)

        # "Iterating" is a lock of handler list
        # If it has been locked, that means something is reading or writting it
        if self._iterating:
            self._handlers_to_remove.append(handler)
        # If not, then we can remove handler in the list at once.
        else:
            self._handlers.remove(handler)

    # Now we can start the loop ~
    def run(self):
        events = []
        while self._ref_handlers: # make sure there is at least one ref_handler in the handler list.
            try:
                # At start , we should get some event, just poll out them, let our see what's happenging ~
                # Maybe these are many events have happened, just poll them.
                events = self.poll(1)
                # If there is something wrong...
            except (OSError, IOError) as e:
                if errno_from_exception(e) in (errno.EPIPE, errno.EINTR):
                    # EPIPE: Happens when the client closes the connection
                    # EINTR: Happens when received a signal
                    # handles them as soon as possible
                    logging.debug('poll:%s', e)
                else:
                    logging.error('poll:%s', e)
                    import traceback
                    traceback.print_exc()
                    continue
            self._iterating = True
            # Now we can handler all of the events that have happened.
            for handler in self._handlers:
                # TODO when there are a lot of handlers
                try:
                    # We tell every handler in the list that XXXXX event is happening ~
                    handler(events)
                except (OSError, IOError) as e:
                    logging.error(e)
                    import traceback
                    traceback.print_exc()
            # Remove the handlers that we want to remove.
            if self._handlers_to_remove:
                for handler in self._handlers_to_remove:
                    self._handlers.remove(handler)
                self._handlers_to_remove = []
            self._iterating = False


# from tornado
def errno_from_exception(e):
    """Provides the errno from an Exception object.

    There are cases that the errno attribute was not set so we pull
    the errno out of the args but if someone instatiates an Exception
    without any args you will get a tuple error. So this function
    abstracts all that behavior to give you a safe way to get the
    errno.
    """

    if hasattr(e, 'errno'):
        return e.errno
    elif e.args:
        return e.args[0]
    else:
        return None


# from tornado
def get_sock_error(sock):
    error_number = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    return socket.error(error_number, os.strerror(error_number))
