from typing import Dict, List, Any, Optional
from datetime import datetime
from socket import gethostname
import functools
import json
import logging

from aw_core.models import Event
from aw_core.log import get_log_file_path

from aw_transform import heartbeat_merge, query2

from .exceptions import BadRequest, NotFound, Unauthorized


logger = logging.getLogger(__name__)


def check_bucket_exists(f):
    @functools.wraps(f)
    def g(self, bucket_id, *args, **kwargs):
        if bucket_id not in self.db.buckets():
            raise NotFound("NoSuchBucket", "There's no bucket named {}".format(bucket_id))
        return f(self, bucket_id, *args, **kwargs)
    return g


class ServerAPI:
    def __init__(self, db, testing):
        self.db = db
        self.testing = testing

    def get_info(self) -> Dict[str, Dict]:
        """Get server info"""
        payload = {
            'hostname': gethostname(),
            'testing': self.testing
        }
        return payload

    def get_buckets(self) -> Dict[str, Dict]:
        """Get dict {bucket_name: Bucket} of all buckets"""
        logger.debug("Received get request for buckets")
        buckets = self.db.buckets()
        for b in buckets:
            # TODO: Move this code to aw-core?
            last_events = self.db[b].get(limit=1)
            if len(last_events) > 0:
                last_event = last_events[0]
                last_updated = last_event.timestamp + last_event.duration
                buckets[b]["last_updated"] = last_updated.isoformat()
        return buckets

    @check_bucket_exists
    def get_bucket_metadata(self, bucket_id: str) -> Dict[str, Any]:
        """Get metadata about bucket."""
        bucket = self.db[bucket_id]
        return bucket.metadata()

    def create_bucket(self, bucket_id: str, event_type: str, client: str, hostname: str) -> bool:
        """Create bucket."""
        if bucket_id in self.db.buckets():
            return False
        self.db.create_bucket(
            bucket_id,
            type=event_type,
            client=client,
            hostname=hostname,
            created=datetime.now()
        )
        return True

    @check_bucket_exists
    def delete_bucket(self, bucket_id: str) -> None:
        """Delete a bucket"""
        self.db.delete_bucket(bucket_id)
        logger.debug("Deleted bucket '{}'".format(bucket_id))
        return None

    @check_bucket_exists
    def get_events(self, bucket_id: str, limit: int = -1,
                   start: datetime = None, end: datetime = None) -> List[Event]:
        """Get events from a bucket"""
        logger.debug("Received get request for events in bucket '{}'".format(bucket_id))
        events = [event.to_json_dict() for event in
                  self.db[bucket_id].get(limit, start, end)]
        return events

    @check_bucket_exists
    def create_events(self, bucket_id: str, events: List[Event]) -> Optional[Event]:
        """Create events for a bucket. Can handle both single events and multiple ones.

        Returns the inserted event when a single event was inserted, otherwise None."""
        return self.db[bucket_id].insert(events[0] if len(events) == 1 else events)

    @check_bucket_exists
    def get_eventcount(self, bucket_id: str,
                       start: datetime = None, end: datetime = None) -> int:
        """Get eventcount from a bucket"""
        logger.debug("Received get request for eventcount in bucket '{}'".format(bucket_id))
        return self.db[bucket_id].get_eventcount(start, end)

    @check_bucket_exists
    def heartbeat(self, bucket_id: str, heartbeat: Event, pulsetime: float) -> Event:
        """
        Heartbeats are useful when implementing watchers that simply keep
        track of a state, how long it's in that state and when it changes.
        A single heartbeat always has a duration of zero.

        If the heartbeat was identical to the last (apart from timestamp), then the last event has its duration updated.
        If the heartbeat differed, then a new event is created.

        Such as:
         - Active application and window title
           - Example: aw-watcher-window
         - Currently open document/browser tab/playing song
           - Example: wakatime
           - Example: aw-watcher-web
           - Example: aw-watcher-spotify
         - Is the user active/inactive?
           Send an event on some interval indicating if the user is active or not.
           - Example: aw-watcher-afk

        Inspired by: https://wakatime.com/developers#heartbeats
        """
        logger.debug("Received heartbeat in bucket '{}'\n\ttimestamp: {}\n\tdata: {}".format(
                     bucket_id, heartbeat.timestamp, heartbeat.data))

        # The endtime here is set such that in the event that the heartbeat is older than an
        # existing event we should try to merge it with the last event before the heartbeat instead.
        # FIXME: This (the endtime=heartbeat.timestamp) gets rid of the "heartbeat was older than last event"
        #        warning and also causes a already existing "newer" event to be overwritten in the
        #        replace_last call below. This is problematic.
        # Solution: This could be solved if we were able to replace arbitrary events.
        #           That way we could double check that the event has been applied
        #           and if it hasn't we simply replace it with the updated counterpart.
        events = self.db[bucket_id].get(limit=1, endtime=heartbeat.timestamp)

        if len(events) >= 1:
            last_event = events[0]
            if last_event.data == heartbeat.data:
                merged = heartbeat_merge(last_event, heartbeat, pulsetime)
                if merged is not None:
                    # Heartbeat was merged into last_event
                    logger.debug("Received valid heartbeat, merging. (bucket: {})".format(bucket_id))
                    self.db[bucket_id].replace_last(merged)
                    return merged
                else:
                    logger.info("Received heartbeat after pulse window, inserting as new event. (bucket: {})".format(bucket_id))
            else:
                logger.debug("Received heartbeat with differing data, inserting as new event. (bucket: {})".format(bucket_id))
        else:
            logger.info("Received heartbeat, but bucket was previously empty, inserting as new event. (bucket: {})".format(bucket_id))

        self.db[bucket_id].insert(heartbeat)
        return heartbeat

    def query2(self, name, query, start, end, cache):
        query = str().join(query)
        result = query2.query(name, query, start, end, self.db)
        if isinstance(result, list):
            result_list = []
            for e in result:
                if isinstance(e, Event):
                    result_list.append(e.to_json_dict())
            result = result_list
        return result

    # TODO: Right now the log format on disk has to be JSON, this is hard to read by humans...
    def get_log(self):
        """Get the server log in json format"""
        payload = []
        with open(get_log_file_path(), 'r') as log_file:
            for line in log_file.readlines()[::-1]:
                payload.append(json.loads(line))
        return payload, 200
