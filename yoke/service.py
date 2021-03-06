from zeroconf import ServiceBrowser, Zeroconf, InterfaceChoice
import ipaddress
import logging
import socket
import sys
from time import sleep
from zeroconf import ServiceInfo, Zeroconf

import time
import socket
from time import sleep, time
from platform import system
from threading import Thread, Event
import sys
import json
import argparse
import atexit

from yoke import events as EVENTS


def get_ip_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    return ip
    
from glob import glob

GAMEPAD_EVENTS = (
    EVENTS.ABS_X,
    EVENTS.ABS_Y,
    EVENTS.ABS_RX,
    EVENTS.ABS_RY,
    EVENTS.ABS_HAT0X,
    EVENTS.ABS_HAT0Y,
    EVENTS.BTN_GAMEPAD,
    EVENTS.BTN_SOUTH,
    EVENTS.BTN_EAST,
    EVENTS.BTN_DPAD_DOWN,
    EVENTS.BTN_DPAD_RIGHT,
    EVENTS.BTN_DPAD_UP,
    EVENTS.BTN_DPAD_LEFT,
    EVENTS.BTN_TR,
    EVENTS.BTN_TL,
    EVENTS.BTN_START,
    EVENTS.BTN_SELECT,
    EVENTS.BTN_MODE,
    )




ABS_EVENTS = [getattr(EVENTS, n) for n in dir(EVENTS) if n.startswith("ABS_")]

class Device:
    def __init__(self, id=1, name="Yoke", events=GAMEPAD_EVENTS):
        self.name = name + '-' + str(id)
        for fn in glob('/sys/class/input/js*/device/name'):
            with open(fn) as f:
                fname = f.read().split()[0]  # need to split because there seem to be newlines
                if name == fname:
                    raise AttributeError('Device name "{}" already taken. Set another name with --name NAME'.format(name))

        # set range (0, 255) for abs events
        self.events = events
        events = [e + (0, 255, 0, 0) if e in ABS_EVENTS else e for e in events]

        BUS_VIRTUAL = 0x06
        import uinput

        self.device = uinput.Device(events, name, BUS_VIRTUAL)

    def emit(self, d, v):
        if d not in self.events:
            raise AttributeError("Event {} has not been registered.".format(d))
        if d in ABS_EVENTS:
            v = (v+1)/2 * 255
        self.device.emit(d, int(v), syn=False)

    def flush(self):
        self.device.syn()

    def close(self):
        self.device.destroy()


# Override on Windows
if system() is 'Windows':
    # print("Warning: This is not well tested on Windows!")

    from yoke.vjoy.vjoydevice import VjoyConstants, VjoyDevice

    # ovverride EVENTS with the correct constants
    for k in vars(EVENTS):
        setattr(EVENTS, k, getattr(VjoyConstants, k, None))

    class Device:
        def __init__(self, id=1, name='Yoke', events=GAMEPAD_EVENTS):
            super().__init__()
            self.name = name + '-' + id
            self.device = VjoyDevice(id)
            self.events = events
        def emit(self, d, v):
            if d is not None:
                if d in range(1, 8+1):
                    self.device.set_button(d, v)
                else:
                    self.device.set_axis(d, int((v+1)/2 * 32768))
        def flush(self):
            pass
        def close(self):
            self.device.close()


zeroconf = Zeroconf()


# Webserver to serve files to android client
from http.server import HTTPServer, SimpleHTTPRequestHandler
from threading import Thread
import socketserver
import os, urllib, posixpath

class HTTPRequestHandler(SimpleHTTPRequestHandler):
    basepath = os.getcwd()

    def translate_path(self, path):
        """Translate a /-separated PATH to the local filename syntax."""
        # abandon query parameters
        path = path.split('?',1)[0]
        path = path.split('#',1)[0]
        # Don't forget explicit trailing slash when normalizing. Issue17324
        trailing_slash = path.rstrip().endswith('/')
        try:
            path = urllib.parse.unquote(path, errors='surrogatepass')
        except UnicodeDecodeError:
            path = urllib.parse.unquote(path)
        path = posixpath.normpath(path)
        words = path.split('/')
        words = filter(None, words)
        path = self.basepath
        for word in words:
            if os.path.dirname(word) or word in (os.curdir, os.pardir):
                # Ignore components that are not a simple file/directory name
                continue
            path = os.path.join(path, word)
        if trailing_slash:
            path += '/'
        return path

def run_webserver(port, path):
    print('starting webserver on ', port, path)
    class RH(HTTPRequestHandler):
        basepath = path
    with socketserver.TCPServer(('', port), RH) as httpd:
        httpd.serve_forever()


DEFAULT_CLIENT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "joypad")

class Service:
    dev = None
    sock = None
    info = None
    dt = 0.02

    def __init__(self, dev, port=0, client_path=DEFAULT_CLIENT_PATH):
        self.dev = dev
        self.port = port
        self.client_path = client_path
        
    def make_events(self, values):
        """returns a (event_code, value) tuple for each value in values
        values are in (-1, 1) and should be returned in (-1, 1)
        """
        raise NotImplementedError()
    
    def preprocess(self, message):
        _, *v, _ = message.split(b',')  # first and last value is nothing
        _, *v = v  # first real value (from accelerometer) is not important yet
        # import pdb; pdb.set_trace()
        v = [float(m) for m in v]
        v = (
                (v[0]/9.81 - 0)    * 1.5,
                (v[1]/9.81 - 0.52) * 3.0,
                v[2] * 2 - 1,
                v[3] * 2 - 1,
                v[4] * 2 - 1,
                v[5] * 2 - 1,
            ) + tuple(v[6:])
        return v

    def run(self):
        atexit.register(self.close_atexit)

        # open udp socket on random available port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 128)  # small buffer for low latency
        self.sock.bind((get_ip_address(), self.port))
        self.sock.settimeout(0)
        adr, port = self.sock.getsockname()
        self.port = port

        Thread(target=run_webserver, args=(self.port, self.client_path), daemon=True).start()

        # create zeroconf service
        stype = "_yoke._udp.local."
        netname = socket.gethostname() + '-' + self.dev.name
        fullname = netname + '.' + stype
        self.info = ServiceInfo(stype, fullname, socket.inet_aton(adr), port, 0, 0, {}, fullname)
        zeroconf.register_service(self.info, ttl=10)
        while True:
            print('To connect select "{}" on your device,'.format(netname))
            print('or connect manually to "{}:{}"'.format(adr, port))
            trecv = time()
            irecv = 0
            connection = None

            while True:
                try:
                    m, address = self.sock.recvfrom(128)

                    if connection is None:
                        print('Connected to ', address)
                        connection = address

                    if connection == address:
                        trecv = time()
                        irecv = 0
                        v = self.preprocess(m)
                        for e in self.make_events(v):
                            self.dev.emit(*e)
                        self.dev.flush()

                    else:
                        pass  # ignore packets from other addresses
                    
                except (socket.timeout, socket.error):
                    pass
                
                tdelta = time() - trecv

                if connection is not None and tdelta > 3:
                    print('Timeout (3 seconds), disconnected.')
                    print('  (listened {} times per second)'.format(int(irecv/tdelta)))
                    break

                sleep(self.dt)
                irecv += 1

    def close_atexit(self):
        print("Yoke: Unregistering zeroconf service...")
        self.close()

    def close(self):
        atexit.unregister(self.close_atexit)
        if self.dev is not None:
            self.dev.close()
        if self.sock is not None:
            self.sock.close()
        if self.info is not None:
            zeroconf.unregister_service(self.info)
