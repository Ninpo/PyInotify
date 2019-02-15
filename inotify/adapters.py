import logging
import select
import os
import struct
import collections
import time

from errno import EINTR

import fcntl
import inotify.constants
import inotify.calls

# Constants.

_DEFAULT_EPOLL_BLOCK_DURATION_S = 1
_HEADER_STRUCT_FORMAT = "iIII"

_DEFAULT_TERMINAL_EVENTS = ("IN_Q_OVERFLOW", "IN_UNMOUNT")

# Globals.

_LOGGER = logging.getLogger(__name__)

_INOTIFY_EVENT = collections.namedtuple(
    "_INOTIFY_EVENT", ["wd", "mask", "cookie", "len"]
)

_STRUCT_HEADER_LENGTH = struct.calcsize(_HEADER_STRUCT_FORMAT)
_IS_DEBUG = bool(int(os.environ.get("DEBUG", "0")))


class EventTimeoutException(Exception):
    pass


class TerminalEventException(Exception):
    def __init__(self, type_name, event):
        super(TerminalEventException, self).__init__(type_name)
        self.event = event


class Inotify(object):
    def __init__(self, paths=[], block_duration_s=_DEFAULT_EPOLL_BLOCK_DURATION_S):
        self.__block_duration = block_duration_s
        self.__watches = {}
        self.__watches_r = {}
        self.__buffer = b""

        self.__inotify_fd = inotify.calls.inotify_init()
        _LOGGER.debug("Inotify handle is (%d).", self.__inotify_fd)

        flag = fcntl.fcntl(self.__inotify_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.__inotify_fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)
        self.__epoll = select.epoll()
        self.__epoll.register(self.__inotify_fd, select.POLLIN)

        self.__last_success_return = None

        for path in paths:
            self.add_watch(path)

    def __get_block_duration(self):
        """Allow the block-duration to be an integer or a function-call."""

        try:
            return self.__block_duration()
        except TypeError:
            # A scalar value describing seconds.
            return self.__block_duration

    def __del__(self):
        _LOGGER.debug("Cleaning-up inotify.")
        os.close(self.__inotify_fd)

    def add_watch(self, path_unicode, mask=inotify.constants.IN_ALL_EVENTS):
        _LOGGER.debug("Adding watch: [%s]", path_unicode)

        # Because there might be race-conditions in the recursive handling (see
        # the notes in the documentation), we recommend to add watches using
        # data from a secondary channel, if possible, which means that we might
        # then be adding it, yet again, if we then receive it in the normal
        # fashion afterward.
        if path_unicode in self.__watches:
            _LOGGER.warning("Path already being watched: [%s]", path_unicode)
            return

        path_bytes = path_unicode.encode("utf8")

        wd = inotify.calls.inotify_add_watch(self.__inotify_fd, path_bytes, mask)
        _LOGGER.debug("Added watch (%d): [%s]", wd, path_unicode)

        self.__watches[path_unicode] = wd
        self.__watches_r[wd] = path_unicode

        return wd

    def _remove_watch(self, wd, path, superficial=False):
        _LOGGER.debug("Removing watch for watch-handle (%d): [%s]", wd, path)

        if superficial is not None:
            del self.__watches[path]
            del self.__watches_r[wd]
            inotify.adapters._LOGGER.debug(".. removed from adaptor")
        if superficial is not False:
            return
        inotify.calls.inotify_rm_watch(self.__inotify_fd, wd)
        _LOGGER.debug(".. removed from inotify")

    def remove_watch(self, path, superficial=False):
        """Remove our tracking information and call inotify to stop watching
        the given path. When a directory is removed, we'll just have to remove
        our tracking since inotify already cleans-up the watch.
        With superficial set to None it is also possible to remove only inotify
        watch to be able to wait for the final IN_IGNORED event received for
        the wd (useful for example in threaded applications).
        """

        wd = self.__watches.get(path)
        if wd is None:
            _LOGGER.warning("Path not in watch list: [%s]", path)
            return
        self._remove_watch(wd, path, superficial)

    def remove_watch_with_id(self, wd, superficial=False):
        """Same as remove_watch but does the same by id"""
        path = self.__watches_r.get(wd)
        if path is None:
            _LOGGER.warning("Watchdescriptor not in watch list: [%d]", wd)
            return
        self._remove_watch(wd, path, superficial)

    def _get_event_names(self, event_type):
        try:
            return inotify.constants.MASK_LOOKUP_COMB[event_type][:]
        except KeyError as ex:
            raise AssertionError(
                "We could not resolve all event-types (%x)" % event_type
            )

    def _handle_inotify_event(self, wd):
        """Handle a series of events coming-in from inotify."""

        b = os.read(wd, 1024)
        if not b:
            return

        self.__buffer += b

        while 1:
            length = len(self.__buffer)

            if length < _STRUCT_HEADER_LENGTH:
                _LOGGER.debug("Not enough bytes for a header.")
                return

            # We have, at least, a whole-header in the buffer.

            peek_slice = self.__buffer[:_STRUCT_HEADER_LENGTH]

            header_raw = struct.unpack(_HEADER_STRUCT_FORMAT, peek_slice)

            header = _INOTIFY_EVENT(*header_raw)
            type_names = self._get_event_names(header.mask)
            _LOGGER.debug("Events received in stream: {}".format(type_names))

            event_length = _STRUCT_HEADER_LENGTH + header.len
            if length < event_length:
                return

            filename = self.__buffer[_STRUCT_HEADER_LENGTH:event_length]

            # Our filename is 16-byte aligned and right-padded with NULs.
            filename_bytes = filename.rstrip(b"\0")

            self.__buffer = self.__buffer[event_length:]

            path = self.__watches_r.get(header.wd)
            if path is not None:
                filename_unicode = filename_bytes.decode("utf8")
                yield (header, type_names, path, filename_unicode)

            buffer_length = len(self.__buffer)
            if buffer_length < _STRUCT_HEADER_LENGTH:
                break

    def event_gen(
        self,
        timeout_s=None,
        yield_nones=True,
        filter_predicate=None,
        terminal_events=_DEFAULT_TERMINAL_EVENTS,
    ):
        """Yield one event after another. If `timeout_s` is provided, we'll
        break when no event is received for that many seconds.
        """

        # We will either return due to the optional filter or because of a
        # timeout. The former will always set this. The latter will never set
        # this.
        self.__last_success_return = None

        last_hit_s = time.time()
        while True:
            block_duration_s = self.__get_block_duration()

            # Poll, but manage signal-related errors.

            try:
                events = self.__epoll.poll(block_duration_s)
            except IOError as e:
                if e.errno != EINTR:
                    raise

                if timeout_s is not None:
                    time_since_event_s = time.time() - last_hit_s
                    if time_since_event_s > timeout_s:
                        break

                continue

            # Process events.

            for fd, event_type in events:
                # (fd) looks to always match the inotify FD.

                names = self._get_event_names(event_type)
                _LOGGER.debug("Events received from epoll: {}".format(names))

                for (header, type_names, path, filename) in self._handle_inotify_event(
                    fd
                ):
                    last_hit_s = time.time()

                    e = (header, type_names, path, filename)
                    for type_name in type_names:
                        if (
                            filter_predicate is not None
                            and filter_predicate(type_name, e) is False
                        ):
                            self.__last_success_return = (type_name, e)
                            return
                        elif type_name in terminal_events:
                            raise TerminalEventException(type_name, e)

                    yield e

            if timeout_s is not None:
                time_since_event_s = time.time() - last_hit_s
                if time_since_event_s > timeout_s:
                    break

            if yield_nones is True:
                yield None

    @property
    def last_success_return(self):
        return self.__last_success_return


