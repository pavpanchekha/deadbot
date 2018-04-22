#!/bin/python3

import math
import urllib.request
import http.server
import json
import codecs
import shlex
import argparse
from datetime import datetime, timedelta
import arrow
import collections
import pickle
import os
import threading
import contextlib

from _secret import HOOK, TOKEN

def to_slack(msg : str):
    req = urllib.request.Request(HOOK, data=json.dumps({"text": msg}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as res:
        if res.getcode() == 200:
            return
        else:
            raise IOError("Scary reponse from Slack", res)

def to_sign(conf, time):
    data = urllib.parse.urlencode({ 'what': conf, 'when': '{:%Y-%m-%d %H:%M:%S GMT-0000}'.format(time) })
    URL = "http://plseaudio.cs.washington.edu:8087/deadline"
    req = urllib.request.Request(url=URL, data=data.encode("utf-8"), method="POST")
    with urllib.request.urlopen(req, timeout=15) as res:
        if res.getcode() == 200:
            return
        else:
            raise IOError("Scary reponse from PLSE Sign", res)

def to_unsign():
    URL = "http://plseaudio.cs.washington.edu:8087/restore_clock"
    req = urllib.request.Request(url=URL, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=15) as res:
        if res.getcode() == 200:
            return
        else:
            raise IOError("Scary reponse from PLSE Sign", res)

def parse_date(string, tz):
    tz_name = ({ "AOE": "Etc/GMT+12", "PT": "US/Pacific", "ET": "US/Eastern" }).get(tz, tz)
    no_tz = arrow.get(string, "YYYY-MM-DD HH:MM")
    return arrow.get(no_tz.datetime, tz).to("utc").datetime

def to_local(date):
    return arrow.get(date, "US/Pacific").datetime

class DeadlineRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))

        if data["token"] != [TOKEN]:
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)

        self.send_header("Content-Type", "application/json")
        self.end_headers()

        uid = data["user_id"][0]
        args = ns(shlex.split(data["text"][0]))

        with DATA.lock():
            try:
                response = handle(uid, args)
            except Exception as e:
                import traceback
                traceback.print_exc()
                response = Ephemeral("*{}*: {}".format(type(e).__name__, e))
        assert DATA.unlocked()

        if response:
            self.wfile.write(json.dumps({
                "response_type": "in_channel" if isinstance(response, Response) else "ephemeral",
                "text": str(response)
            }).encode("utf-8"))
        else:
            self.wfile.write(json.dumps({"response_type": "in_channel"}).encode("utf-8"))

def start_server(port:int = 57005):
    httpd = http.server.HTTPServer(("", port), DeadlineRequestHandler)
    httpd.serve_forever()

class Ephemeral(str): pass
class Response(str): pass

class ns:
    def __init__(self, args):
        self.args = args

    def match(self, *pat):
        out = []
        if len(pat) != len(self.args):
            return False
        for val, arg in zip(pat, self.args):
            if isinstance(val, list):
                out.append(arg)
            elif val == arg:
                continue
            else:
                return False
        self.vars = out
        return True

Conference = collections.namedtuple("Conference", ["when", "who", "announcements"])

class Deadlines:
    def __init__(self):
        self.deadlines = None
        self._deadlines = {}
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def lock(self):
        self._lock.acquire()
        self.deadlines = self._deadlines
        yield
        self._deadlines = self.deadlines
        self.deadlines = None
        self._lock.release()

    def unlocked(self):
        return not self.deadlines

    def save(self):
        with open("data.pickle", "wb") as fd:
            pickle.dump(self.deadlines, fd)

    def load(self):
        if os.path.exists("data.pickle"):
            with open("data.pickle", "rb") as fd:
                self.deadlines = pickle.load(fd)
                print("Loaded data about {} conferences".format(len(self.deadlines)))

    def get_conf(self, name, when):
        opts = self.deadlines.get(name, [])
        confs = [conf for conf in opts if when < conf.when]
        if not confs: raise KeyError("No conference {}".format(name))
        return min(confs, key=lambda x: x.when)

    def get_conf_idx(self, name, when):
        opts = self.deadlines.get(name, [])
        confs = [(i, conf) for i, conf in enumerate(opts) if when < conf.when]
        if not confs: raise KeyError("No conference {}".format(name))
        return min(confs, key=lambda x: x[1].when)

    def set(self, name, when, uid):
        self.get_conf(name, when).who.add(uid)
        self.save()

    def unset(self, name, when, uid):
        self.get_conf(name, when).who.remove(uid)
        self.save()

    def add(self, name, when):
        self.deadlines.setdefault(name, []).append(Conference(when, set(), []))
        self.save()

    def modify(self, name, old_when, new_when):
        idx, conf = self.get_conf_idx(name, old_when)
        self.deadlines[name][idx] = Conference(new_when, conf.who, conf.announcements)
        self.save()

    def remove(self, name, when):
        idx, conf = self.get_conf_idx(name, when)
        del self.deadlines[name][idx]
        self.save()

    def who(self, name, when):
        return self.get_conf(name, when).who

    def when(self, name, when):
        return self.get_conf(name, when).when

    def upcoming(self, when):
        out = []
        for name, opts in self.deadlines.items():
            confs = list(filter(lambda conf: when < conf.when, opts))
            if confs:
                out.append((name, min(confs, key=lambda x: x.when)))
        return sorted(out, key=lambda x: x[1].when)

    def all(self):
        out = []
        for name, opts in self.deadlines.items():
            for opt in opts:
                out.append((name, opt))
        return out

