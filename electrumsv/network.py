# Electrum - Lightweight Bitcoin Client
# Copyright (c) 2011-2016 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from collections import defaultdict
import errno
import json
import logging
import os
import queue
import random
import re
import select
import socket
import stat
import threading
import time

import socks
from . import util
from . import bitcoin
from .bitcoin import COIN, bfh, Hash
from .i18n import _
from .interface import Connection, Interface
from . import blockchain
from .version import PACKAGE_VERSION, PROTOCOL_VERSION
from .simple_config import SimpleConfig


logger = logging.getLogger("network")

class RPCError(Exception):
    pass


NODES_RETRY_INTERVAL = 60
SERVER_RETRY_INTERVAL = 10

# Called by util.py:get_peers()
def parse_servers(result):
    """ parse servers list into dict format"""
    servers = {}
    for item in result:
        host = item[1]
        out = {}
        version = None
        pruning_level = '-'
        if len(item) > 2:
            for v in item[2]:
                if re.match(r"[st]\d*", v):
                    protocol, port = v[0], v[1:]
                    if port == '': port = bitcoin.NetworkConstants.DEFAULT_PORTS[protocol]
                    out[protocol] = port
                elif re.match(r"v(.?)+", v):
                    version = v[1:]
                elif re.match(r"p\d*", v):
                    pruning_level = v[1:]
                if pruning_level == '': pruning_level = '0'
        if out:
            out['pruning'] = pruning_level
            out['version'] = version
            servers[host] = out
    return servers

# Imported by scripts/servers.py
def filter_version(servers):
    def is_recent(version):
        try:
            return util.normalize_version(version) >= util.normalize_version(PROTOCOL_VERSION)
        except Exception as e:
            return False
    return {k: v for k, v in servers.items() if is_recent(v.get('version'))}


# Imported by scripts/peers.py
def filter_protocol(hostmap, protocol = 's'):
    '''Filters the hostmap for those implementing protocol.
    The result is a list in serialized form.'''
    eligible = []
    for host, portmap in hostmap.items():
        port = portmap.get(protocol)
        if port:
            eligible.append(serialize_server(host, port, protocol))
    return eligible

def _get_eligible_servers(hostmap=None, protocol="s", exclude_set=None):
    if exclude_set is None:
        exclude_set = set()
    if hostmap is None:
        hostmap = bitcoin.NetworkConstants.DEFAULT_SERVERS
    return list(set(filter_protocol(hostmap, protocol)) - exclude_set)

def _pick_random_server(hostmap=None, protocol='s', exclude_set=None):
    if exclude_set is None:
        exclude_set = set()
    eligible = _get_eligible_servers(hostmap, protocol, exclude_set)
    return random.choice(eligible) if eligible else None

proxy_modes = ['socks4', 'socks5', 'http']


def _serialize_proxy(p):
    if not isinstance(p, dict):
        return None
    return ':'.join([p.get('mode'), p.get('host'), p.get('port'),
                     p.get('user', ''), p.get('password', '')])


def _deserialize_proxy(s):
    if not isinstance(s, str):
        return None
    if s.lower() == 'none':
        return None
    proxy = { "mode":"socks5", "host":"localhost" }
    args = s.split(':')
    n = 0
    if proxy_modes.count(args[n]) == 1:
        proxy["mode"] = args[n]
        n += 1
    if len(args) > n:
        proxy["host"] = args[n]
        n += 1
    if len(args) > n:
        proxy["port"] = args[n]
        n += 1
    else:
        proxy["port"] = "8080" if proxy["mode"] == "http" else "1080"
    if len(args) > n:
        proxy["user"] = args[n]
        n += 1
    if len(args) > n:
        proxy["password"] = args[n]
    return proxy


# Imported by gui.qt.network_dialog.py
def deserialize_server(server_str):
    host, port, protocol = str(server_str).rsplit(':', 2)
    assert protocol in 'st'
    int(port)    # Throw if cannot be converted to int
    return host, port, protocol

# Imported by gui.qt.network_dialog.py
def serialize_server(host, port, protocol):
    return str(':'.join([host, port, protocol]))


