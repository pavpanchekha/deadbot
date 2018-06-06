#!/bin/python3

import math
import urllib.request
import http.server
import json
import codecs
import shlex
import argparse
import datetime
import collections
import pickle
import os
import threading
import contextlib

date = datetime.date
from _secret_birthbot import HOOK, TOKEN

def to_slack(msg : str):
    req = urllib.request.Request(HOOK, data=json.dumps({"text": msg}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as res:
        if res.getcode() == 200:
            return
        else:
            raise IOError("Scary reponse from Slack", res)

class BirthdayRequestHandler(http.server.BaseHTTPRequestHandler):
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

def start_server(port:int = 57001):
    httpd = http.server.HTTPServer(("", port), BirthdayRequestHandler)
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

Birthday = collections.namedtuple("Birthday", ["when", "announcements"])

class Birthdays:
    def __init__(self):
        self.birthdays = None
        self._birthdays = {}
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def lock(self):
        self._lock.acquire()
        self.birthdays = self._birthdays
        yield
        self._birthdays = self.birthdays
        self.birthdays = None
        self._lock.release()

    def unlocked(self):
        return not self.birthdays

    def save(self):
        with open("birthbot.pickle", "wb") as fd:
            pickle.dump(self.birthdays, fd)

    def load(self):
        if os.path.exists("birthbot.pickle"):
            with open("birthbot.pickle", "rb") as fd:
                self.birthdays = pickle.load(fd)
                print("Loaded birthdays of {} people".format(len(self.birthdays)))

    def get(self, name):
        return self.birthdays[name]

    def set(self, name, date):
        self.birthdays[name] = Birthday(date, [])

    def delete(self, name):
        del self.birthdays[name]

    @staticmethod
    def next(bday, when):
        next = bday.when.replace(year=when.year)
        if next < when or next == when and when in bday.announcements:
            next = next.replace(year=next.year + 1)
        return next

    def upcoming(self, when):
        out = [(user, Birthdays.next(bday, when)) for user, bday in self.birthdays.items()]
        return sorted(out, key=lambda x: x[1])

    def all(self):
        return sorted(self.birthdays.items())

DATA = Birthdays()

def describe_who(who):
    if who:
        names = ", ".join(["<@{}>".format(name) for name in who])
    else:
        names = "No one"
    return names

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
    now = date.today()
    out = []
    for name, bday in DATA.all():
        inst = bday.when.replace(year=now.year)
        if inst != now or inst in bday.announcements: continue
        bday.announcements.append(inst)
        out.append(name)
    return out

def days_ago(d):
    n = round((d - date.today()) / datetime.timedelta(days=1))
    return ("1 day" if n == 1 else "{} days".format(n))

def print_date(date):
    return "{:%d %b} ({})".format(date, days_ago(date))

def make_announcements():
    who = new_announcements()
    if who:
        print("Announcing birthdays for", who)
        to_slack("Happy birthday {}!".format(describe_who(who)))

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

def parse_date(s):
    fmts = ["%Y-%m-%d", "%m/%d", "%m-%d", "%m/%d/%Y", "%m/%d/%y"]
    for fmt in fmts:
        try:
            d = datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
        else:
            return d
    raise ValueError("Could not parse date {}".format(s))

class Commands:
    @command(["user"], "set", ["date"])
    def set_user(user, date):
        """Declare someone's birthday"""
        uid = parse_uid(user)
        return Commands.set(uid, date)

    @command("set", ["date"], uid=True)
    def set(uid, d):
        """Declare your birthday"""
        when = parse_date(d)
        DATA.set(uid, when)
        return Ephemeral("Thanks, <@{}>! I'll remember that.".format(uid))

    @command(["user"], "unset")
    def unset_user(user):
        """Delete someone's birthday from the database"""
        uid = parse_uid(user)
        return Commands.unset(uid)

    @command(["user"], "unset", uid=True)
    def unset(uid, user):
        """Delete your birthday from the database"""
        return DATA.delete(uid)

    @command("when", ["user"])
    def when(user):
        """When is someone's birthday?"""
        uid = parse_uid(user)
        when = Birthdays.next(DATA.get(uid), date.today())
        return Ephemeral("<@{}>'s next birthday is on {}".format(uid, print_date(when)))

    @command("upcoming", public=True)
    def upcoming():
        """List upcoming birthdays"""
        upcoming = DATA.upcoming(date.today())
        return Response("The following birthdays are coming up:\n" + "\n".join([
            "• <@{}> on {}".format(name, print_date(when))
            for name, when in upcoming]))

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
        s = "• `/birthday "
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