DATA = Deadlines()

def describe_who(who, conf):
    if who:
        names = ", ".join(["<@{}>".format(name) for name in who])
    else:
        names = "No one"
    return names + " " + ("are" if len(who) > 1 else "is") + " submitting to " + conf_name(conf)

def parse_uid(user):
    assert user[0] == "<"
    assert user[-1] == ">"
    assert user[1] == "@"
    return user[2:-1].split("|")[0]

COMMANDS = []

def handle(uid, args):
    for pattern, opts, f in COMMANDS:
        if args.match(*pattern):
            print(" ".join(args.args), "(executing in {})".format(f.__name__))
            if opts["uid"]:
                return f(uid, *args.vars)
            else:
                return f(*args.vars)
    else:
        return Ephemeral("I couldn't understand that command\n\n" + help())

def command(*pattern, uid=False, public=False):
    def decorator(f):
        COMMANDS.append((pattern, { "uid": uid, "public": public }, f))
        return f
    return decorator

def new_announcements():
    now = datetime.utcnow()
    announce_days = [28, 21, 14, 7, 6, 5, 4, 3, 2, 1, 0]
    out = []
    for name, conf in DATA.all():
        if conf.when < now: continue
        if conf.when > now + timedelta(days=max(announce_days)): continue
        if not conf.who: continue
        announce = False
        for days in announce_days:
            if conf.when < now + timedelta(days=days) \
               and days not in conf.announcements:
                announce = True
                conf.announcements.append(days)
        if announce:
            out.append((name, conf))
    return out

def days_ago(date):
    n = round((date - datetime.utcnow()) / timedelta(days=1))
    return ("1 day" if n == 1 else "{} days".format(n))

def conf_name(conf):
    return conf.upper() if conf.islower() else conf

def print_utcdate(date):
    return "{:%d %b at %H:%M} ({})".format(arrow.get(date).to("US/Pacific").datetime, days_ago(date))

def make_announcements():
    now = datetime.utcnow()
    for name, conf in new_announcements():
        print("Announcing", name, "on", conf.when)
        delta = math.ceil((conf.when - now) / timedelta(days=1))
        who = ", ".join(["<@{}>".format(uid) for uid in conf.who])
        if delta == 0:
            to_slack("{} deadline! Congrats to everyone who submitted!".format(name))
        else:
            to_slack("{} is in {}! Good luck {}".format(name, days_ago(conf.when), who))

def start_announcement_thread():
    with DATA.lock():
        try:
            make_announcements()
        except Exception as e:
            import traceback
            traceback.print_exc()
        DATA.save()

    assert DATA.unlocked()

    FREQUENCY = 60 * 60
    timer = threading.Timer(FREQUENCY, start_announcement_thread)
    timer.start()
    return timer