class _BaseTree(object):
    def __init__(
        self,
        mask=inotify.constants.IN_ALL_EVENTS,
        block_duration_s=_DEFAULT_EPOLL_BLOCK_DURATION_S,
    ):

        # No matter what we actually received as the mask, make sure we have
        # the minimum that we require to curate our list of watches.
        #
        # todo: we really should have two masks... the combined one (requested|needed)
        # and the user specified mask for the events he wants to receive from tree...
        self._mask = (
            mask
            | inotify.constants.IN_ISDIR
            | inotify.constants.IN_CREATE
            | inotify.constants.IN_MOVED_TO
            | inotify.constants.IN_DELETE
            | inotify.constants.IN_MOVED_FROM
        )

        self._i = Inotify(block_duration_s=block_duration_s)

    def event_gen(self, ignore_missing_new_folders=False, **kwargs):
        """This is a secondary generator that wraps the principal one, and
        adds/removes watches as directories are added/removed.

        If we're doing anything funky and allowing the events to queue while a
        rename occurs then the folder may no longer exist. In this case, set
        `ignore_missing_new_folders`.
        """

        for event in self._i.event_gen(**kwargs):
            if event is not None:
                (header, type_names, path, filename) = event

                if header.mask & inotify.constants.IN_ISDIR:
                    full_path = os.path.join(path, filename)

                    if (
                        (header.mask & inotify.constants.IN_MOVED_TO)
                        or (header.mask & inotify.constants.IN_CREATE)
                    ) and (
                        os.path.exists(full_path) is True
                        or
                        # todo: as long as the "Path already being watche/not in watch list" warnings
                        # instead of exceptions are in place, it should really be default to also log
                        # only a warning if target folder does not exists in tree autodiscover mode.
                        # - but probably better to implement that with try/catch around add_watch
                        # when errno fix is merged and also this should normally not be an argument
                        # to event_gen but to InotifyTree(s) constructor (at least set default there)
                        # to not steal someones use case to specify this differently for each event_call??
                        ignore_missing_new_folders is False
                    ):
                        _LOGGER.debug(
                            "A directory has been created. We're "
                            "adding a watch on it (because we're "
                            "being recursive): [%s]",
                            full_path,
                        )

                        self._i.add_watch(full_path, self._mask)

                    if header.mask & inotify.constants.IN_DELETE:
                        _LOGGER.debug(
                            "A directory has been removed. We're "
                            "being recursive, but it would have "
                            "automatically been deregistered: [%s]",
                            full_path,
                        )

                        # The watch would've already been cleaned-up internally.
                        self._i.remove_watch(full_path, superficial=True)
                    elif header.mask & inotify.constants.IN_MOVED_FROM:
                        _LOGGER.debug(
                            "A directory has been renamed. We're "
                            "being recursive, we will remove watch "
                            "from it and re-add with IN_MOVED_TO "
                            "if target parent dir is within "
                            "our tree: [%s]",
                            full_path,
                        )

                        try:
                            self._i.remove_watch(full_path, superficial=False)
                        except inotify.calls.InotifyError as ex:
                            # for the unlikely case the moved diretory is deleted
                            # and automatically unregistered before we try to
                            # unregister....
                            pass

            yield event

    @property
    def inotify(self):
        return self._i


