#!/usr/bin/env python

import pdb
import json
import urllib.request
from .util import to_bytes
from .app_state import app_state

class TransactionNotifier:
    @staticmethod
    def notify(address, history):
        headers = {'content-type':'application/json'}
        data = {'address':address, 'history':history}
        serialized_data = to_bytes(json.dumps(data))
        URL = app_state.config.get('notification_url');
        pdb.set_trace();
        try:
            req = urllib.request.Request(URL, serialized_data, headers)
            response_stream = urllib.request.urlopen(req, timeout=5)
            logger.debug('Got Response for %s', address)
        except Exception as e:
            logger.error("exception processing response %s", e)