class Commands:
    @command(["user"], "set", ["conf"])
    def set_user(user, conf):
        """Declare that someone is submitting to a conference"""
        uid2 = parse_uid(user)
        return Commands.set(uid2, conf)

    @command("set", ["conf"], uid=True)
    def set(uid, conf):
        """Declare that you are submitting to a conference"""
        conf = conf_name(conf)
        try:
            DATA.set(conf, datetime.utcnow(), uid)
            return Response("Good luck, <@{}>, on {}!".format(uid, conf))
        except ValueError:
            return Ephemeral("Conference {} does not yet exit. Please `/deadline add` it.".format(conf))

    @command(["user"], "unset", ["conf"])
    def unset_user(user, conf):
        """Declare that someone is no longer submitting to a conference"""
        uid = parse_uid(user)
        conf = conf_name(conf)
        try:
            DATA.unset(conf, datetime.utcnow(), uid)
            return Ephemeral("Sorry to hear that.")
        except ValueError:
            return Ephemeral("Conference {} does not yet exit. Please `/deadline add` it.".format(conf))

    @command("unset", ["conf"], uid=True)
    def unset(uid, conf):
        """Declare that you are no longer submitting to a conference"""
        conf = conf_name(conf)
        try:
            DATA.unset(conf, datetime.utcnow(), uid)
            return Ephemeral("Patience is bitter, but its fruit is sweet, <@{}>!".format(uid, conf))
        except ValueError:
            return Ephemeral("Conference {} does not yet exit. Please `/deadline add` it.".format(conf))

    @command("add", ["conf"], ["date"], ["time"], ["tz"])
    def add_tz(conf, date, time, tz):
        """Add a conference"""
        conf = conf_name(conf)
        when = parse_date(date + " " + time, tz)
        DATA.add(conf, when)
        return Response("Added {} on {}.\nDon't forget to `/deadline set` some submitters.".format(conf, print_utcdate(when)))

    @command("add", ["conf"], ["date"], ["time"])
    def add(conf, date, time):
        """Add a conference"""
        return Commands.add_tz(conf, date, time, "PT")

    @command("modify", ["conf"], ["date"], ["time"], ["tz"])
    def modify_tz(conf, date, time, tz):
        """Change when a conference is"""
        conf = conf_name(conf)
        when = parse_date(date + " " + time, tz)
        DATA.modify(conf, datetime.utcnow(), when)
        return Response("Set {} to be on {}".format(conf, print_utcdate(when)))

    @command("modify", ["conf"], ["date"], ["time"])
    def modify(conf, date, time):
        """Change when a conference is"""
        return Commands.modify_tz(conf, date, time, "PT")

    @command("remove", ["conf"])
    def remove(conf):
        """Delete a conference is"""
        conf = conf_name(conf)
        now = datetime.utcnow()
        who = DATA.who(conf, now)
        when = DATA.when(conf, now)
        if who: raise ValueError("Conference {} cannot be removed; {}".format(conf, describe_who(who, conf)))
        DATA.remove(conf, now)
        return Ephemeral("Removed {} on {}".format(conf, print_utcdate(when)))

    @command("when", ["conf"])
    def when(conf):
        """When is a conference?"""
        conf = conf_name(conf)
        when = DATA.when(conf, datetime.utcnow())
        return Ephemeral("{} is on {}".format(conf, print_utcdate(when)))

    @command("who", ["conf"])
    def who(conf):
        """Who is submitting to a conference?"""
        conf = conf_name(conf)
        who = DATA.who(conf, datetime.utcnow())
        return Ephemeral(describe_who(who, conf))

    @command("upcoming", public=True)
    def upcoming():
        """List upcoming deadlines"""
        upcoming = DATA.upcoming(datetime.utcnow())
        return Response("The following deadlines are coming up:\n" + "\n".join([
            "• {} on {}".format(name, print_utcdate(conf.when))
            for name, conf in upcoming]))

    @command("announce", ["conf"], public=True)
    def announce(conf):
        """Announce who is submitting to a conference"""
        conf = conf_name(conf)
        who = DATA.who(conf, datetime.utcnow())
        return Response(describe_who(who, conf))

    @command("sign", ["conf"], public=True)
    def sign(conf):
        """Announce who is submitting to a conference"""
        conf = conf_name(conf)
        when = DATA.when(conf, datetime.utcnow())
        to_sign(conf, when)
        return Ephemeral("{} on {} sent to sign!".format(conf, print_utcdate(when)))

    @command("sign")
    def sign():
        """Announce who is submitting to a conference"""
        to_unsign()
        return Ephemeral("Sign restored")

    @command("help")
    def help():
        return Ephemeral(help())

FORMATS = {
    "user": "@USER",
    "date": "YYYY-MM-DD",
    "time": "HH:MM",
}

def help():
    private = "I understand the following commands:\n\n"
    public = "Public announcement commands:\n\n"
    for pattern, opts, f in COMMANDS:
        if not f.__doc__: continue
        s = "• `/deadline "
        for val in pattern:
            if isinstance(val, list):
                s += FORMATS.get(val[0], val[0].upper())
            else:
                s += val
            s += " "
        s += "` — " + str(f.__doc__) + "\n"
        if opts["public"]:
            public += s
        else:
            private += s
    return private + "\n" + public

if __name__ == "__main__":
    with DATA.lock():
        DATA.load()
    assert DATA.unlocked()

    try:
        announce_thread = start_announcement_thread()
        try:
            start_server()
        except KeyboardInterrupt:
            announce_thread.cancel()
    finally:
        with DATA.lock():
            DATA.save()