class InotifyTree(_BaseTree):
    """Recursively watch a path."""

    def __init__(
        self,
        path,
        mask=inotify.constants.IN_ALL_EVENTS,
        block_duration_s=_DEFAULT_EPOLL_BLOCK_DURATION_S,
    ):
        super(InotifyTree, self).__init__(mask=mask, block_duration_s=block_duration_s)

        self.__root_path = path

        self.__load_tree(path)

    def __load_tree(self, path):
        _LOGGER.debug("Adding initial watches on tree: [%s]", path)

        paths = []

        q = [path]
        while q:
            current_path = q[0]
            del q[0]

            paths.append(current_path)

            for filename in os.listdir(current_path):
                entry_filepath = os.path.join(current_path, filename)
                if os.path.isdir(entry_filepath) is False:
                    continue

                q.append(entry_filepath)

        for path in paths:
            self._i.add_watch(path, self._mask)


class InotifyTrees(_BaseTree):
    """Recursively watch over a list of trees."""

    def __init__(
        self,
        paths,
        mask=inotify.constants.IN_ALL_EVENTS,
        block_duration_s=_DEFAULT_EPOLL_BLOCK_DURATION_S,
    ):
        super(InotifyTrees, self).__init__(mask=mask, block_duration_s=block_duration_s)

        self.__load_trees(paths)

    def __load_trees(self, paths):
        _LOGGER.debug(
            "Adding initial watches on trees: [%s]", ",".join(map(str, paths))
        )

        found = []

        q = paths
        while q:
            current_path = q[0]
            del q[0]

            found.append(current_path)

            for filename in os.listdir(current_path):
                entry_filepath = os.path.join(current_path, filename)
                if os.path.isdir(entry_filepath) is False:
                    continue

                q.append(entry_filepath)

        for path in found:
            self._i.add_watch(path, self._mask)
