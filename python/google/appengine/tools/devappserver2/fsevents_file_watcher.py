#!/usr/bin/env python
#
# Copyright 2007 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Monitors a directory tree for changes using the Mac OS X FSEvents API."""


import logging
import os.path
import threading

from google.appengine.tools.devappserver2 import watcher_common

try:
  import AppKit
  import FSEvents
except ImportError:
  AppKit = None
  FSEvents = None


class FSEventsFileWatcher(object):
  """Monitors a directory tree for changes using FSEvents.

  Note that this class does not provide file-level change precision on Mac OS X
  version 10.7 or older. It also does not detect changes in symlinked files or
  directories. It would still be possible to do that efficiently by watching
  all symlinked directories and using mtime checking for symlinked files. On
  any change in a directory, it would have to be rescanned to see if a new
  symlinked file or directory was added. It also might be possible to use
  kevents instead of the Carbon API to detect files changes.
  """

  SUPPORTS_MULTIPLE_DIRECTORIES = True

  def __init__(self, directories):
    """Initializer for FSEventsFileWatcher.

    Args:
      directories: An iterable of strings representing the path to a directory
          that should be monitored for changes i.e. files and directories
          added, renamed, deleted or changed.
    """
    self._directories = [os.path.abspath(d) for d in directories]
    self._skip_files_re = None
    self._changes = set()
    self._change_event = threading.Event()
    self._quit_event = threading.Event()
    self._event_watcher_thread = threading.Thread(target=self._watch_changes)

  @staticmethod
  def is_available():
    return FSEvents is not None

  def _fsevents_callback(self, stream_ref, client_call_back_info, num_events,
      event_paths, event_flags, event_ids):
    changes = set()
    for absolute_path, flag in zip(event_paths, event_flags):
      directory = next(
        d for d in self._directories if absolute_path.startswith(d))
      skip_files_re = self._skip_files_re

      if not flag & (FSEvents.kFSEventStreamEventFlagItemCreated |
                      FSEvents.kFSEventStreamEventFlagItemRemoved |
                      FSEvents.kFSEventStreamEventFlagItemInodeMetaMod |
                      FSEvents.kFSEventStreamEventFlagItemRenamed |
                      FSEvents.kFSEventStreamEventFlagItemModified |
                      FSEvents.kFSEventStreamEventFlagItemFinderInfoMod |
                      FSEvents.kFSEventStreamEventFlagItemChangeOwner |
                      FSEvents.kFSEventStreamEventFlagItemXattrMod):
        continue

      if watcher_common.ignore_file(absolute_path, skip_files_re):
        continue

      # We also want to ignore a path if we should ignore any directory
      # that the path is in.
      def _recursive_ignore_dir(dirname):
        assert not os.path.isabs(dirname)  # or the while will never terminate
        (dir_dirpath, dir_base) = os.path.split(dirname)
        while dir_base:
          if watcher_common.ignore_dir(dir_dirpath, dir_base, skip_files_re):
            return True
          (dir_dirpath, dir_base) = os.path.split(dir_dirpath)
        return False

      relpath = os.path.relpath(absolute_path, directory)
      if _recursive_ignore_dir(os.path.dirname(relpath)):
        continue

      logging.warning("Reloading instances due to change in %s", relpath)
      changes.add(absolute_path)

      self._changes = changes
      self._change_event.set()

  def _watch_changes(self):
    # Do the file watching in a thread to ensure that
    # FSEventStreamScheduleWithRunLoop and CFRunLoopRunInMode are called in the
    # same thread.

    # Each thread needs its own AutoreleasePool.
    pool = AppKit.NSAutoreleasePool.alloc().init()
    event_stream = FSEvents.FSEventStreamCreate(
        None,
        self._fsevents_callback,
        None,
        self._directories,
        FSEvents.kFSEventStreamEventIdSinceNow,
        1,  # Seconds to wait to between received events.
        FSEvents.kFSEventStreamCreateFlagFileEvents,
        )

    FSEvents.FSEventStreamScheduleWithRunLoop(event_stream,
                                              FSEvents.CFRunLoopGetCurrent(),
                                              FSEvents.kCFRunLoopDefaultMode)

    assert FSEvents.FSEventStreamStart(event_stream), (
        'event stream could not be started')
    while not self._quit_event.is_set():
      FSEvents.CFRunLoopRunInMode(FSEvents.kCFRunLoopDefaultMode,
                                  0.1,    # seconds
                                  False)  # returnAfterSourceHandled

    FSEvents.FSEventStreamRelease(event_stream)
    del pool  # del is recommended by the PyObjc programming guide.

  def start(self):
    """Start watching the directory for changes."""
    self._changes = set()
    self._event_watcher_thread.start()

  def set_skip_files_re(self, skip_files_re):
    """All re's in skip_files_re are taken to be relative to its base-dir."""
    self._skip_files_re = skip_files_re

  def quit(self):
    """Stop watching the directory for changes."""
    self._quit_event.set()

  def changes(self, timeout_ms=0):
    assert self._event_watcher_thread.is_alive(), (
        'watcher thread exited or was not started')
    try:
      self._change_event.wait(timeout_ms / 1000.0)
      return self._changes
    finally:
      self._changes = set()
      self._change_event.clear()
