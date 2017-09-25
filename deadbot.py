
import urllib.request
import http.server
import json
import codecs
import shlex
import argparse
from datetime import datetime
import collections
import pickle
import os

HOOK = "https://hooks.slack.com/services/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"
TOKEN = "NZLjPrrU9rlVvdHsrILIsD4J"

def post(msg : str):
    req = urllib.request.Request(HOOK, data=json.dumps({"text": msg}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as res:
        if res.getcode() == 200:
            return
        else:
            raise IOError("Scary reponse from Slack", res)

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
        response = handle(uid, args)
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
    class Var: pass

    def __init__(self, args):
        self.args = args

    def match(self, *pat):
        out = []
        if len(pat) != len(self.args):
            return False
        for val, arg in zip(pat, self.args):
            if isinstance(val, ns.Var):
                out.append(arg)
            elif val == arg:
                continue
            else:
                return False
        self.vars = out
        return True

__ = ns.Var()

Conference = collections.namedtuple("Conference", ["when", "who"])

class Deadlines:
    def __init__(self):
        self.deadlines = {}

    def save(self):
        with open("data.pickle", "wb") as fd:
            pickle.dump(self.deadlines, fd)

    def load(self):
        if os.path.exists("data.pickle"):
            with open("data.pickle", "rb") as fd:
                self.deadlines = pickle.load(fd)

    def set(self, name, when, uid):
        opts = self.deadlines.setdefault(name.upper(), [])
        confs = filter(lambda conf: when < conf.when, opts)
        if not confs: raise ValueError
        conf = min(confs, key=lambda x: x.when)
        conf.who.add(uid)
        self.save()

    def unset(self, name, when, uid):
        opts = self.deadlines.setdefault(name.upper(), [])
        confs = filter(lambda conf: when < conf.when, opts)
        if not confs: raise ValueError
        conf = min(confs, key=lambda x: x.when)
        conf.who.remove(uid)
        self.save()

    def add(self, name, when):
        self.deadlines.setdefault(name.upper(), []).append(Conference(when, set()))
        self.save()

    def who(self, name, when):
        opts = self.deadlines.setdefault(name.upper(), [])
        confs = filter(lambda conf: when < conf.when, opts)
        if not confs: raise ValueError
        conf = min(confs, key=lambda x: x.when)
        return conf.who

    def upcoming(self, when):
        out = []
        for name, opts in self.deadlines.items():
            confs = list(filter(lambda conf: when < conf.when, opts))
            if confs:
                out.append((name, min(confs, key=lambda x: x.when)))
        return sorted(out, key=lambda x: x[1].when)

DATA = Deadlines()

def describe_who(who, conf):
    return ", ".join(["<@{}>".format(name) for name in who]) + " " + ("are" if len(who) > 1 else "is") + " submitting to " + conf.upper()

def parse_uid(user):
    assert user[0] == "<"
    assert user[-1] == ">"
    assert user[1] == "@"
    return user[2:-1].split("|")[0]

def handle(uid, args):
    if args.match(__, "set", __):
        user, conf = args.vars
        uid = parse_uid(user)
        try:
            DATA.set(conf, datetime.now(), uid)
            return Response("Good luck, <@{}>, on {}!".format(uid, conf.upper()))
        except ValueError:
            return Ephemeral("Conference {} does not yet exit. Please `/deadline add` it.".format(conf.upper()))
    elif args.match("set", __):
        conf, = args.vars
        try:
            DATA.set(conf, datetime.now(), uid)
            return Response("Good luck, <@{}>, on {}!".format(uid, conf.upper()))
        except ValueError:
            return Ephemeral("Conference {} does not yet exit. Please `/deadline add` it.".format(conf.upper()))
    elif args.match(__, "unset", __):
        user, conf = args.vars
        uid = parse_uid(user)
        try:
            DATA.unset(conf, datetime.now(), uid)
            return Ephemeral("Sorry to hear that.".format(uid, conf.upper()))
        except ValueError:
            return Ephemeral("Conference {} does not yet exit. Please `/deadline add` it.".format(conf.upper()))
    elif args.match("unset", __):
        conf, = args.vars
        try:
            DATA.unset(conf, datetime.now(), uid)
            return Ephemeral("Patience is bitter, but its fruit is sweet, <@{}>!".format(uid, conf.upper()))
        except ValueError:
            return Ephemeral("Conference {} does not yet exit. Please `/deadline add` it.".format(conf.upper()))
    elif args.match("add", __, __, __):
        conf, date, time = args.vars
        when = datetime.strptime(date + " " + time, "%Y-%m-%d %H:%M")
        DATA.add(conf, when)
        return Ephemeral("Added {} on {} at {}".format(conf, when.strftime("%d %b"), when.strftime("%H:%M")))
    elif args.match("upcoming"):
        upcoming = DATA.upcoming(datetime.now())
        return Response("The following deadlines are coming up: " + ", ".join([
            "{} on {}".format(name, conf.when.strftime("%d %b")) for name, conf in upcoming
        ]))
    elif args.match("who", __):
        conf, = args.vars
        who = DATA.who(conf, datetime.now())
        return Ephemeral(describe_who(who, conf))
    elif args.match("announce", __):
        conf, = args.vars
        who = DATA.who(conf, datetime.now())
        return Response(describe_who(who, conf))
    elif args.match("help"):
        return Ephemeral(help())
    else:
        return Ephemeral("I couldn't understand that command\n\n" + help())

def help():
    return """I understand the following commands:

• `/deadline [@user] set <conf>` — Declare that you are submitting to <conf>
• `/deadline add <conf> <YYYY-MM-DD> <HH:MM>` — Add a conference, with date and time
• `/deadline who <conf>` — Who is submitting to <conf>

Public announcement commands:

• `/deadline upcoming` — List upcoming deadlines
• `/deadline announce <conf>` — Announce who is submitting to <conf>
"""

if __name__ == "__main__":
    DATA.load()
    try:
        start_server()
    finally:
        DATA.save()