class Network(util.DaemonThread):
    """
    The Network class manages a set of connections to remote electrum
    servers, each connected socket is handled by an Interface() object.
    Connections are initiated by a Connection() thread which stops once
    the connection succeeds or fails.
    """

    def __init__(self, config=None):
        if config is None:
            config = {}  # Do not use mutables as default values!
        util.DaemonThread.__init__(self)
        self.config = SimpleConfig(config) if isinstance(config, dict) else config
        self.num_server = 10 if not self.config.get('oneserver') else 0
        self.blockchains = blockchain.read_blockchains(self.config)
        logger.debug("blockchains %s", self.blockchains.keys())
        self.blockchain_index = config.get('blockchain_index', 0)
        if self.blockchain_index not in self.blockchains.keys():
            self.blockchain_index = 0
        # Server for addresses and transactions
        self.default_server = self.config.get('server', None)
        self.blacklisted_servers = set(self.config.get('server_blacklist', []))
        logger.debug("server blacklist: %s", self.blacklisted_servers)
        # Sanitize default server
        if self.default_server:
            try:
                deserialize_server(self.default_server)
            except:
                logger.error('failed to parse server-string; falling back to random.')
                self.default_server = None
        if not self.default_server or self.default_server in self.blacklisted_servers:
            self.default_server = _pick_random_server()

        self.lock = threading.Lock()
        # locks: if you need to take several acquire them in the order they are defined here!
        self.interface_lock = threading.RLock()            # <- re-entrant
        self.pending_sends_lock = threading.Lock()

        self.pending_sends = []
        self.message_id = 0
        self.verified_checkpoint = False
        self.verifications_required = 1
        # If the height is cleared from the network constants, we're
        # taking looking to get 3 confirmations of the first verification.
        if bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT is None:
            self.verifications_required = 3
        self.checkpoint_servers_verified = {}
        self.checkpoint_height = bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT
        self.debug = False
        self.irc_servers = {} # returned by interface (list from irc)
        self.recent_servers = self._read_recent_servers()

        self.banner = ''
        self.donation_address = ''
        self.relay_fee = None
        # callbacks passed with subscriptions
        self.subscriptions = defaultdict(list)
        self.sub_cache = {}                     # note: needs self.interface_lock
        # callbacks set by the GUI
        self.callbacks = defaultdict(list)

        dir_path = os.path.join( self.config.path, 'certs')
        if not os.path.exists(dir_path):
            os.mkdir(dir_path)
            os.chmod(dir_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        # subscriptions and requests
        self.subscribed_addresses = set()
        # Requests from client we've not seen a response to
        self.unanswered_requests = {}
        # retry times
        self.server_retry_time = time.time()
        self.nodes_retry_time = time.time()
        # kick off the network.  interface is the main server we are currently
        # communicating with.  interfaces is the set of servers we are connecting
        # to or have an ongoing connection with
        self.interface = None                   # note: needs self.interface_lock
        self.interfaces = {}                    # note: needs self.interface_lock
        self.auto_connect = self.config.get('auto_connect', True)
        self.connecting = set()
        self.requested_chunks = set()
        self.socket_queue = queue.Queue()
        self._start_network(deserialize_server(self.default_server)[2],
                           _deserialize_proxy(self.config.get('proxy')))

    # Called by gui.qt.main_window.py:__init__()
    # Called by gui.qt.coinsplitting_tab.py:_on_split_button_clicked()
    # Called by gui.qt.network_dialog.py:__init__()
    # Called by scripts/stdio.py
    # Called by scripts/text.py
    def register_callback(self, callback, events):
        with self.lock:
            for event in events:
                self.callbacks[event].append(callback)

    # Called by gui.qt.main_window.py:clean_up()
    # Called by gui.qt.coinsplitting_tab.py:_split_cleanup()
    def unregister_callback(self, callback):
        with self.lock:
            for callbacks in self.callbacks.values():
                if callback in callbacks:
                    callbacks.remove(callback)

    # Called by exchange_rate.py:on_quotes()
    # Called by exchange_rate.py:on_history()
    # Called by synchronizer.py:tx_response()
    # Called by synchronizer.py:run()
    def trigger_callback(self, event, *args):
        with self.lock:
            callbacks = self.callbacks[event][:]
        [callback(event, *args) for callback in callbacks]

    def _recent_servers_file(self):
        return os.path.join(self.config.path, "recent-servers")

    def _read_recent_servers(self):
        if not self.config.path:
            return []
        try:
            with open(self._recent_servers_file(), "r", encoding='utf-8') as f:
                data = f.read()
                return json.loads(data)
        except:
            return []

    def _save_recent_servers(self):
        if not self.config.path:
            return
        s = json.dumps(self.recent_servers, indent=4, sort_keys=True)
        try:
            with open(self._recent_servers_file(), "w", encoding='utf-8') as f:
                f.write(s)
        except:
            pass

    # Called by daemon.py:run_daemon()
    # Called by gui.qt.main_window.py:update_status()
    def get_server_height(self):
        return self.interface.tip if self.interface else 0

    def _server_is_lagging(self):
        sh = self.get_server_height()
        if not sh:
            logger.debug('no height for main interface')
            return True
        lh = self.get_local_height()
        result = (lh - sh) > 1
        if result:
            logger.debug('%s is lagging (%d vs %d)', self.default_server, sh, lh)
        return result

    def _set_status(self, status):
        self.connection_status = status
        self._notify('status')

    # Called by daemon.py:run_daemon()
    # Called by gui.qt.main_window.py:notify_tx_cb()
    # Called by gui.qt.main_window.py:update_status()
    # Called by gui.qt.main_window.py:update_wallet()
    # Called by gui.qt.network_dialog.py:__init__()
    # Called by gui.stdio.py:get_balance()
    # Called by gui.text.py:print_balance()
    # Called by wallet.py:wait_until_synchronized()
    # Called by scripts/block_headers.py
    # Called by scripts/watch_address.py
    def is_connected(self):
        return self.interface is not None

    # Called by scripts/block_headers.py
    # Called by scripts/watch_address.py
    def is_connecting(self):
        return self.connection_status == 'connecting'

    def _queue_request(self, method, params, interface=None):
        # If you want to queue a request on any interface it must go
        # through this function so message ids are properly tracked
        if interface is None:
            interface = self.interface
        message_id = self.message_id
        self.message_id += 1
        if self.debug:
            logger.debug("%s --> %s %s %s", interface.host, method, params, message_id)
        interface.queue_request(method, params, message_id)
        return message_id

    def _send_subscriptions(self):
        logger.debug('sending subscriptions to %s %d %d', self.interface.server,
                     len(self.unanswered_requests), len(self.subscribed_addresses))
        self.sub_cache.clear()
        # Resend unanswered requests
        requests = self.unanswered_requests.values()
        self.unanswered_requests = {}
        for request in requests:
            message_id = self._queue_request(request[0], request[1])
            self.unanswered_requests[message_id] = request
        self._queue_request('server.banner', [])
        self._queue_request('server.donation_address', [])
        self._queue_request('server.peers.subscribe', [])
        self._request_fee_estimates()
        self._queue_request('blockchain.relayfee', [])
        for h in self.subscribed_addresses:
            self._queue_request('blockchain.scripthash.subscribe', [h])

    def _request_fee_estimates(self):
        self.config.requested_fee_estimates()
        for i in bitcoin.FEE_TARGETS:
            self._queue_request('blockchain.estimatefee', [i])

    def _get_status_value(self, key):
        if key == 'status':
            value = self.connection_status
        elif key == 'banner':
            value = self.banner
        elif key == 'fee':
            value = self.config.fee_estimates
        elif key == 'updated':
            value = (self.get_local_height(), self.get_server_height())
        elif key == 'servers':
            value = self.get_servers()
        elif key == 'interfaces':
            value = self.get_interfaces()
        return value

    def _notify(self, key):
        if key in ['status', 'updated']:
            self.trigger_callback(key)
        else:
            self.trigger_callback(key, self._get_status_value(key))

    # Called by daemon.py:run_daemon()
    # Called by gui.qt.main_window.py:donate_to_server()
    # Called by gui.qt.network_dialog.py:update()
    # Called by gui.qt.network_dialog.py:fill_in_proxy_settings()
    # Called by gui.qt.network_dialog.py:follow_server()
    # Called by gui.qt.network_dialog.py:set_server()
    # Called by gui.qt.network_dialog.py:set_proxy()
    def get_parameters(self):
        host, port, protocol = deserialize_server(self.default_server)
        return host, port, protocol, self.proxy, self.auto_connect

    # Called by gui.qt.main_window.py:donate_to_server()
    def get_donation_address(self):
        if self.is_connected():
            return self.donation_address

    # Called by daemon.py:run_daemon()
    # Called by gui.qt.network_dialog.py:update()
    # Called by scripts/util.py
    def get_interfaces(self):
        '''The interfaces that are in connected state'''
        return list(self.interfaces.keys())

    # Called by commands.py:getservers()
    # Called by gui.qt.network_dialog.py:update()
    def get_servers(self):
        out = bitcoin.NetworkConstants.DEFAULT_SERVERS
        if self.irc_servers:
            out.update(filter_version(self.irc_servers.copy()))
        else:
            for s in self.recent_servers:
                try:
                    host, port, protocol = deserialize_server(s)
                except:
                    continue
                if host not in out:
                    out[host] = { protocol:port }
        return out

    def _start_interface(self, server_key):
        """Start the given server if it is not already active or being connected to.

        Arguments:
        server_key --- server specifier in the form of '<host>:<port>:<protocol>'
        """
        if (not server_key in self.interfaces and not server_key in self.connecting):
            if server_key == self.default_server:
                logger.debug("connecting to %s as new interface", server_key)
                self._set_status('connecting')
            self.connecting.add(server_key)
            c = Connection(server_key, self.socket_queue, self.config.path)

    def _get_unavailable_servers(self):
        exclude_set = set(self.interfaces)
        exclude_set = exclude_set.union(self.connecting)
        exclude_set = exclude_set.union(self.disconnected_servers)
        exclude_set = exclude_set.union(self.blacklisted_servers)
        return exclude_set

    def _start_random_interface(self):
        exclude_set = self._get_unavailable_servers()
        server_key = _pick_random_server(self.get_servers(), self.protocol, exclude_set)
        if server_key:
            self._start_interface(server_key)

    def _start_interfaces(self):
        self._start_interface(self.default_server)
        for i in range(self.num_server - 1):
            self._start_random_interface()

    def _set_proxy(self, proxy):
        self.proxy = proxy
        # Store these somewhere so we can un-monkey-patch
        if not hasattr(socket, "_socketobject"):
            socket._socketobject = socket.socket
            socket._getaddrinfo = socket.getaddrinfo
        if proxy:
            logger.debug("setting proxy '%s'", proxy)
            proxy_mode = proxy_modes.index(proxy["mode"]) + 1
            socks.setdefaultproxy(proxy_mode,
                                  proxy["host"],
                                  int(proxy["port"]),
                                  # socks.py seems to want either None or a non-empty string
                                  username=(proxy.get("user", "") or None),
                                  password=(proxy.get("password", "") or None))
            socket.socket = socks.socksocket
            # prevent dns leaks, see http://stackoverflow.com/questions/13184205/dns-over-proxy
            socket.getaddrinfo = lambda *args: [(socket.AF_INET, socket.SOCK_STREAM,
                                                 6, '', (args[0], args[1]))]
        else:
            socket.socket = socket._socketobject
            socket.getaddrinfo = socket._getaddrinfo

    def _start_network(self, protocol, proxy):
        assert not self.interface and not self.interfaces
        assert not self.connecting and self.socket_queue.empty()
        logger.debug('starting network')
        self.disconnected_servers = set([])
        self.protocol = protocol
        self._set_proxy(proxy)
        self._start_interfaces()

    def _stop_network(self):
        logger.debug("stopping network")
        for interface in list(self.interfaces.values()):
            self._close_interface(interface)
        if self.interface:
            self._close_interface(self.interface)
        assert self.interface is None
        assert not self.interfaces
        self.connecting = set()
        # Get a new queue - no old pending connections thanks!
        self.socket_queue = queue.Queue()

    # Called by network_dialog.py:follow_server()
    # Called by network_dialog.py:set_server()
    # Called by network_dialog.py:set_proxy()
    def set_parameters(self, host, port, protocol, proxy, auto_connect):
        proxy_str = _serialize_proxy(proxy)
        server = serialize_server(host, port, protocol)
        # sanitize parameters
        try:
            deserialize_server(serialize_server(host, port, protocol))
            if proxy:
                proxy_modes.index(proxy["mode"]) + 1
                int(proxy['port'])
        except:
            return
        self.config.set_key('auto_connect', auto_connect, False)
        self.config.set_key("proxy", proxy_str, False)
        self.config.set_key("server", server, True)
        # abort if changes were not allowed by config
        if self.config.get('server') != server or self.config.get('proxy') != proxy_str:
            return
        self.auto_connect = auto_connect
        if self.proxy != proxy or self.protocol != protocol:
            # Restart the network defaulting to the given server
            self._stop_network()
            self.default_server = server
            self._start_network(protocol, proxy)
        elif self.default_server != server:
            self.switch_to_interface(server, self.SWITCH_SET_PARAMETERS)
        else:
            self._switch_lagging_interface()
            self._notify('updated')

    def _switch_to_random_interface(self):
        '''Switch to a random connected server other than the current one'''
        servers = self.get_interfaces()    # Those in connected state
        if self.default_server in servers:
            servers.remove(self.default_server)
        if servers:
            self.switch_to_interface(random.choice(servers))

    def _switch_lagging_interface(self):
        '''If auto_connect and lagging, switch interface'''
        if self._server_is_lagging() and self.auto_connect:
            # switch to one that has the correct header (not height)
            header = self.blockchain().read_header(self.get_local_height())
            filtered = [key for key, value in self.interfaces.items()
                        if value.tip_header==header]
            if filtered:
                choice = random.choice(filtered)
                self.switch_to_interface(choice, self.SWITCH_LAGGING)

    SWITCH_DEFAULT = 'SWITCH_DEFAULT'
    SWITCH_RANDOM = 'SWITCH_RANDOM'
    SWITCH_LAGGING = 'SWITCH_LAGGING'
    SWITCH_SOCKET_LOOP = 'SWITCH_SOCKET_LOOP'
    SWITCH_FOLLOW_CHAIN = 'SWITCH_FOLLOW_CHAIN'
    SWITCH_SET_PARAMETERS = 'SWITCH_SET_PARAMETERS'

    # Called by network_dialog.py:follow_server()
    def switch_to_interface(self, server, switch_reason=None):
        '''Switch to server as our interface.  If no connection exists nor
        being opened, start a thread to connect.  The actual switch will
        happen on receipt of the connection notification.  Do nothing
        if server already is our interface.'''
        self.default_server = server
        if server not in self.interfaces:
            self.interface = None
            self._start_interface(server)
            return
        i = self.interfaces[server]
        if self.interface != i:
            logger.debug("switching to '%s' reason '%s'", server, switch_reason)
            # stop any current interface in order to terminate subscriptions
            # fixme: we don't want to close headers sub
            #self._close_interface(self.interface)
            self.interface = i
            self._send_subscriptions()
            self._set_status('connected')
            self._notify('updated')

    def _close_interface(self, interface):
        if interface:
            if interface.server in self.interfaces:
                self.interfaces.pop(interface.server)
            if interface.server == self.default_server:
                self.interface = None
            interface.close()

    def _add_recent_server(self, server):
        # list is ordered
        if server in self.recent_servers:
            self.recent_servers.remove(server)
        self.recent_servers.insert(0, server)
        self.recent_servers = self.recent_servers[0:20]
        self._save_recent_servers()

    def _process_response(self, interface, request, response, callbacks):
        if self.debug:
            logger.debug("<-- %s", response)
        error = response.get('error')
        result = response.get('result')
        method = response.get('method')
        params = response.get('params')

        # We handle some responses; return the rest to the client.
        if method == 'server.version':
            self._on_server_version(interface, result)
        elif method == 'blockchain.headers.subscribe':
            if error is None:
                self._on_notify_header(interface, result)
        elif method == 'server.peers.subscribe':
            if error is None:
                self.irc_servers = parse_servers(result)
                self._notify('servers')
        elif method == 'server.banner':
            if error is None:
                self.banner = result
                self._notify('banner')
        elif method == 'server.donation_address':
            if error is None:
                self.donation_address = result
        elif method == 'blockchain.estimatefee':
            if error is None and result > 0:
                i = params[0]
                fee = int(result*COIN)
                self.config.update_fee_estimates(i, fee)
                logger.debug("fee_estimates[%d] %d", i, fee)
                self._notify('fee')
        elif method == 'blockchain.relayfee':
            if error is None:
                self.relay_fee = int(result * COIN)
                logger.debug("relayfee %s", self.relay_fee)
        elif method == 'blockchain.block.headers':
            self._on_block_headers(interface, request, response)
        elif method == 'blockchain.block.header':
            self._on_header(interface, request, response)

        for callback in callbacks:
            callback(response)

    def _get_index(self, method, params):
        """ hashable index for subscriptions and cache"""
        return str(method) + (':' + str(params[0]) if params else '')

    def _process_responses(self, interface):
        responses = interface.get_responses()
        for request, response in responses:
            if request:
                method, params, message_id = request
                k = self._get_index(method, params)
                # client requests go through self.send() with a
                # callback, are only sent to the current interface,
                # and are placed in the unanswered_requests dictionary
                client_req = self.unanswered_requests.pop(message_id, None)
                if client_req:
                    assert interface == self.interface
                    callbacks = [client_req[2]]
                else:
                    # fixme: will only work for subscriptions
                    k = self._get_index(method, params)
                    callbacks = self.subscriptions.get(k, [])

                # Copy the request method and params to the response
                response['method'] = method
                response['params'] = params
                # Only once we've received a response to an addr subscription
                # add it to the list; avoids double-sends on reconnection
                if method == 'blockchain.scripthash.subscribe':
                    self.subscribed_addresses.add(params[0])
            else:
                if not response:  # Closed remotely / misbehaving
                    self._connection_down(interface.server)
                    break
                # Rewrite response shape to match subscription request response
                method = response.get('method')
                params = response.get('params')
                k = self._get_index(method, params)
                if method == 'blockchain.headers.subscribe':
                    response['result'] = params[0]
                    response['params'] = []
                elif method == 'blockchain.scripthash.subscribe':
                    response['params'] = [params[0]]  # addr
                    response['result'] = params[1]
                callbacks = self.subscriptions.get(k, [])

            # update cache if it's a subscription
            if method.endswith('.subscribe'):
                with self.interface_lock:
                    self.sub_cache[k] = response
            # Response is now in canonical form
            self._process_response(interface, request, response, callbacks)

    # Called by synchronizer.py:subscribe_to_addresses()
    def subscribe_to_scripthashes(self, scripthashes, callback):
        msgs = [('blockchain.scripthash.subscribe', [sh])
                for sh in scripthashes]
        self.send(msgs, callback)

    # Called by synchronizer.py:on_address_status()
    def request_scripthash_history(self, sh, callback):
        self.send([('blockchain.scripthash.get_history', [sh])], callback)

    # Called by commands.py:notify()
    # Called by websockets.py:reading_thread()
    # Called by websockets.py:run()
    # Called locally.
    def send(self, messages, callback):
        '''Messages is a list of (method, params) tuples'''
        messages = list(messages)
        with self.pending_sends_lock:
            self.pending_sends.append((messages, callback))

    def _process_pending_sends(self):
        # Requests needs connectivity.  If we don't have an interface,
        # we cannot process them.
        if not self.interface:
            return

        with self.pending_sends_lock:
            sends = self.pending_sends
            self.pending_sends = []

        for messages, callback in sends:
            for method, params in messages:
                r = None
                if method.endswith('.subscribe'):
                    k = self._get_index(method, params)
                    # add callback to list
                    l = self.subscriptions.get(k, [])
                    if callback not in l:
                        l.append(callback)
                    self.subscriptions[k] = l
                    # check cached response for subscriptions
                    r = self.sub_cache.get(k)
                if r is not None:
                    logger.debug("cache hit '%s'", k)
                    callback(r)
                else:
                    message_id = self._queue_request(method, params)
                    self.unanswered_requests[message_id] = method, params, callback

    # Called by synchronizer.py:release()
    def unsubscribe(self, callback):
        '''Unsubscribe a callback to free object references to enable GC.'''
        # Note: we can't unsubscribe from the server, so if we receive
        # subsequent notifications _process_response() will emit a harmless
        # "received unexpected notification" warning
        with self.lock:
            for v in self.subscriptions.values():
                if callback in v:
                    v.remove(callback)

    def _connection_down(self, server, blacklist=False):
        '''A connection to server either went down, or was never made.
        We distinguish by whether it is in self.interfaces.'''
        if blacklist:
            self.blacklisted_servers.add(server)
            # rt12 --- there might be a better place for this.
            self.config.set_key("server_blacklist", list(self.blacklisted_servers), True)
        else:
            self.disconnected_servers.add(server)
        if server == self.default_server:
            self._set_status('disconnected')
        if server in self.interfaces:
            self._close_interface(self.interfaces[server])
            self._notify('interfaces')
        for b in self.blockchains.values():
            if b.catch_up == server:
                b.catch_up = None

    def _new_interface(self, server_key, socket):
        self._add_recent_server(server_key)

        interface = Interface(server_key, socket)
        interface.blockchain = None
        interface.tip_header = None
        interface.tip = 0
        interface.set_mode(Interface.MODE_VERIFICATION)

        with self.interface_lock:
            self.interfaces[server_key] = interface

        # server.version should be the first message
        params = [PACKAGE_VERSION, PROTOCOL_VERSION]
        self._queue_request('server.version', params, interface)
        # The interface will immediately respond with it's last known header.
        self._queue_request('blockchain.headers.subscribe', [], interface)

        if server_key == self.default_server:
            self.switch_to_interface(server_key, self.SWITCH_DEFAULT)

    def _maintain_sockets(self):
        '''Socket maintenance.'''
        # Responses to connection attempts?
        while not self.socket_queue.empty():
            server, socket = self.socket_queue.get()
            if server in self.connecting:
                self.connecting.remove(server)
            if socket:
                self._new_interface(server, socket)
            else:
                self._connection_down(server)

        # Send pings and shut down stale interfaces
        # must use copy of values
        with self.interface_lock:
            interfaces = list(self.interfaces.values())
        for interface in interfaces:
            if interface.has_timed_out():
                self._connection_down(interface.server)
            elif interface.ping_required():
                self._queue_request('server.ping', [], interface)

        now = time.time()
        # nodes
        with self.interface_lock:
            server_count = len(self.interfaces) + len(self.connecting)
            if server_count < self.num_server:
                self._start_random_interface()
                if now - self.nodes_retry_time > NODES_RETRY_INTERVAL:
                    logger.debug('retrying connections')
                    self.disconnected_servers = set([])
                    self.nodes_retry_time = now

        # main interface
        with self.interface_lock:
            if not self.is_connected():
                if self.auto_connect:
                    if not self.is_connecting():
                        self._switch_to_random_interface()
                else:
                    if self.default_server in self.disconnected_servers:
                        if now - self.server_retry_time > SERVER_RETRY_INTERVAL:
                            self.disconnected_servers.remove(self.default_server)
                            self.server_retry_time = now
                    else:
                        self.switch_to_interface(self.default_server, self.SWITCH_SOCKET_LOOP)
            else:
                if self.config.is_fee_estimates_update_required():
                    self._request_fee_estimates()

    # Called by verifier.py:run()
    def request_chunk(self, interface, chunk_index):
        if chunk_index in self.requested_chunks:
            return False
        self.requested_chunks.add(chunk_index)

        interface.logger.debug("requesting chunk %s", chunk_index)
        chunk_base_height = chunk_index * 2016
        chunk_count = 2016
        self._request_headers(interface, chunk_base_height, chunk_count, silent=True)
        return True

    def _request_headers(self, interface, base_height, count, silent=False):
        if not silent:
            interface.logger.debug("requesting multiple consecutive headers, from %s count %s",
                                   base_height, count)
        if count > 2016:
            raise ValueError("too many headers")

        top_height = base_height + count - 1
        if top_height > bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT:
            if base_height < bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT:
                # As part of the verification process, we fetched the set of headers that
                # allowed manual verification of the post-checkpoint headers that were
                # fetched as part of the "catch-up" process.  This requested header batch
                # overlaps the checkpoint, so we know we have the post-checkpoint segment
                # from the "catch-up".  This leaves us needing some header preceding the
                # checkpoint, and we can clip the batch to the checkpoint to ensure we can
                # verify the fetched batch, which we wouldn't otherwise be able to do
                # manually as we cannot guarantee we have the headers preceding the batch.
                interface.logger.debug("clipping request across checkpoint height %s (%s -> %s)",
                                       bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT,
                                       base_height, top_height)
                verified_count = (bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT
                                  - base_height + 1)
                self.__request_headers(interface, base_height, verified_count,
                                      bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT)
            else:
                self.__request_headers(interface, base_height, count)
        else:
            self.__request_headers(interface, base_height, count,
                                  bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT)

    def __request_headers(self, interface, base_height, count, checkpoint_height=0):
        params = [base_height, count, checkpoint_height]
        self._queue_request('blockchain.block.headers', params, interface)

    def _on_block_headers(self, interface, request, response):
        '''Handle receiving a chunk of block headers'''
        error = response.get('error')
        result = response.get('result')
        params = response.get('params')
        if not request or result is None or params is None or error is not None:
            interface.logger.error(error or 'bad response')
            # Ensure the chunk can be rerequested, but only if the request originated from us.
            if request and request[1][0] // 2016 in self.requested_chunks:
                self.requested_chunks.remove(request[1][0] // 2016)
            return

        # Ignore unsolicited chunks
        request_params = request[1]
        request_base_height = request_params[0]
        expected_header_count = request_params[1]
        index = request_base_height // 2016
        if request_params != params:
            interface.logger.error("unsolicited chunk base_height=%s count=%s",
                                   request_base_height, expected_header_count)
            return
        if index in self.requested_chunks:
            self.requested_chunks.remove(index)

        header_hexsize = 80 * 2
        hexdata = result['hex']
        actual_header_count = len(hexdata) // header_hexsize
        # We accept less headers than we asked for, to cover the case where the distance
        # to the tip was unknown.
        if actual_header_count > expected_header_count:
            interface.logger.error("chunk data size incorrect expected_size=%s actual_size=%s",
                                   expected_header_count * header_hexsize, len(hexdata))
            return

        proof_was_provided = False
        if 'root' in result and 'branch' in result:
            header_height = request_base_height + actual_header_count - 1
            header_offset = (actual_header_count - 1) * header_hexsize
            header = hexdata[header_offset : header_offset + header_hexsize]
            if not self._validate_checkpoint_result(interface, result["root"],
                                                   result["branch"], header, header_height):
                # Got checkpoint validation data, server failed to provide proof.
                interface.logger.error("disconnecting server for incorrect checkpoint proof")
                self._connection_down(interface.server, blacklist=True)
                return

            data = bfh(hexdata)
            try:
                blockchain.verify_proven_chunk(request_base_height, data)
            except blockchain.VerifyError as e:
                interface.logger.error('disconnecting server for failed verify_proven_chunk: %s',
                                       e)
                self._connection_down(interface.server, blacklist=True)
                return

            proof_was_provided = True
        elif len(request_params) == 3 and request_params[2] != 0:
            # Expected checkpoint validation data, did not receive it.
            self._connection_down(interface.server)
            return

        verification_top_height = self.checkpoint_servers_verified.get(
            interface.server, {}).get('height')
        was_verification_request = (verification_top_height and
                                    request_base_height == verification_top_height - 147 + 1 and
                                    actual_header_count == 147)

        initial_interface_mode = interface.mode
        if interface.mode == Interface.MODE_VERIFICATION:
            if not was_verification_request:
                interface.logger.error("disconnecting unverified server for sending "
                                       "unrelated header chunk")
                self._connection_down(interface.server, blacklist=True)
                return
            if not proof_was_provided:
                interface.logger.error("disconnecting unverified server for sending "
                                       "verification header chunk without proof")
                self._connection_down(interface.server, blacklist=True)
                return

            if not self._apply_successful_verification(interface, request_params[2],
                                                      result['root']):
                return
            # We connect this verification chunk into the longest chain.
            target_blockchain = self.blockchains[0]
        else:
            target_blockchain = interface.blockchain

        chunk_data = bfh(hexdata)
        connect_state = target_blockchain.connect_chunk(request_base_height, chunk_data,
                                                        proof_was_provided)
        if connect_state == blockchain.CHUNK_ACCEPTED:
            interface.logger.debug("connected chunk, height=%s count=%s proof_was_provided=%s",
                                   request_base_height, actual_header_count, proof_was_provided)
        elif connect_state == blockchain.CHUNK_FORKS:
            interface.logger.error("identified forking chunk, height=%s count=%s",
                                   request_base_height, actual_header_count)
            # We actually have all the headers up to the bad point. In theory we
            # can use them to detect a fork point in some cases. Maybe we should never
            # get here because the blockchain code should actually work.
        else:
            interface.logger.error("discarded bad chunk, height=%s count=%s reason=%s",
                                   request_base_height, actual_header_count, connect_state)
            self._connection_down(interface.server)
            return

        # This interface was verified above. Get it syncing.
        if initial_interface_mode == Interface.MODE_VERIFICATION:
            self._process_latest_tip(interface)
            return

        # If not finished, get the next chunk.
        if proof_was_provided and not was_verification_request:
            # the verifier must have asked for this chunk.  It has been overlaid into the file.
            pass
        else:
            if interface.blockchain.height() < interface.tip:
                self._request_headers(interface, request_base_height + actual_header_count, 2016)
            else:
                interface.set_mode(Interface.MODE_DEFAULT)
                interface.logger.debug('catch up done %s', interface.blockchain.height())
                interface.blockchain.catch_up = None
        self._notify('updated')

    def _request_header(self, interface, height):
        '''
        This works for all modes except for 'default'.

        If it is to be used for piecemeal filling of the sparse blockchain
        headers file before the checkpoint height, it needs extra
        handling for the 'default' mode.

        A server interface does not get associated with a blockchain
        until it gets handled in the response to it's first header
        request.
        '''
        interface.logger.debug("requesting header %d", height)
        if height > bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT:
            params = [height]
        else:
            params = [height, bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT]
        self._queue_request('blockchain.block.header', params, interface)
        return True

    def _on_header(self, interface, request, response):
        '''Handle receiving a single block header'''
        result = response.get('result')
        if not result:
            interface.logger.error(response)
            self._connection_down(interface.server)
            return

        if not request:
            interface.logger.error("disconnecting server for sending unsolicited header, "
                                   "no request, params=%s", response['params'])
            self._connection_down(interface.server, blacklist=True)
            return
        request_params = request[1]
        height = request_params[0]

        response_height = response['params'][0]
        # This check can be removed if request/response params are reconciled in some sort
        # of rewrite.
        if height != response_height:
            interface.logger.error("unsolicited header request=%s request_height=%s "
                                   "response_height=%s", request_params, height, response_height)
            self._connection_down(interface.server)
            return

        proof_was_provided = False
        hexheader = None
        if 'root' in result and 'branch' in result and 'header' in result:
            hexheader = result["header"]
            if not self._validate_checkpoint_result(interface, result["root"],
                                                   result["branch"], hexheader, height):
                # Got checkpoint validation data, failed to provide proof.
                interface.logger.error("unprovable header request=%s height=%s",
                                       request_params, height)
                self._connection_down(interface.server)
                return
            proof_was_provided = True
        else:
            hexheader = result

        # Simple header request.
        header = blockchain.deserialize_header(bfh(hexheader), height)
        # Is there a blockchain that already includes this header?
        chain = blockchain.check_header(header)
        if interface.mode == Interface.MODE_BACKWARD:
            if chain:
                interface.logger.debug("binary search")
                interface.set_mode(Interface.MODE_BINARY)
                interface.blockchain = chain
                interface.good = height
                next_height = (interface.bad + interface.good) // 2
            else:
                # A backwards header request should not happen before the checkpoint
                # height. It isn't requested in this context, and it isn't requested
                # anywhere else. If this happens it is an error. Additionally, if the
                # checkpoint height header was requested and it does not connect, then
                # there's not much ElectrumSV can do about it (that we're going to
                # bother). We depend on the checkpoint being relevant for the blockchain
                # the user is running against.
                if height <= bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT:
                    self._connection_down(interface.server)
                    next_height = None
                else:
                    interface.bad = height
                    interface.bad_header = header
                    delta = interface.tip - height
                    # If the longest chain does not connect at any point we check to the
                    # chain this interface is serving, then we fall back on the checkpoint
                    # height which is expected to work.
                    next_height = max(bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT,
                                      interface.tip - 2 * delta)

        elif interface.mode == Interface.MODE_BINARY:
            if chain:
                interface.good = height
                interface.blockchain = chain
            else:
                interface.bad = height
                interface.bad_header = header
            if interface.bad != interface.good + 1:
                next_height = (interface.bad + interface.good) // 2
            elif not interface.blockchain.can_connect(interface.bad_header, check_height=False):
                self._connection_down(interface.server)
                next_height = None
            else:
                branch = self.blockchains.get(interface.bad)
                if branch is not None:
                    if branch.check_header(interface.bad_header):
                        interface.logger.debug('joining chain %s', interface.bad)
                        next_height = None
                    elif branch.parent().check_header(header):
                        interface.logger.debug('reorg %s %s', interface.bad, interface.tip)
                        interface.blockchain = branch.parent()
                        next_height = None
                    else:
                        interface.logger.debug('checkpoint conflicts with existing fork %s',
                                               branch.path())
                        branch.write(b'', 0)
                        branch.save_header(interface.bad_header)
                        interface.set_mode(Interface.MODE_CATCH_UP)
                        interface.blockchain = branch
                        next_height = interface.bad + 1
                        interface.blockchain.catch_up = interface.server
                else:
                    bh = interface.blockchain.height()
                    next_height = None
                    if bh > interface.good:
                        if not interface.blockchain.check_header(interface.bad_header):
                            b = interface.blockchain.fork(interface.bad_header)
                            self.blockchains[interface.bad] = b
                            interface.blockchain = b
                            interface.logger.debug("new chain %s", b.base_height)
                            interface.set_mode(Interface.MODE_CATCH_UP)
                            next_height = interface.bad + 1
                            interface.blockchain.catch_up = interface.server
                    else:
                        assert bh == interface.good
                        if interface.blockchain.catch_up is None and bh < interface.tip:
                            interface.logger.debug("catching up from %d", (bh + 1))
                            interface.set_mode(Interface.MODE_CATCH_UP)
                            next_height = bh + 1
                            interface.blockchain.catch_up = interface.server

                self._notify('updated')

        elif interface.mode == Interface.MODE_CATCH_UP:
            can_connect = interface.blockchain.can_connect(header)
            if can_connect:
                interface.blockchain.save_header(header)
                next_height = height + 1 if height < interface.tip else None
            else:
                # go back
                interface.logger.debug("cannot connect %d", height)
                interface.set_mode(Interface.MODE_BACKWARD)
                interface.bad = height
                interface.bad_header = header
                next_height = height - 1

            if next_height is None:
                # exit catch_up state
                interface.logger.debug('catch up done %d', interface.blockchain.height())
                interface.blockchain.catch_up = None
                self._switch_lagging_interface()
                self._notify('updated')
        elif interface.mode == Interface.MODE_DEFAULT:
            interface.logger.error("ignored header %d received in default mode, %d",
                                   height, result)
            return

        # If not finished, get the next header
        if next_height:
            if interface.mode == Interface.MODE_CATCH_UP and interface.tip > next_height:
                self._request_headers(interface, next_height, 2016)
            else:
                self._request_header(interface, next_height)
        else:
            interface.set_mode(Interface.MODE_DEFAULT)
            self._notify('updated')
        # refresh network dialog
        self._notify('interfaces')

    def maintain_requests(self):
        with self.interface_lock:
            interfaces = list(self.interfaces.values())
        for interface in interfaces:
            if interface.unanswered_requests and time.time() - interface.request_time > 20:
                # The last request made is still outstanding, and was over 20 seconds ago.
                interface.logger.error("blockchain request timed out")
                self._connection_down(interface.server)
                continue

    def wait_on_sockets(self):
        # Python docs say Windows doesn't like empty selects.
        # Sleep to prevent busy looping
        if not self.interfaces:
            time.sleep(0.1)
            return
        with self.interface_lock:
            interfaces = list(self.interfaces.values())
        rin = [i for i in interfaces]
        win = [i for i in interfaces if i.num_requests()]
        try:
            rout, wout, xout = select.select(rin, win, [], 0.1)
        except socket.error as e:
            # TODO: py3, get code from e
            code = None
            if code == errno.EINTR:
                return
            raise
        assert not xout
        for interface in wout:
            interface.send_requests()
        for interface in rout:
            self._process_responses(interface)

    def _init_headers_file(self):
        b = self.blockchains[0]
        filename = b.path()
        length = 80 * (bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT + 1)
        if not os.path.exists(filename) or os.path.getsize(filename) < length:
            with open(filename, 'wb') as f:
                if length>0:
                    f.seek(length-1)
                    f.write(b'\x00')
        util.ensure_sparse_file(filename)
        with b.lock:
            b.update_size()

    def run(self):
        b = self.blockchains[0]
        header = None
        if bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT is not None:
            self._init_headers_file()
            header = b.read_header(bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT)
        if header is not None:
            self.verified_checkpoint = True

        while self.is_running():
            self._maintain_sockets()
            self.wait_on_sockets()
            self.maintain_requests()
            if self.verified_checkpoint:
                self.run_jobs()    # Synchronizer and Verifier and Fx
            self._process_pending_sends()
        self._stop_network()
        self.on_stop()

    def _on_server_version(self, interface, version_data):
        interface.server_version = version_data

    def _on_notify_header(self, interface, header_dict):
        '''
        When we subscribe for 'blockchain.headers.subscribe', a server will send
        us it's topmost header.  After that, it will forward on any additional
        headers as it receives them.
        '''
        if 'hex' not in header_dict or 'height' not in header_dict:
            self._connection_down(interface.server)
            return

        header_hex = header_dict['hex']
        height = header_dict['height']
        header = blockchain.deserialize_header(bfh(header_hex), height)

        # If the server is behind the verification height, then something is wrong with
        # it.  Drop it.
        if (bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT is not None and
                height <= bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT):
            self._connection_down(interface.server)
            return

        # We will always update the tip for the server.
        interface.tip_header = header
        interface.tip = height

        if interface.mode == Interface.MODE_VERIFICATION:
            # If the server has already had this requested, this will be a no-op.
            self._request_initial_proof_and_headers(interface)
            return

        self._process_latest_tip(interface)

    def _process_latest_tip(self, interface):
        if interface.mode != Interface.MODE_DEFAULT:
            return

        header = interface.tip_header
        height = interface.tip

        b = blockchain.check_header(header) # Does it match the hash of a known header.
        if b:
            interface.blockchain = b
            self._switch_lagging_interface()
            self._notify('updated')
            self._notify('interfaces')
            return
        b = blockchain.can_connect(header) # Is it the next header on a given blockchain.
        if b:
            interface.blockchain = b
            b.save_header(header)
            self._switch_lagging_interface()
            self._notify('updated')
            self._notify('interfaces')
            return

        heights = [x.height() for x in self.blockchains.values()]
        tip = max(heights)
        if tip > bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT:
            interface.logger.debug("attempt to reconcile longest chain tip=%s heights=%s",
                                   tip, heights)
            interface.set_mode(Interface.MODE_BACKWARD)
            interface.bad = height
            interface.bad_header = header
            self._request_header(interface, min(tip, height - 1))
        else:
            interface.logger.debug("attempt to catch up tip=%s heights=%s", tip, heights)
            chain = self.blockchains[0]
            if chain.catch_up is None:
                chain.catch_up = interface
                interface.set_mode(Interface.MODE_CATCH_UP)
                interface.blockchain = chain
                interface.logger.debug("switching to catchup mode %s", tip)
                self._request_header(interface,
                                    bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT + 1)
            else:
                interface.logger.debug("chain already catching up with %s", chain.catch_up.server)

    def _request_initial_proof_and_headers(self, interface):
        # This will be the initial topmost header response.  But we might get new blocks.
        if interface.server not in self.checkpoint_servers_verified:
            interface.logger.debug("_request_initial_proof_and_headers pending")

            top_height = self.checkpoint_height
            # If there is no known checkpoint height for this network, we look to get
            # a given number of confirmations for the same conservative height.
            if self.checkpoint_height is None:
                self.checkpoint_height = interface.tip - 100
            self.checkpoint_servers_verified[interface.server] = {
                'root': None, 'height': self.checkpoint_height }
            # We need at least 147 headers before the post checkpoint headers for daa calculations.
            self.__request_headers(interface, self.checkpoint_height - 147 + 1,
                                  147, self.checkpoint_height)
        else:
            # We already have them verified, maybe we got disconnected.
            interface.logger.debug("_request_initial_proof_and_headers bypassed")
            interface.set_mode(Interface.MODE_DEFAULT)
            self._process_latest_tip(interface)

    def _apply_successful_verification(self, interface, checkpoint_height, checkpoint_root):
        known_roots = [v['root'] for v in self.checkpoint_servers_verified.values()
                       if v['root'] is not None]
        if len(known_roots) > 0 and checkpoint_root != known_roots[0]:
            interface.logger.error("server sent inconsistent root '%s'", checkpoint_root)
            self._connection_down(interface.server)
            return False
        self.checkpoint_servers_verified[interface.server]['root'] = checkpoint_root

        # rt12 --- checkpoint generation currently disabled.
        if False:
            interface.logger.debug("received verification %s", self.verifications_required)
            self.verifications_required -= 1
            if self.verifications_required > 0:
                return False

            if bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT is None:
                bitcoin.NetworkConstants.VERIFICATION_BLOCK_HEIGHT = checkpoint_height
                bitcoin.NetworkConstants.VERIFICATION_BLOCK_MERKLE_ROOT = checkpoint_root

                network_name = "TESTNET" if bitcoin.NetworkConstants.TESTNET else "MAINNET"
                interface.logger.debug(
                    "found verified checkpoint for %s at height %s with merkle root %r",
                    network_name, checkpoint_height, checkpoint_root)

        if not self.verified_checkpoint:
            self._init_headers_file()
            self.verified_checkpoint = True

        interface.logger.debug("server was verified correctly")
        interface.set_mode(Interface.MODE_DEFAULT)
        return True

    def _validate_checkpoint_result(self, interface, merkle_root, merkle_branch,
                                   header, header_height):
        '''
        header: hex representation of the block header.
        merkle_root: hex representation of the server's calculated merkle root.
        branch: list of hex representations of the server's calculated merkle root branches.

        Returns a boolean to represent whether the server's proof is correct.
        '''
        received_merkle_root = bytes(reversed(bfh(merkle_root)))
        if bitcoin.NetworkConstants.VERIFICATION_BLOCK_MERKLE_ROOT:
            expected_merkle_root = bytes(reversed(bfh(
                bitcoin.NetworkConstants.VERIFICATION_BLOCK_MERKLE_ROOT)))
        else:
            expected_merkle_root = received_merkle_root

        if received_merkle_root != expected_merkle_root:
            interface.logger.error("Sent unexpected merkle root, expected: '%s', got: '%s'",
                                   bitcoin.NetworkConstants.VERIFICATION_BLOCK_MERKLE_ROOT,
                                   merkle_root)
            return False

        header_hash = Hash(bfh(header))
        byte_branches = [ bytes(reversed(bfh(v))) for v in merkle_branch ]
        proven_merkle_root = blockchain.root_from_proof(header_hash, byte_branches, header_height)
        if proven_merkle_root != expected_merkle_root:
            interface.logger.error("Sent incorrect merkle branch, expected: '%s', proved: '%s'",
                                   bitcoin.NetworkConstants.VERIFICATION_BLOCK_MERKLE_ROOT,
                                   util.hfu(reversed(proven_merkle_root)))
            return False

        return True

    def blockchain(self):
        if self.interface and self.interface.blockchain is not None:
            self.blockchain_index = self.interface.blockchain.base_height
        return self.blockchains[self.blockchain_index]

    def get_blockchains(self):
        out = {}
        for k, b in self.blockchains.items():
            r = [i for i in self.interfaces.values() if i.blockchain==b]
            if r:
                out[k] = r
        return out

    # Called by gui.qt.network_dialog.py:follow_branch()
    def follow_chain(self, index):
        blockchain = self.blockchains.get(index)
        if blockchain:
            self.blockchain_index = index
            self.config.set_key('blockchain_index', index)
            with self.interface_lock:
                interfaces = list(self.interfaces.values())
            for i in interfaces:
                if i.blockchain == blockchain:
                    self.switch_to_interface(i.server, self.SWITCH_FOLLOW_CHAIN)
                    break
        else:
            raise BaseException('blockchain not found', index)

        with self.interface_lock:
            if self.interface:
                server = self.interface.server
                host, port, protocol, proxy, auto_connect = self.get_parameters()
                host, port, protocol = server.split(':')
                self.set_parameters(host, port, protocol, proxy, auto_connect)

    # Called by daemon.py:run_daemon()
    # Called by verifier.py:run()
    # Called by gui.qt.main_window.py:update_status()
    # Called by gui.qt.network_dialog.py:update()
    # Called by wallet.py:sweep()
    def get_local_height(self):
        return self.blockchain().height()

    # Called by gui.qt.main_window.py:do_process_from_txid()
    # Called by wallet.py:append_utxos_to_inputs()
    # Called by scripts/get_history.py
    def synchronous_get(self, request, timeout=30):
        q = queue.Queue()
        self.send([request], q.put)
        try:
            r = q.get(True, timeout)
        except queue.Empty:
            raise BaseException('Server did not answer')
        if r.get('error'):
            raise BaseException(r.get('error'))
        return r.get('result')

    @staticmethod
    def __wait_for(it):
        """Wait for the result of calling lambda `it`."""
        q = queue.Queue()
        it(q.put)
        try:
            result = q.get(block=True, timeout=30)
        except queue.Empty:
            raise util.TimeoutException(_('Server did not answer'))

        if result.get('error'):
            # Text should not be sanitized before user display
            raise RPCError(result['error'])

        return result.get('result')

    @staticmethod
    def __with_default_synchronous_callback(invocation, callback):
        """ Use this method if you want to make the network request
        synchronous. """
        if not callback:
            return Network.__wait_for(invocation)

        invocation(callback)

    # Called by commands.py:broadcast()
    # Called by main_window.py:broadcast_transaction()
    def broadcast_transaction(self, transaction):
        command = 'blockchain.transaction.broadcast'
        invocation = lambda c: self.send([(command, [str(transaction)])], c)
        our_txid = transaction.txid()

        try:
            their_txid = Network.__wait_for(invocation)
        except RPCError as e:
            msg = sanitized_broadcast_message(e.args[0])
            return False, _('transaction broadcast failed: ') + msg
        except util.TimeoutException:
            return False, e.args[0]

        if their_txid != our_txid:
            try:
                their_txid = int(their_txid, 16)
            except ValueError:
                return False, _('bad server response; it is unknown whether '
                                'the transaction broadcast succeeded')
            logging.warning(f'server TxID {their_txid} differs from '
                            f'ours {our_txid}')

        return True, our_txid

    # Called by verifier.py:run()
    def get_merkle_for_transaction(self, tx_hash, tx_height, callback=None):
        command = 'blockchain.transaction.get_merkle'
        invocation = lambda c: self.send([(command, [tx_hash, tx_height])], c)

        return Network.__with_default_synchronous_callback(invocation, callback)


def sanitized_broadcast_message(error):
    unknown_reason = _('reason unknown')
    try:
        msg = str(error['message'])
    except:
        msg = ''   # fall-through

    if 'dust' in msg:
        return _('very small "dust" payments')
    if ('Missing inputs' in msg or 'Inputs unavailable' in msg or
        'bad-txns-inputs-spent' in msg):
        return _('missing, already-spent, or otherwise invalid coins')
    if 'insufficient priority' in msg:
        return _('insufficient fees or priority')
    if 'bad-txns-premature-spend-of-coinbase' in msg:
        return _('attempt to spend an unmatured coinbase')
    if 'txn-already-in-mempool' in msg or 'txn-already-known' in msg:
        return _("it already exists in the server's mempool")
    if 'txn-mempool-conflict' in msg:
        return _("it conflicts with one already in the server's mempool")
    if 'bad-txns-nonstandard-inputs' in msg:
        return _('use of non-standard input scripts')
    if 'absurdly-high-fee' in msg:
        return _('fee is absurdly high')
    if 'non-mandatory-script-verify-flag' in msg:
        return _('the script fails verification')
    if 'tx-size' in msg:
        return _('transaction is too large')
    if 'scriptsig-size' in msg:
        return _('it contains an oversized script')
    if 'scriptpubkey' in msg:
        return _('it contains a non-standard signature')
    if 'bare-multisig' in msg:
        return _('it contains a bare multisig input')
    if 'multi-op-return' in msg:
        return _('it contains more than 1 OP_RETURN input')
    if 'scriptsig-not-pushonly' in msg:
        return _('a scriptsig is not simply data')

    logging.debug(f'server error (untrusted): {error}')
    return unknown_reason
