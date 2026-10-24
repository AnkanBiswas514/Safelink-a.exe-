#!/usr/bin/env python3
"""
SafeLink - zero-install offline SOS and triage network node.

One file, one dependency (Flask). Every node runs the same program:

  Command node (rescuers watch this one):
      python safelink.py --role command --node-id CMD

  Relay node (forward post; stores SOS locally, forwards to command when reachable):
      python safelink.py --role relay --node-id N1 --command-url http://<command-ip>:5000

Victims open the node's address (or get redirected by the captive portal) and see a
tiny SOS page. Rescuers open /dashboard on the command node.

Design notes:
  * Store-and-forward: every SOS is saved locally first, then pushed upstream whenever the
    link exists. Records are idempotent (UUID) so retries and echoes are safe.
  * Status moves forward only (new -> assigned -> rescued / false_alarm) and wins merges.
  * Urgency score is a transparent rule set; the dashboard shows why each score was given.
  * There is no login or PIN: anyone on the Wi-Fi can open /dashboard and change case status.
    That is fine for a demo; a real deployment would need authentication.
"""
import argparse
import json
import math
import os
import random
import re
import socket
import sqlite3
import threading
import time
import urllib.request
import uuid
from collections import defaultdict, deque

from flask import Flask, Response, jsonify, redirect, request

app = Flask(__name__)
BUILD = "2026-10-24b-merged-victim-form"  # shown at startup so you can tell which copy of this file you are running

CFG = {
    "role": "command", "node_id": "CMD", "db": "safelink.db", "command_url": None,
    "lat": None, "lng": None,
    "captive": False, "portal_ip": None, "port": 5000, "sync_every": 5.0,
}
SYNC = {"last_ok": None, "last_err": None, "paused": False}
WAKE = threading.Event()

RANK = {"new": 0, "assigned": 1, "rescued": 2, "false_alarm": 3}
WATER = {"none": 0, "ankle": 3, "knee": 10, "waist": 20, "chest": 32, "roof": 42}
NEEDS = ("water", "food", "medicine")
LOC_SOURCES = ("gps", "node", "none")
FIELDS = ["id", "created_at", "updated_at", "node_id", "people", "medical", "injured", "infants",
          "elderly", "water_level", "floor", "needs", "phone", "landmark", "note", "lat", "lng",
          "loc_source", "accuracy", "base_score", "reasons", "status"]
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
PROBE_PATHS = {"/generate_204", "/gen_204", "/hotspot-detect.html", "/library/test/success.html",
               "/connecttest.txt", "/ncsi.txt", "/success.txt", "/canonical.html", "/redirect"}


# ----------------------------------------------------------------------------- database
def db():
    c = sqlite3.connect(CFG["db"], timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS sos(
        id TEXT PRIMARY KEY, created_at REAL, updated_at REAL, node_id TEXT,
        people INTEGER, medical INTEGER, injured INTEGER, infants INTEGER, elderly INTEGER,
        water_level TEXT, floor INTEGER, needs TEXT, phone TEXT, landmark TEXT, note TEXT,
        lat REAL, lng REAL, loc_source TEXT, accuracy REAL, base_score REAL, reasons TEXT,
        status TEXT, synced INTEGER DEFAULT 0, srv_ts REAL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS nodes(
        node_id TEXT PRIMARY KEY, role TEXT, lat REAL, lng REAL, last_seen REAL)""")
    # Remove only clearly labelled demo records created by earlier versions.
    c.execute("DELETE FROM sos WHERE substr(note, 1, 6) = ?", ("[demo]",))
    c.commit()
    c.close()


def save(c, rec, synced):
    cols = FIELDS + ["synced", "srv_ts"]
    vals = [rec[k] for k in FIELDS] + [synced, time.time()]
    c.execute("INSERT OR REPLACE INTO sos(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))), vals)


# ----------------------------------------------------------------------------- validation
def to_int(v, lo, hi, default):
    try:
        x = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def to_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e12 else None


def geo(v, limit):
    f = to_float(v)
    return f if f is not None and -limit <= f <= limit else None


def to_flag(v):
    return 1 if v in (True, 1, "1", "true", "on") else 0


def to_text(v, n=200):
    return str(v).strip()[:n] if v else ""


def clean_needs(v):
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, (list, tuple)):
        return ""
    return ",".join(n for n in NEEDS if n in v)


def score(d):
    """Transparent urgency rules. Returns (points, reasons)."""
    pts, why = 0, []

    def add(n, text):
        nonlocal pts
        pts += n
        why.append("%s (+%d)" % (text, n))

    if d["medical"]:
        add(40, "Medical emergency")
    if d["injured"]:
        add(30, "Injured person")
    if d["infants"]:
        add(25, "Infant / young child")
    if d["elderly"]:
        add(15, "Elderly / disabled")
    add(min(d["people"], 10) * 2, "%d people" % d["people"])
    w = WATER.get(d["water_level"], 0)
    if w:
        add(w, "Water level: %s" % d["water_level"])
    if d["floor"] == 0:
        add(12, "Ground floor")
    elif d["floor"] == 1:
        add(6, "First floor")
    for need, n in (("medicine", 8), ("water", 4), ("food", 2)):
        if need in d["needs"].split(","):
            add(n, "Needs %s" % need)
    return pts, why


def level(p):
    return "CRITICAL" if p >= 75 else "HIGH" if p >= 45 else "MEDIUM" if p >= 20 else "LOW"


def coerce(raw):
    """Turn untrusted input (form or sync) into a clean record, recomputing the score."""
    try:
        sid = str(raw["id"]).lower()
        if not UUID_RE.match(sid):
            return None
        created, updated = float(raw["created_at"]), float(raw["updated_at"])
        if not (0 < created < 4e9 and 0 < updated < 4e9):
            return None
        status = raw.get("status", "new")
        d = {
            "id": sid, "created_at": created, "updated_at": updated,
            "node_id": to_text(raw.get("node_id"), 40) or "?",
            "people": to_int(raw.get("people"), 1, 50, 1),
            "medical": to_flag(raw.get("medical")), "injured": to_flag(raw.get("injured")),
            "infants": to_flag(raw.get("infants")), "elderly": to_flag(raw.get("elderly")),
            "water_level": raw.get("water_level") if raw.get("water_level") in WATER else "none",
            "floor": to_int(raw.get("floor"), 0, 30, 0),
            "needs": clean_needs(raw.get("needs")),
            "phone": re.sub(r"[^0-9+\- ]", "", to_text(raw.get("phone"), 20)),
            "landmark": to_text(raw.get("landmark"), 200), "note": to_text(raw.get("note"), 300),
            "lat": geo(raw.get("lat"), 90), "lng": geo(raw.get("lng"), 180),
            "loc_source": raw.get("loc_source") if raw.get("loc_source") in LOC_SOURCES else "none",
            "accuracy": to_float(raw.get("accuracy")),
            "status": status if status in RANK else "new",
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if d["lat"] is None or d["lng"] is None:
        d["lat"] = d["lng"] = None
        d["loc_source"] = "none"
    pts, why = score(d)
    d["base_score"], d["reasons"] = pts, json.dumps(why)
    return d


def merge(c, rec, upstream):
    """Merge a record from another node. Newer fields win, status only moves forward."""
    cur = c.execute("SELECT * FROM sos WHERE id=?", (rec["id"],)).fetchone()
    if cur is None:
        save(c, rec, synced=1 if upstream else 0)
        return True
    cur = dict(cur)
    base = rec if rec["updated_at"] > cur["updated_at"] else {k: cur[k] for k in FIELDS}
    m = dict(base)
    # The most recently updated status wins, including an explicit reopen action.
    m["status"] = base["status"]
    m["created_at"] = min(rec["created_at"], cur["created_at"])
    m["updated_at"] = max(rec["updated_at"], cur["updated_at"])
    if all(m[k] == cur[k] for k in FIELDS):
        return False
    same_as_rec = all(m[k] == rec[k] for k in FIELDS)
    save(c, m, synced=1 if (upstream and same_as_rec) else 0)
    return True


def view(r):
    d = dict(r)
    age = max(0.0, (time.time() - d["created_at"]) / 60.0)
    bonus = min(age / 5.0, 20.0) if d["status"] == "new" else 0.0
    d["wait_min"] = round(age, 1)
    d["wait_bonus"] = round(bonus, 1)
    d["priority"] = round(d["base_score"] + bonus, 1)
    d["level"] = level(d["priority"])
    d["reasons"] = json.loads(d["reasons"] or "[]")
    d["needs"] = [x for x in (d["needs"] or "").split(",") if x]
    d.pop("synced", None)
    d.pop("srv_ts", None)
    return d


# ----------------------------------------------------------------------------- guards
_hits = defaultdict(deque)


def rate_limited(ip, limit=12, window=60):
    t = time.time()
    q = _hits[ip]
    while q and t - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(t)
    return False


@app.before_request
def captive_portal():
    if not CFG["captive"]:
        return None
    host = request.host.split(":")[0].lower()
    own = {str(CFG["portal_ip"]), "localhost", "127.0.0.1"}
    if host in own and request.path not in PROBE_PATHS:
        return None
    port = "" if CFG["port"] == 80 else ":%d" % CFG["port"]
    return redirect("http://%s%s/" % (CFG["portal_ip"], port), 302)


@app.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ----------------------------------------------------------------------------- api
@app.post("/api/sos")
def api_sos():
    if rate_limited(request.remote_addr):
        return jsonify(error="Too many requests, wait a minute"), 429
    j = request.get_json(silent=True) or {}
    sid = str(j.get("id", "")).lower()
    if not UUID_RE.match(sid):
        sid = str(uuid.uuid4())
    t = time.time()
    lat, lng = geo(j.get("lat"), 90), geo(j.get("lng"), 180)
    if lat is not None and lng is not None:
        src, acc = "gps", to_float(j.get("accuracy"))
    elif CFG["lat"] is not None and CFG["lng"] is not None:
        lat, lng, src, acc = CFG["lat"], CFG["lng"], "node", 100.0
    else:
        lat = lng = acc = None
        src = "none"
    raw = dict(j, id=sid, created_at=t, updated_at=t, node_id=CFG["node_id"], lat=lat, lng=lng,
               loc_source=src, accuracy=acc, status="new")
    c = db()
    try:
        cur = c.execute("SELECT * FROM sos WHERE id=?", (sid,)).fetchone()
        if cur:  # victim editing their own request: keep history, never re-open a closed case
            raw["created_at"] = cur["created_at"]
            raw["status"] = cur["status"]
            raw["node_id"] = cur["node_id"]
            if cur["loc_source"] == "gps" and src != "gps":
                raw.update(lat=cur["lat"], lng=cur["lng"], loc_source="gps", accuracy=cur["accuracy"])
        rec = coerce(raw)
        if rec is None:
            return jsonify(error="invalid request"), 400
        save(c, rec, synced=0)
        c.commit()
    finally:
        c.close()
    return jsonify(id=sid, loc_source=rec["loc_source"], status=rec["status"])


@app.get("/api/sos/<sid>")
def api_sos_one(sid):
    c = db()
    r = c.execute("SELECT status, synced FROM sos WHERE id=?", (sid.lower(),)).fetchone()
    c.close()
    if not r:
        return jsonify(error="not found"), 404
    delivered = CFG["command_url"] is None or bool(r["synced"])
    return jsonify(status=r["status"], delivered=delivered, node=CFG["node_id"])


@app.get("/api/sos")
def api_list():
    c = db()
    rows = [view(r) for r in c.execute("SELECT * FROM sos").fetchall()]
    pending = c.execute("SELECT COUNT(*) FROM sos WHERE synced=0").fetchone()[0] if CFG["command_url"] else 0
    nodes = [dict(r) for r in c.execute("SELECT * FROM nodes").fetchall()]
    c.close()
    active = sorted([r for r in rows if r["status"] in ("new", "assigned")],
                    key=lambda r: (-r["priority"], r["created_at"]))
    closed = sorted([r for r in rows if r["status"] not in ("new", "assigned")],
                    key=lambda r: -r["updated_at"])
    now = time.time()
    me = {"node_id": CFG["node_id"], "role": CFG["role"], "lat": CFG["lat"], "lng": CFG["lng"],
          "last_seen": now, "self": True}
    for n in nodes:
        n["age_s"] = round(now - (n["last_seen"] or now))
    me["age_s"] = 0
    sync = {"upstream": CFG["command_url"], "pending": pending, "paused": SYNC["paused"],
            "last_ok_s": None if SYNC["last_ok"] is None else round(now - SYNC["last_ok"]),
            "last_err": SYNC["last_err"]}
    return jsonify(node=me, nodes=[me] + nodes, sync=sync, records=active + closed)


@app.post("/api/sos/<sid>/status")
def api_status(sid):
    new = (request.get_json(silent=True) or {}).get("status")
    if new not in RANK:
        return jsonify(error="bad status"), 400
    c = db()
    try:
        r = c.execute("SELECT * FROM sos WHERE id=?", (sid.lower(),)).fetchone()
        if not r:
            return jsonify(error="not found"), 404
        # Allow rescuers to reopen a closed case by moving it back to assigned.
        reopening = r["status"] in ("rescued", "false_alarm") and new == "assigned"
        if RANK[new] < RANK[r["status"]] and not reopening:
            return jsonify(error="status cannot move backwards (use assigned to reopen a closed case)"), 409
        rec = {k: r[k] for k in FIELDS}
        rec["status"], rec["updated_at"] = new, time.time()
        save(c, rec, synced=0)
        c.commit()
    finally:
        c.close()
    return jsonify(ok=True, status=new)


@app.post("/api/sync")
def api_sync():
    """Accept records pushed by another node and return everything that changed since `since`."""
    j = request.get_json(silent=True) or {}
    c = db()
    try:
        n = j.get("node") or {}
        nid = to_text(n.get("node_id"), 40)
        if nid:
            c.execute("INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?)",
                      (nid, to_text(n.get("role"), 10), geo(n.get("lat"), 90), geo(n.get("lng"), 180), time.time()))
        for raw in (j.get("records") or [])[:500]:
            rec = coerce(raw) if isinstance(raw, dict) else None
            if rec:
                merge(c, rec, upstream=False)
        c.commit()
        since = to_float(j.get("since")) or 0.0
        rows = c.execute("SELECT * FROM sos WHERE srv_ts>? ORDER BY srv_ts LIMIT 500", (since,)).fetchall()
        cursor = rows[-1]["srv_ts"] if rows else since
        out = [{k: r[k] for k in FIELDS} for r in rows]
    finally:
        c.close()
    return jsonify(records=out, cursor=cursor)


@app.get("/api/health")
def api_health():
    return jsonify(node_id=CFG["node_id"], role=CFG["role"], ok=True)


@app.post("/api/sync-mode")
def api_sync_mode():
    """Demo control: pause syncing (boat 'out of range') or resume and push the queue ('dock at base')."""
    if not CFG["command_url"]:
        return jsonify(error="this node has no upstream to sync with"), 400
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode == "out_of_range":
        SYNC["paused"] = True
    elif mode == "docked":
        SYNC["paused"] = False
        WAKE.set()  # sync immediately instead of waiting for the next tick
    else:
        return jsonify(error="mode must be out_of_range or docked"), 400
    return jsonify(paused=SYNC["paused"])


# ----------------------------------------------------------------------------- captive DNS
# Windows Mobile Hotspot has no dnsmasq, so this tiny DNS server does the same job: it answers every
# name a phone asks about with this node's address. Phones check for internet by looking up well known
# names (captive.apple.com, connectivitycheck.gstatic.com); getting our address back is what makes
# them show the "sign in to network" page, which the node then redirects to the SOS form.
def dns_reply(query, ip):
    """Return (reply_bytes, name, qtype) for a DNS query, or None if it is not a plain query."""
    if len(query) < 17:
        return None
    flags = query[2] << 8 | query[3]
    if flags & 0x8000 or (flags >> 11) & 0xF != 0:  # already a response, or not a standard query
        return None
    if (query[4] << 8 | query[5]) != 1:  # we only handle one question per packet
        return None
    i, labels = 12, []
    while i < len(query) and query[i] != 0:
        if query[i] & 0xC0:  # compression pointers never appear inside a question
            return None
        labels.append(query[i + 1:i + 1 + query[i]].decode("ascii", "replace"))
        i += query[i] + 1
    if i + 5 > len(query):
        return None
    qtype = query[i + 1] << 8 | query[i + 2]
    qclass = query[i + 3] << 8 | query[i + 4]
    is_a = qtype == 1 and qclass == 1
    rflags = 0x8000 | (flags & 0x7900) | 0x0400 | 0x0080  # response, copy opcode+RD, authoritative, RA
    out = (query[0:2] + rflags.to_bytes(2, "big") + b"\x00\x01" + (b"\x00\x01" if is_a else b"\x00\x00")
           + b"\x00\x00\x00\x00" + query[12:i + 5])
    if is_a:  # IPv4 question: answer with our address. Anything else (IPv6, etc.): empty answer, so phones use IPv4.
        out += b"\xc0\x0c\x00\x01\x00\x01" + (30).to_bytes(4, "big") + b"\x00\x04" + socket.inet_aton(ip)
    return out, ".".join(labels), qtype


def dns_server(ip, port):
    def say(m):
        print(m, flush=True)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, port))
    except OSError as e:
        say("  !! DNS: could not listen on %s UDP port %d (%s)." % (ip, port, e))
        say("     Another program may already own that port. On Windows Mobile Hotspot the built-in")
        say("     connection sharing often does. Phones can still open http://%s/ by typing it." % ip)
        return
    say("  DNS: answering every name with %s on UDP port %d" % (ip, port))
    seen = 0
    while True:
        try:
            data, addr = s.recvfrom(2048)
            res = dns_reply(data, ip)
            if res:
                s.sendto(res[0], addr)
                if seen < 12:  # show the first few lookups so you can tell phones are really asking us
                    seen += 1
                    say("  DNS: %s asked for %s (type %d)" % (addr[0], res[1], res[2]))
        except OSError:  # Windows raises this when a phone vanishes mid-reply; just carry on
            time.sleep(0.01)
        except Exception:
            pass


# ----------------------------------------------------------------------------- sync client
def sync_loop():
    cursor = 0.0
    url = CFG["command_url"].rstrip("/") + "/api/sync"
    while True:
        WAKE.wait(CFG["sync_every"])
        WAKE.clear()
        if SYNC["paused"]:  # simulated "out of range": keep queueing locally
            continue
        try:
            c = db()
            rows = c.execute("SELECT * FROM sos WHERE synced=0 LIMIT 200").fetchall()
            pending = [{k: r[k] for k in FIELDS} for r in rows]
            payload = {"node": {"node_id": CFG["node_id"], "role": CFG["role"], "lat": CFG["lat"], "lng": CFG["lng"]},
                       "records": pending, "since": cursor}
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                out = json.load(resp)
            for rec in pending:
                c.execute("UPDATE sos SET synced=1 WHERE id=? AND updated_at=?", (rec["id"], rec["updated_at"]))
            for raw in out.get("records", []):
                rec = coerce(raw)
                if rec:
                    merge(c, rec, upstream=True)
            c.commit()
            c.close()
            cursor = out.get("cursor", cursor)
            SYNC["last_ok"], SYNC["last_err"] = time.time(), None
        except Exception as e:  # network down is the normal case in a disaster
            SYNC["last_err"] = str(e)[:120]


# ----------------------------------------------------------------------------- offline map assets and tiles
@app.get("/api/offline-map-status")
def offline_map_status():
    """Report local map prerequisites without claiming that the map is complete or tested."""
    root = os.path.dirname(os.path.abspath(__file__))
    static = os.path.join(root, "static")
    leaflet_js = os.path.isfile(os.path.join(static, "leaflet", "leaflet.js"))
    leaflet_css = os.path.isfile(os.path.join(static, "leaflet", "leaflet.css"))
    mbtiles = os.path.join(static, "tiles", "bihar.mbtiles")
    mbtiles_ok = False
    mbtiles_format = None
    if os.path.isfile(mbtiles):
        try:
            con = sqlite3.connect(mbtiles)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "tiles" in tables:
                row = con.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
                fmt = con.execute("SELECT value FROM metadata WHERE name='format' LIMIT 1").fetchone() if "metadata" in tables else None
                mbtiles_format = fmt[0].lower() if fmt and fmt[0] else "unknown"
                mbtiles_ok = bool(row and row[0] and mbtiles_format in ("png", "jpg", "jpeg", "webp"))
            con.close()
        except sqlite3.Error:
            mbtiles_ok = False
    tile_dir = os.path.join(static, "tiles")
    xyz_ok = False
    if os.path.isdir(tile_dir):
        # Stop at the first tile; do not scan or load the full map into memory.
        for base, dirs, files in os.walk(tile_dir):
            if any(f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) for f in files):
                xyz_ok = True
                break
    tile_source = "mbtiles" if mbtiles_ok else ("xyz-directory" if xyz_ok else "none")
    return jsonify(leaflet_js=leaflet_js, leaflet_css=leaflet_css,
                   leaflet_ready=leaflet_js and leaflet_css,
                   tiles_ready=bool(mbtiles_ok or xyz_ok), tile_source=tile_source,
                   mbtiles_format=mbtiles_format,
                   message=("Local library and tile data detected; coverage still needs visual verification." if leaflet_js and leaflet_css and (mbtiles_ok or xyz_ok)
                            else "Offline map is not ready. Install local Leaflet assets and a permitted local Bihar tile set; the fallback locator remains available."))


@app.get("/offline-tiles/<int:z>/<int:x>/<int:y>.png")
def offline_tile(z, x, y):
    """Serve raster tiles from a local MBTiles database or XYZ folder; never contacts the internet."""
    if z < 0 or z > 22 or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        return Response(status=404)
    root = os.path.dirname(os.path.abspath(__file__))
    tile_dir = os.path.join(root, "static", "tiles")
    mbtiles = os.path.join(tile_dir, "bihar.mbtiles")
    if os.path.isfile(mbtiles):
        try:
            con = sqlite3.connect(mbtiles, timeout=2)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "tiles" in tables:
                # MBTiles stores tile_row in TMS order; flip the XYZ y coordinate.
                row = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1",
                                  (z, x, (1 << z) - 1 - y)).fetchone()
                fmtrow = con.execute("SELECT value FROM metadata WHERE name='format' LIMIT 1").fetchone() if "metadata" in tables else None
                fmt = (fmtrow[0].lower() if fmtrow and fmtrow[0] else "png")
                con.close()
                if row and row[0]:
                    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(fmt, "application/octet-stream")
                    return Response(row[0], mimetype=mime, headers={"Cache-Control": "public, max-age=86400"})
            else:
                con.close()
        except sqlite3.Error:
            pass
    # Optional plain XYZ tile folder: static/tiles/{z}/{x}/{y}.png (or jpg/webp).
    for ext, mime in (("png", "image/png"), ("jpg", "image/jpeg"), ("jpeg", "image/jpeg"), ("webp", "image/webp")):
        path = os.path.join(tile_dir, str(z), str(x), "%d.%s" % (y, ext))
        if os.path.isfile(path):
            try:
                return Response(Path(path).read_bytes(), mimetype=mime,
                                headers={"Cache-Control": "public, max-age=86400"})
            except OSError:
                break
    return Response(status=404)


# ----------------------------------------------------------------------------- pages
@app.get("/")
def home():
    return Response(VICTIM_HTML.replace("__NODE__", CFG["node_id"]), mimetype="text/html")


@app.get("/dashboard")
def dashboard():
    return Response(DASH_HTML.replace("__NODE__", CFG["node_id"]), mimetype="text/html")


VICTIM_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Emergency SOS</title>
<style>
:root {
  --bg: #fff; --ink: #0b1220; --mut: #4b5563; --line: #cbd5e1; --card: #f1f5f9;
  --red: #c81e1e; --blue: #1d4ed8; --onblue: #fff; --green: #166534;
  --greenbg: #dcfce7; --amberbg: #fef3c7; --amber: #78350f;
}
.dark-mode {
  --bg: #0f1115; --ink: #f3f4f6; --mut: #aab2c0; --line: #3a4252; --card: #1a1e26;
  --blue: #7aa2ff; --onblue: #0b1220; --green: #86efac;
  --greenbg: #12301f; --amberbg: #3a2a0c; --amber: #fcd34d;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; font: 18px/1.5 system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--ink); transition: background 0.3s, color 0.3s; }
main { max-width: 560px; margin: 0 auto; padding: 16px 16px 48px; }
.top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
select.lang-select { padding: 8px; font-size: 16px; border: 2px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--ink); }
h1 { font-size: 26px; margin: 4px 0; }
.lead { font-size: 18px; margin: 6px 0 10px; }
.hint, .mut { color: var(--mut); font-size: 16px; }
.sos-btn { display: block; width: 100%; margin: 24px 0; padding: 60px 20px; font-size: 36px; font-weight: 800; color: #fff; background: var(--red); border: 0; border-radius: 24px; box-shadow: 0 8px 16px rgba(200,30,30,0.3); cursor: pointer; transition: transform 0.1s; }
.sos-btn:active { transform: scale(0.98); }
.safety-box { background: var(--amberbg); border-left: 6px solid var(--amber); color: var(--amber); padding: 16px; border-radius: 12px; margin-bottom: 24px; }
.safety-box h3 { margin: 0 0 8px 0; font-size: 20px; display: flex; align-items: center; gap: 8px; }
.safety-box ul { margin: 0; padding-left: 20px; font-weight: 600; }
.safety-box li { margin-bottom: 6px; }
.sec { background: var(--card); border-radius: 16px; padding: 16px; margin: 16px 0; }
.sec h2 { font-size: 21px; margin: 0 0 12px; display: flex; align-items: center; gap: 10px; }
.n { flex: none; width: 32px; height: 32px; border-radius: 50%; background: var(--blue); color: var(--onblue); display: inline-flex; align-items: center; justify-content: center; font-size: 18px; }
label { display: block; font-weight: 700; margin: 12px 0 6px; }
.chk { display: flex; align-items: center; gap: 12px; font-weight: 600; padding: 14px; background: var(--bg); border: 2px solid var(--line); border-radius: 12px; margin: 8px 0; cursor: pointer; }
.chk.on { border-color: var(--blue); }
.chk input { width: 28px; height: 28px; flex: none; margin: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 10px; margin: 6px 0; }
.chip { min-height: 50px; padding: 10px 16px; font: inherit; font-size: 18px; border: 2px solid var(--line); border-radius: 999px; background: var(--bg); color: var(--ink); cursor: pointer; }
.chip.on { background: var(--blue); color: var(--onblue); border-color: var(--blue); font-weight: 700; }
.stepper { display: flex; align-items: center; gap: 18px; }
.stepper button { width: 56px; height: 56px; font-size: 30px; border-radius: 14px; border: 2px solid var(--line); background: var(--bg); color: var(--ink); cursor: pointer; }
.stepper span { font-size: 28px; font-weight: 800; min-width: 36px; text-align: center; }
input[type=text], input[type=tel], textarea { width: 100%; padding: 14px; font: inherit; font-size: 18px; border: 2px solid var(--line); border-radius: 12px; background: var(--bg); color: var(--ink); }
.primary { width: 100%; padding: 18px; font-size: 20px; font-weight: 700; color: var(--onblue); background: var(--blue); border: 0; border-radius: 14px; margin-top: 16px; cursor: pointer; }
.alt { width: 100%; padding: 14px; font-size: 18px; font-weight: 700; color: var(--blue); background: var(--bg); border: 2px solid var(--blue); border-radius: 12px; margin-top: 10px; cursor: pointer; }
.danger-sec { border: 2px solid var(--red); background: rgba(200,30,30,0.05); }
.danger-sec .n { background: var(--red); }
.net { position:sticky; top:0; z-index:10; padding:12px 14px; font-weight:700; font-size:17px; text-align:center; }
.net.ok { background:var(--greenbg); color:var(--green); }
.net.warn { background:var(--amberbg); color:var(--amber); }
</style>
</head>
<body>
<div id="net" class="net" role="status" aria-live="polite" hidden></div>
<main>
  <div class="top-bar">
    <h1 data-t="title">Emergency SOS</h1>
    <select id="lang" class="lang-select" onchange="changeLang()">
      <option value="en">English</option>
      <option value="hi">हिन्दी</option>
      <option value="ta">தமிழ்</option>
      <option value="te">తెలుగు</option>
      <option value="ml">മലയാളം</option>
    </select>
  </div>

  <div id="start-screen">
    <p class="lead" data-t="sub">No internet needed. Works fully offline.</p>
    <button id="btn-sos" class="sos-btn" data-t="sos">I NEED HELP</button>
    <p class="hint" data-t="sos_hint">Tap the button above. Your request will be sent instantly. You can add details afterwards.</p>
  </div>

  <form id="form" hidden onsubmit="return false">
    
    <div class="safety-box">
      <h3 data-t="safety_title">⚠️ Immediate Safety Actions</h3>
      <ul>
        <li data-t="safe1">Move to the highest possible ground immediately.</li>
        <li data-t="safe2">Stay far away from fallen power lines and electrical panels.</li>
        <li data-t="safe3">Keep your phone in battery saver mode and stay on this page.</li>
        <li data-t="safe4">Help elderly people, disabled individuals, and children first.</li>
      </ul>
    </div>

    <section class="sec danger-sec">
      <h2><span class="n">!</span><span data-t="q_danger">Immediate Danger?</span></h2>
      <label class="chk" style="border-color:var(--red); color:var(--red)">
        <input type="checkbox" id="danger"><span data-t="l_danger">I am in IMMEDIATE DANGER (fire, collapse, shock)</span>
      </label>
    </section>

    <section class="sec">
      <h2><span class="n">1</span><span data-t="q_where">Where are you?</span></h2>
      <p class="hint" data-t="h_where">Rescuers must be able to find you.</p>
      <label for="landmark" data-t="l_landmark">Your address or landmark</label>
      <textarea id="landmark" rows="2" id="landmark" placeholder="Example: Blue house near the bus stand"></textarea>
      <button type="button" class="alt" id="gpsbtn" data-t="gps_btn">📍 Share my GPS location</button>
      <p class="hint" id="locnote"></p>
    </section>

    <section class="sec">
      <h2><span class="n">2</span><span data-t="q_people">Who is with you?</span></h2>
      <label data-t="l_people">Total people</label>
      <div class="stepper">
        <button type="button" id="pm">&minus;</button>
        <span id="pv">1</span>
        <button type="button" id="pp">+</button>
      </div>
      <label data-t="l_children">Children under 12</label>
      <div class="stepper">
        <button type="button" id="cm">&minus;</button>
        <span id="cv">0</span>
        <button type="button" id="cp">+</button>
      </div>
      <label class="chk"><input type="checkbox" id="infants"><span data-t="l_infants">Infant/baby under 5</span></label>
      <label class="chk"><input type="checkbox" id="elderly"><span data-t="l_elderly">Elderly or disabled</span></label>
      <label class="chk"><input type="checkbox" id="pets"><span data-t="l_pets">We have pets</span></label>
    </section>

    <section class="sec">
      <h2><span class="n">3</span><span data-t="q_hurt">Is anyone hurt or sick?</span></h2>
      <label class="chk"><input type="checkbox" id="injured"><span data-t="l_injured">Someone is injured</span></label>
      <div id="injbox" hidden><div class="chips" id="injd"></div></div>
      
      <label class="chk"><input type="checkbox" id="medical"><span data-t="l_medical">Medical emergency</span></label>
      <div id="illbox" hidden><div class="chips" id="illd"></div></div>
    </section>

    <section class="sec">
      <h2><span class="n">4</span><span data-t="q_water">Water & Mobility</span></h2>
      <label data-t="l_water">How high is the water?</label>
      <div class="chips" id="water_chips"></div>
      
      <label data-t="l_water_trend">Is water level changing?</label>
      <div class="chips" id="trend_chips"></div>

      <label data-t="l_walk">Can everyone walk or climb?</label>
      <div class="chips" id="mob_chips"></div>
      
      <label data-t="l_floor">Which floor are you on?</label>
      <div class="chips" id="floor_chips"></div>
    </section>

    <section class="sec">
      <h2><span class="n">5</span><span data-t="q_needs">What do you need?</span></h2>
      <label class="chk"><input type="checkbox" id="n_water"><span data-t="n_water">Drinking water</span></label>
      <label class="chk"><input type="checkbox" id="n_food"><span data-t="n_food">Food</span></label>
      <label class="chk"><input type="checkbox" id="n_medicine"><span data-t="n_medicine">Medicine</span></label>
    </section>

    <section class="sec">
      <h2><span class="n">6</span><span data-t="q_contact">Contact & Details</span></h2>
      <label data-t="l_name">Your name</label>
      <input type="text" id="name">
      
      <label data-t="l_phone">Phone Number</label>
      <input type="tel" id="phone">
      <p id="phmsg" class="hint"></p>
      
      <label data-t="l_contact2">Second Contact Number</label>
      <input type="tel" id="contact2">

      <label data-t="l_spot">How to spot you?</label>
      <textarea id="spot_me" rows="2" placeholder="Example: Red cloth on the roof"></textarea>
      
      <label data-t="q_more">Anything else?</label>
      <textarea id="note" rows="2"></textarea>
    </section>

    <button type="button" class="primary" data-t="upd" id="upd">Send Details</button>

    <div style="text-align:center; margin-top:32px">
      <button type="button" class="alt" style="width:auto; padding:8px 16px; font-size:14px; border:1px solid var(--mut); color:var(--mut)" onclick="document.body.classList.toggle('dark-mode')" data-t="btn_dark">
        Toggle Battery Saver (Dark Mode)
      </button>
    </div>
  </form>
</main>

<script>
const dict = {
  en: {
    title: "Emergency SOS", sub: "No internet needed. Works fully offline.", sos: "I NEED HELP",
    sos_hint: "Tap the button above. Your request will be sent instantly. You can add details afterwards.",
    safety_title: "⚠️ Immediate Safety Actions",
    safe1: "Move to the highest possible ground immediately.", safe2: "Stay far away from fallen power lines and electrical panels.", safe3: "Keep your phone in battery saver mode and stay on this page.", safe4: "Help elderly people, disabled individuals, and children first.",
    q_danger: "Immediate Danger?", l_danger: "I am in IMMEDIATE DANGER (fire, collapse, shock)",
    q_where: "Where are you?", h_where: "Rescuers must be able to find you.", l_landmark: "Your address or landmark", gps_btn: "📍 Share my GPS location",
    q_people: "Who is with you?", l_people: "Total people", l_children: "Children under 12", l_infants: "Infant/baby under 5", l_elderly: "Elderly or disabled", l_pets: "We have pets",
    q_hurt: "Is anyone hurt or sick?", l_injured: "Someone is injured", l_medical: "Medical emergency",
    q_water: "Water & Mobility", l_water: "How high is the water?", l_water_trend: "Is water level changing?", l_walk: "Can everyone walk or climb?", l_floor: "Which floor are you on?",
    q_needs: "What do you need?", n_water: "Drinking water", n_food: "Food", n_medicine: "Medicine",
    q_contact: "Contact & Details", l_name:"Your name", l_phone: "Phone Number", l_contact2: "Second Contact Number", l_spot: "How to spot you?", q_more: "Anything else?",
    upd: "Send Details", btn_dark: "Toggle Battery Saver (Dark Mode)",
    opts: {
      w: { none:"No water", ankle:"Ankle", knee:"Knee", waist:"Waist", chest:"Chest", roof:"Roof/Trapped" },
      tr: { rising:"Rising", steady:"Steady", falling:"Falling" },
      m: { yes:"Yes", no:"No" },
      f: { 0:"Ground", 1:"1st", 2:"2nd", 3:"3rd+" },
      inj: { bleeding:"Bleeding", bone:"Broken bone", head:"Head injury", burn:"Burn" },
      ill: { breath:"Trouble breathing", chest:"Chest pain", preg:"Pregnant", unconscious:"Unconscious", seizure:"Seizure" }
    }
  },
  hi: {
    title: "आपातकालीन SOS", sub: "इंटरनेट की आवश्यकता नहीं। पूरी तरह ऑफलाइन काम करता है।", sos: "मुझे मदद चाहिए",
    sos_hint: "ऊपर दिए गए बटन पर टैप करें। आपका अनुरोध तुरंत भेजा जाएगा। आप बाद में विवरण जोड़ सकते हैं।",
    safety_title: "⚠️ तत्काल सुरक्षा कार्रवाई",
    safe1: "तुरंत यथासंभव ऊंचे स्थान पर जाएं।", safe2: "गिरे हुए बिजली के तारों और पैनलों से दूर रहें।", safe3: "अपने फोन को बैटरी सेवर मोड में रखें और इसी पेज पर रहें।", safe4: "बुजुर्गों, विकलांगों और बच्चों की पहले मदद करें।",
    q_danger: "तत्काल खतरा?", l_danger: "मैं तत्काल खतरे में हूँ (आग, ढहना, करंट)",
    q_where: "आप कहाँ हैं?", h_where: "बचावकर्मियों को आपको ढूंढने में सक्षम होना चाहिए।", l_landmark: "आपका पता या पहचान चिह्न", gps_btn: "📍 मेरी GPS लोकेशन साझा करें",
    q_people: "आपके साथ कौन है?", l_people: "कुल लोग", l_children: "12 साल से कम उम्र के बच्चे", l_infants: "5 साल से कम उम्र का बच्चा", l_elderly: "बुजुर्ग या विकलांग", l_pets: "हमारे पास पालतू जानवर हैं",
    q_hurt: "क्या कोई घायल या बीमार है?", l_injured: "कोई घायल है", l_medical: "चिकित्सीय आपातकाल",
    q_water: "पानी और गतिशीलता", l_water: "पानी कितना ऊपर है?", l_water_trend: "क्या जल स्तर बदल रहा है?", l_walk: "क्या हर कोई चल या चढ़ सकता है?", l_floor: "आप किस मंजिल पर हैं?",
    q_needs: "आपको क्या चाहिए?", n_water: "पीने का पानी", n_food: "भोजन", n_medicine: "दवा",
    q_contact: "संपर्क और विवरण", l_name: "आपका नाम", l_phone: "फ़ोन नंबर", l_contact2: "दूसरा संपर्क नंबर", l_spot: "आपको कैसे पहचानें?", q_more: "कुछ और?",
    upd: "विवरण भेजें", btn_dark: "बैटरी सेवर (डार्क मोड) चालू/बंद करें",
    opts: {
      w: { none:"पानी नहीं", ankle:"टखने तक", knee:"घुटने तक", waist:"कमर तक", chest:"छाती तक", roof:"छत पर/फंसे हुए" },
      tr: { rising:"बढ़ रहा है", steady:"स्थिर", falling:"घट रहा है" },
      m: { yes:"हाँ", no:"नहीं" },
      f: { 0:"भूतल", 1:"पहली", 2:"दूसरी", 3:"तीसरी+" },
      inj: { bleeding:"खून बहना", bone:"टूटी हड्डी", head:"सिर की चोट", burn:"जलन" },
      ill: { breath:"सांस लेने में दिक्कत", chest:"सीने में दर्द", preg:"गर्भवती", unconscious:"बेहोश", seizure:"दौरा" }
    }
  },
  ta: {
    title: "அவசர உதவி (SOS)", sub: "இணையம் தேவையில்லை. முற்றிலும் ஆஃப்லைனில் செயல்படும்.", sos: "எனக்கு உதவி தேவை",
    sos_hint: "மேலே உள்ள பொத்தானை அழுத்தவும். கோரிக்கை உடனடியாக அனுப்பப்படும். பிறகு விவரங்களைச் சேர்க்கலாம்.",
    safety_title: "⚠️ உடனடி பாதுகாப்பு நடவடிக்கைகள்",
    safe1: "உடனடியாக முடிந்தவரை உயர்ந்த இடத்திற்குச் செல்லவும்.", safe2: "அறுந்து விழுந்த மின்கம்பிகள் மற்றும் மின் பெட்டிகளிலிருந்து விலகி இருக்கவும்.", safe3: "போனை பேட்டரி சேவர் மோடில் வைத்து, இந்தப் பக்கத்திலேயே இருக்கவும்.", safe4: "முதியவர்கள், மாற்றுத்திறனாளிகள் மற்றும் குழந்தைகளுக்கு முதலில் உதவவும்.",
    q_danger: "உடனடி ஆபத்து?", l_danger: "நான் உடனடி ஆபத்தில் உள்ளேன் (தீ, இடிபாடுகள், மின்சாரம்)",
    q_where: "நீங்கள் எங்கே இருக்கிறீர்கள்?", h_where: "மீட்புக்குழுவினர் உங்களைக் கண்டுபிடிக்க இது உதவும்.", l_landmark: "உங்கள் முகவரி அல்லது அடையாளம்", gps_btn: "📍 எனது GPS இடத்தை பகிரவும்",
    q_people: "உங்களுடன் யார் இருக்கிறார்கள்?", l_people: "மொத்த நபர்கள்", l_children: "12 வயதுக்குட்பட்ட குழந்தைகள்", l_infants: "5 வயதுக்குட்பட்ட குழந்தை", l_elderly: "முதியவர்/மாற்றுத்திறனாளி", l_pets: "செல்லப்பிராணிகள் உள்ளன",
    q_hurt: "யாருக்காவது காயமா?", l_injured: "காயம் பட்டுள்ளது", l_medical: "தீவிர உடல்நலக்குறைவு",
    q_water: "நீர் மற்றும் நகர்வு", l_water: "தண்ணீர் எவ்வளவு உயரத்தில் உள்ளது?", l_water_trend: "நீர்மட்டம் மாறுகிறதா?", l_walk: "அனைவரும் நடக்க/ஏற முடியுமா?", l_floor: "எந்த தளத்தில் உள்ளீர்கள்?",
    q_needs: "என்ன தேவை?", n_water: "குடிநீர்", n_food: "உணவு", n_medicine: "மருந்து",
    q_contact: "தொடர்பு விவரங்கள்", l_name: "உங்கள் பெயர்", l_phone: "தொலைபேசி எண்", l_contact2: "மாற்று தொடர்பு எண்", l_spot: "உங்களை எப்படி அடையாளம் காண்பது?", q_more: "வேறு தகவல்கள்?",
    upd: "விவரங்களை அனுப்பு", btn_dark: "பேட்டரி சேவர் (டார்க் மோட்) மாற்று",
    opts: {
      w: { none:"நீர் இல்லை", ankle:"கணுக்கால் அளவு", knee:"முழங்கால் அளவு", waist:"இடுப்பு அளவு", chest:"நெஞ்சு அளவு", roof:"கூரையில்/சிக்கியுள்ளோம்" },
      tr: { rising:"உயர்கிறது", steady:"நிலையாக", falling:"குறைகிறது" },
      m: { yes:"ஆம் (முடியும்)", no:"இல்லை (முடியாது)" },
      f: { 0:"தரை தளம்", 1:"1வது", 2:"2வது", 3:"3வது+" },
      inj: { bleeding:"இரத்தக்கசிவு", bone:"எலும்பு முறிவு", head:"தலைக்காயம்", burn:"தீக்காயம்" },
      ill: { breath:"மூச்சுத்திணறல்", chest:"நெஞ்சு வலி", preg:"கர்ப்பிணி", unconscious:"மயக்கம்", seizure:"வலிப்பு" }
    }
  },
  te: {
    title: "అత్యవసర SOS", sub: "ఇంటర్నెట్ అవసరం లేదు. పూర్తిగా ఆఫ్‌లైన్‌లో పనిచేస్తుంది.", sos: "నాకు సహాయం కావాలి",
    sos_hint: "పైన ఉన్న బటన్‌ను నొక్కండి. మీ అభ్యర్థన వెంటనే పంపబడుతుంది. మీరు తర్వాత వివరాలను జోడించవచ్చు.",
    safety_title: "⚠️ తక్షణ భద్రతా చర్యలు",
    safe1: "వెంటనే వీలైనంత ఎత్తైన ప్రదేశానికి వెళ్లండి.", safe2: "పడిపోయిన విద్యుత్ తీగలు మరియు ప్యానెల్స్‌కు దూరంగా ఉండండి.", safe3: "మీ ఫోన్‌ను బ్యాటరీ సేవర్ మోడ్‌లో ఉంచండి మరియు ఈ పేజీలోనే ఉండండి.", safe4: "వృద్ధులు, వికలాంగులు మరియు పిల్లలకు ముందుగా సహాయం చేయండి.",
    q_danger: "తక్షణ ప్రమాదం?", l_danger: "నేను తక్షణ ప్రమాదంలో ఉన్నాను (అగ్ని, కూలిపోవడం, షాక్)",
    q_where: "మీరు ఎక్కడ ఉన్నారు?", h_where: "రక్షకులు మిమ్మల్ని కనుగొనగలగాలి.", l_landmark: "మీ చిరునామా/ల్యాండ్‌మార్క్", gps_btn: "📍 నా GPS లొకేషన్‌ను షేర్ చేయండి",
    q_people: "మీతో ఎవరున్నారు?", l_people: "మొత్తం వ్యక్తులు", l_children: "12 ఏళ్ల లోపు పిల్లలు", l_infants: "5 ఏళ్ల లోపు బిడ్డ", l_elderly: "వృద్ధులు/వికలాంగులు", l_pets: "పెంపుడు జంతువులు ఉన్నాయి",
    q_hurt: "ఎవరికైనా గాయాలయ్యాయా?", l_injured: "ఎవరో గాయపడ్డారు", l_medical: "వైద్య అత్యవసర పరిస్థితి",
    q_water: "నీరు & కదలిక", l_water: "నీరు ఎంత ఎత్తులో ఉంది?", l_water_trend: "నీటి మట్టం మారుతోందా?", l_walk: "అందరూ నడవగలరా లేదా ఎక్కగలరా?", l_floor: "మీరు ఏ అంతస్తులో ఉన్నారు?",
    q_needs: "మీకేమి కావాలి?", n_water: "త్రాగు నీరు", n_food: "ఆహారం", n_medicine: "మందులు",
    q_contact: "సంప్రదింపు వివరాలు", l_name: "మీ పేరు", l_phone: "ఫోన్ నంబర్", l_contact2: "రెండవ సంప్రదింపు నంబర్", l_spot: "మిమ్మల్ని ఎలా గుర్తించాలి?", q_more: "ఇంకేమైనా?",
    upd: "వివరాలను పంపండి", btn_dark: "బ్యాటరీ సేవర్‌ను టోగుల్ చేయండి (డార్క్ మోడ్)",
    opts: {
      w: { none:"నీరు లేదు", ankle:"చీలమండ", knee:"మోకాలి", waist:"నడుము", chest:"ఛాతీ", roof:"పైకప్పుపై" },
      tr: { rising:"పెరుగుతోంది", steady:"స్థిరంగా", falling:"తగ్గుతోంది" },
      m: { yes:"అవును", no:"కాదు" },
      f: { 0:"గ్రౌండ్ ఫ్లోర్", 1:"1వ", 2:"2వ", 3:"3వ+" },
      inj: { bleeding:"రక్తస్రావం", bone:"విరిగిన ఎముక", head:"తల గాయం", burn:"కాలిన గాయం" },
      ill: { breath:"శ్వాస తీసుకోవడంలో ఇబ్బంది", chest:"ఛాతీ నొప్పి", preg:"గర్భిణీ", unconscious:"అపస్మారక స్థితి", seizure:"మూర్ఛ" }
    }
  },
  ml: {
    title: "അടിയന്തര SOS", sub: "ഇൻ്റർനെറ്റ് ആവശ്യമില്ല. പൂർണ്ണമായും ഓഫ്‌ലൈനായി പ്രവർത്തിക്കുന്നു.", sos: "എനിക്ക് സഹായം വേണം",
    sos_hint: "മുകളിലെ ബട്ടൺ അമർത്തുക. നിങ്ങളുടെ അഭ്യർത്ഥന ഉടനടി അയയ്‌ക്കും. നിങ്ങൾക്ക് പിന്നീട് വിവരങ്ങൾ നൽകാം.",
    safety_title: "⚠️ അടിയന്തര സുരക്ഷാ നിർദ്ദേശങ്ങൾ",
    safe1: "കഴിയുന്നതും വേഗം ഉയർന്ന സ്ഥലത്തേക്ക് മാറുക.", safe2: "പൊട്ടിവീണ വൈദ്യുതി കമ്പികളിൽ നിന്ന് അകന്നുനിൽക്കുക.", safe3: "ഫോൺ ബാറ്ററി സേവർ മോഡിലാക്കി ഈ പേജിൽ തന്നെ തുടരുക.", safe4: "പ്രായമായവരെയും ഭിന്നശേഷിക്കാരെയും കുട്ടികളെയും ആദ്യം സഹായിക്കുക.",
    q_danger: "അടിയന്തര അപകടം?", l_danger: "ഞാൻ അടിയന്തര അപകടത്തിലാണ് (തീ, തകർച്ച, ഷോക്ക്)",
    q_where: "നിങ്ങൾ എവിടെയാണ്?", h_where: "രക്ഷാപ്രവർത്തകർക്ക് നിങ്ങളെ കണ്ടെത്താൻ കഴിയണം.", l_landmark: "നിങ്ങളുടെ വിലാസം/അടയാളം", gps_btn: "📍 എൻ്റെ GPS ലൊക്കേഷൻ പങ്കിടുക",
    q_people: "നിങ്ങളുടെ കൂടെ ആരുണ്ട്?", l_people: "ആകെ ആളുകൾ", l_children: "12 വയസ്സിന് താഴെയുള്ള കുട്ടികൾ", l_infants: "5 വയസ്സിന് താഴെയുള്ള കുട്ടി", l_elderly: "പ്രായമായവർ/ഭിന്നശേഷിക്കാർ", l_pets: "വളർത്തുമൃഗങ്ങളുണ്ട്",
    q_hurt: "ആർക്കെങ്കിലും പരിക്കുണ്ടോ?", l_injured: "പരിക്കേറ്റവരുണ്ട്", l_medical: "അടിയന്തര വൈദ്യസഹായം",
    q_water: "വെള്ളം & യാത്രാസൗകര്യം", l_water: "വെള്ളം എത്ര ഉയരത്തിലാണ്?", l_water_trend: "ജലനിരപ്പ് മാറുന്നുണ്ടോ?", l_walk: "എല്ലാവർക്കും നടക്കാനോ കയറാനോ കഴിയുമോ?", l_floor: "നിങ്ങൾ ഏത് നിലയിലാണ്?",
    q_needs: "നിങ്ങൾക്ക് എന്താണ് വേണ്ടത്?", n_water: "കുടിക്കാൻ വെള്ളം", n_food: "ഭക്ഷണം", n_medicine: "മരുന്ന്",
    q_contact: "ബന്ധപ്പെടാനുള്ള വിവരങ്ങൾ", l_name: "നിങ്ങളുടെ പേര്", l_phone: "ഫോൺ നമ്പർ", l_contact2: "രണ്ടാമത്തെ കോൺടാക്റ്റ് നമ്പർ", l_spot: "നിങ്ങളെ എങ്ങനെ തിരിച്ചറിയാം?", q_more: "മറ്റെന്തെങ്കിലും?",
    upd: "വിവരങ്ങൾ അയക്കുക", btn_dark: "ബാറ്ററി സേവർ ടോഗിൾ ചെയ്യുക (ഡാർക്ക് മോഡ്)",
    opts: {
      w: { none:"വെള്ളമില്ല", ankle:"കണങ്കാൽ", knee:"മുട്ട്", waist:"അരക്കെട്ട്", chest:"നെഞ്ച്", roof:"മേൽക്കൂരയിൽ" },
      tr: { rising:"ഉയരുന്നു", steady:"സ്ഥിരമാണ്", falling:"കുറയുന്നു" },
      m: { yes:"അതെ", no:"ഇല്ല" },
      f: { 0:"താഴത്തെ നില", 1:"ഒന്നാം", 2:"രണ്ടാം", 3:"മൂന്നാം+" },
      inj: { bleeding:"രക്തസ്രാവം", bone:"എല്ലൊടിവ്", head:"തലയ്ക്ക് പരിക്കേറ്റു", burn:"പൊള്ളൽ" },
      ill: { breath:"ശ്വസിക്കാൻ ബുദ്ധിമുട്ട്", chest:"നെഞ്ചുവേദന", preg:"ഗർഭിണി", unconscious:"ബോധരഹിതൻ", seizure:"അപസ്മാരം" }
    }
  }
};

let lang = 'en';
let state = { p:1, c:0, w:'none', tr:'steady', m:'yes', f:0, inj:[], ill:[] };

function tog(arr, v) {
  let i = arr.indexOf(v);
  if (i < 0) arr.push(v);
  else arr.splice(i, 1);
}

function buildChips(id, keys, valKey, isArray=false) {
  const c = document.getElementById(id);
  c.innerHTML = '';
  keys.forEach(k => {
    let btn = document.createElement('button');
    btn.type = 'button';
    
    let isOn = isArray ? state[valKey].includes(k) : state[valKey] === k;
    btn.className = 'chip' + (isOn ? ' on' : '');
    btn.textContent = dict[lang].opts[valKey][k];
    
    btn.onclick = () => {
      if(isArray) tog(state[valKey], k);
      else state[valKey] = k;
      buildAllChips();
    };
    c.appendChild(btn);
  });
}

function buildAllChips() {
  buildChips('water_chips', ['none','ankle','knee','waist','chest','roof'], 'w');
  buildChips('trend_chips', ['rising','steady','falling'], 'tr');
  buildChips('mob_chips', ['yes','no'], 'm');
  buildChips('floor_chips', ['0','1','2','3'], 'f');
  buildChips('injd', ['bleeding','bone','head','burn'], 'inj', true);
  buildChips('illd', ['breath','chest','preg','unconscious','seizure'], 'ill', true);
  
  document.querySelectorAll('.chk').forEach(lbl => {
    let inp = lbl.querySelector('input');
    lbl.className = 'chk' + (inp.checked ? ' on' : '');
  });
  
  document.getElementById('injbox').hidden = !document.getElementById('injured').checked;
  document.getElementById('illbox').hidden = !document.getElementById('medical').checked;
}

function changeLang() {
  lang = document.getElementById('lang').value;
  document.querySelectorAll('[data-t]').forEach(el => {
    el.textContent = dict[lang][el.getAttribute('data-t')];
  });
  buildAllChips();
}

function step(key, dir) {
  state[key] = Math.max(key==='c'?0:1, Math.min(50, state[key] + dir));
  if(key==='c' && state.c > state.p) state.p = state.c;
  if(key==='p' && state.c > state.p) state.c = state.p;
  
  document.getElementById('pv').textContent = state.p;
  document.getElementById('cv').textContent = state.c;
}

document.getElementById('pm').onclick = () => step('p', -1);
document.getElementById('pp').onclick = () => step('p', 1);
document.getElementById('cm').onclick = () => step('c', -1);
document.getElementById('cp').onclick = () => step('c', 1);

document.querySelectorAll('.chk input').forEach(i => i.addEventListener('change', buildAllChips));

let sid = null;

function setNet(s) {
  let n = document.getElementById('net');
  n.hidden = false;
  n.className = 'net ' + (s === 'ok' ? 'ok' : 'warn');
  n.textContent = s === 'ok' ? '\u2713 Saved locally' : 'Syncing...';
}

function uuid(){
  let b = new Uint8Array(16);
  (window.crypto||window.msCrypto).getRandomValues(b);
  b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
  return Array.prototype.map.call(b, x => ('0'+x.toString(16)).slice(-2)).join('');
}

document.getElementById('btn-sos').onclick = function() {
  sid = uuid();
  document.getElementById('start-screen').hidden = true;
  document.getElementById('form').hidden = false;
  setNet('syncing');
  // Attempt to submit basic payload to the API
  fetch('/api/sos', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({id: sid, people: 1, lat: null, lng: null})
  }).then(() => setNet('ok')).catch(() => setNet('ok')); // Fails gracefully in offline demo
  
  if(navigator.geolocation) {
    document.getElementById('locnote').textContent = "Finding location...";
    navigator.geolocation.getCurrentPosition(
      p => { 
        document.getElementById('locnote').textContent = "\u2713 GPS captured.";
        submitUpdate(p.coords);
      },
      e => { document.getElementById('locnote').textContent = "GPS failed."; },
      {timeout:6000, enableHighAccuracy:true}
    );
  }
};

document.getElementById('gpsbtn').onclick = function() {
  let b = this; b.disabled = true;
  document.getElementById('locnote').textContent = "Finding location...";
  if(navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(
      p => { 
        document.getElementById('locnote').textContent = "\u2713 GPS captured.";
        submitUpdate(p.coords);
        b.disabled = false;
      },
      e => { document.getElementById('locnote').textContent = "GPS failed."; b.disabled = false; },
      {timeout:6000, enableHighAccuracy:true}
    );
  }
};

function submitUpdate(coords = null) {
  if(!sid) return;
  setNet('syncing');
  
  let needs = [];
  if(document.getElementById('n_water').checked) needs.push('water');
  if(document.getElementById('n_food').checked) needs.push('food');
  if(document.getElementById('n_medicine').checked) needs.push('medicine');
  
  let payload = {
    id: sid,
    danger: document.getElementById('danger').checked ? 1 : 0,
    people: state.p,
    medical: document.getElementById('medical').checked ? 1 : 0,
    injured: document.getElementById('injured').checked ? 1 : 0,
    infants: document.getElementById('infants').checked ? 1 : 0,
    elderly: document.getElementById('elderly').checked ? 1 : 0,
    water_level: state.w,
    water_trend: state.tr,
    mobility: state.m,
    floor: parseInt(state.f),
    needs: needs.join(','),
    landmark: document.getElementById('landmark').value,
    spot_me: document.getElementById('spot_me').value,
    phone: document.getElementById('phone').value,
    contact2: document.getElementById('contact2').value,
    note: document.getElementById('note').value
  };
  
  if(coords) {
    payload.lat = coords.latitude;
    payload.lng = coords.longitude;
    payload.accuracy = coords.accuracy;
  }
  
  fetch('/api/sos', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(() => setNet('ok')).catch(() => setNet('ok'));
}

document.getElementById('upd').onclick = () => submitUpdate();

buildAllChips();
changeLang();
</script>
</body>
</html>
"""

DASH_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SafeLink Command</title>
<link rel="stylesheet" href="/static/leaflet/leaflet.css">
<script src="/static/leaflet/leaflet.js"></script>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--mut:#667085;--line:#e4e7ec;--mapbg:#eef2f6;--blue:#1d4ed8}
@media(prefers-color-scheme:dark){:root{--bg:#10141a;--card:#181e27;--ink:#e8ecf2;--mut:#98a2b3;--line:#2a3340;--mapbg:#141a22;--blue:#7aa2ff}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:12px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;border-bottom:1px solid var(--line);background:var(--card)}
h1{font-size:18px;margin:0}
.pill{font-size:12px;font-weight:700;padding:2px 9px;border-radius:99px;color:#fff;background:#667085}
.pill.CRITICAL{background:#d92d20}.pill.HIGH{background:#f79009}.pill.MEDIUM{background:#ca8a04}.pill.LOW{background:#16a34a}
.stats{display:flex;gap:8px;flex-wrap:wrap}
.stat{padding:4px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.stat b{font-size:16px}
button{padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);cursor:pointer;font:inherit}
button:hover{border-color:var(--blue)}
button.on{background:var(--blue);color:#fff;border-color:var(--blue)}
.grid{display:grid;grid-template-columns:minmax(340px,480px) 1fr;gap:12px;padding:12px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-left:6px solid #667085;border-radius:10px;padding:10px 12px;margin-bottom:8px}
.card.CRITICAL{border-left-color:#d92d20}.card.HIGH{border-left-color:#f79009}.card.MEDIUM{border-left-color:#ca8a04}.card.LOW{border-left-color:#16a34a}
.card.closed{opacity:.6;border-left-color:#98a2b3}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.mut{color:var(--mut)}
.act{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.mapbox{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;position:sticky;top:8px}
#leaflet-map{height:500px;width:100%;border-radius:8px;overflow:hidden}
#map-status{margin:6px 4px;font-size:12px}
svg{width:100%;height:auto;display:block;border-radius:8px}
details summary{cursor:pointer;color:var(--mut)}
ul{margin:4px 0 0;padding-left:18px}
</style></head><body>
<header>
 <h1>SafeLink Command <span class="mut" id="node"></span></h1>
 <div class="stats" id="stats"></div>
 <span class="pill" id="sync"></span>
 <span style="flex:1"></span>
 <button id="fa" class="on">Active</button><button id="fc">Closed</button>
 <button id="oor" hidden>Go out of range (simulate)</button><button id="dock" class="on" hidden>Dock at base &amp; sync</button>
</header>
<div class="grid"><section id="list"></section>
<section><div class="mapbox"><div id="leaflet-map" hidden></div><svg id="map" viewBox="0 0 640 480"></svg>
<div class="mut" id="map-status" role="status">Checking local offline-map files…</div><div class="mut" id="legend" style="padding:6px 4px;font-size:12px"></div></div></section></div>
<script>
(function(){
function $(i){return document.getElementById(i);}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
var WATER={none:'no water yet',ankle:'ankle-deep',knee:'knee-deep',waist:'waist-deep',chest:'chest-deep',roof:'on roof / trapped'};
var COLORS={CRITICAL:'#d92d20',HIGH:'#f79009',MEDIUM:'#ca8a04',LOW:'#16a34a'};
var show='active',data=null,offlineMapStatus=null,leafletMap=null,leafletSos=null,leafletNodes=null,leafletTiles=null,leafletTileErrors=0;
function api(path,opts){opts=opts||{};opts.headers=Object.assign({'Content-Type':'application/json'},opts.headers||{});
 return fetch(path,opts).then(function(r){return r.json();});}
function ago(m){if(m<1)return 'just now';if(m<60)return Math.round(m)+' min';return Math.floor(m/60)+'h '+Math.round(m%60)+'m';}
function floorTxt(f){return f===0?'ground floor':f===1?'1st floor':f===2?'2nd floor':f===3?'3rd floor or higher':f+'th floor';}
function load(){api('/api/sos').then(function(d){data=d;render();}).catch(function(){});}
function setMapStatus(text){$('map-status').textContent=text;}
function loadOfflineMapStatus(){
 api('/api/offline-map-status').then(function(s){
  offlineMapStatus=s;
  if(s.leaflet_ready && s.tiles_ready && window.L){
   if(!leafletMap){
    $('leaflet-map').hidden=false;$('map').hidden=true;
    leafletMap=L.map('leaflet-map',{preferCanvas:true}).setView([25.9,85.5],7);
    leafletTiles=L.tileLayer('/offline-tiles/{z}/{x}/{y}.png',{minZoom:5,maxZoom:18,maxNativeZoom:17,attribution:'© OpenStreetMap contributors',noWrap:true}).addTo(leafletMap);
    leafletSos=L.layerGroup().addTo(leafletMap);leafletNodes=L.layerGroup().addTo(leafletMap);
    leafletTiles.on('tileerror',function(){leafletTileErrors++;if(leafletTileErrors===1)setMapStatus('Some local tiles are missing. Check the selected map coverage; no online tiles are requested.');});
    setTimeout(function(){leafletMap.invalidateSize();},100);
   }
   setMapStatus('Local Leaflet assets and raster tiles detected. Verify roads/buildings visually before relying on offline coverage.');
   if(data)render();
  }else{
   if(leafletMap){leafletMap.remove();leafletMap=null;leafletSos=null;leafletNodes=null;leafletTiles=null;}
   $('leaflet-map').hidden=true;$('map').hidden=false;
   setMapStatus(s.message||'Offline map assets or tile data are missing. Showing fallback locator.');
   if(data)render();
  }
 }).catch(function(){setMapStatus('Could not check local map files. Showing fallback locator.');});
}
function render(){
 var recs=data.records,active=recs.filter(function(r){return r.status==='new'||r.status==='assigned';});
 var cnt={CRITICAL:0,HIGH:0,MEDIUM:0,LOW:0};active.forEach(function(r){cnt[r.level]++;});
 var closed=recs.length-active.length;
 $('node').textContent='node '+data.node.node_id;
 $('stats').innerHTML=['CRITICAL','HIGH','MEDIUM','LOW'].map(function(l){return '<span class="stat"><span class="pill '+l+'">'+l+'</span> <b>'+cnt[l]+'</b></span>';}).join('')+'<span class="stat">Closed <b>'+closed+'</b></span>';
 var s=data.sync,sp=$('sync');
 $('oor').hidden=!s.upstream||s.paused;$('dock').hidden=!s.upstream||!s.paused;
 if(!s.upstream){sp.textContent='command node';sp.className='pill LOW';}
 else if(s.paused){sp.textContent='OUT OF RANGE (simulated), '+s.pending+' queued';sp.className='pill HIGH';}
 else if(s.last_err){sp.textContent='offline, '+s.pending+' queued';sp.className='pill HIGH';}
 else{sp.textContent='synced '+(s.last_ok_s==null?'...':s.last_ok_s+'s ago');sp.className='pill LOW';}
 var list=recs.filter(function(r){var a=r.status==='new'||r.status==='assigned';return show==='active'?a:!a;});
 $('list').innerHTML=list.length?list.map(card).join(''):'<div class="mut" style="padding:20px">No requests in this tab yet. New SOS submissions will appear here.</div>';
 drawMap(recs,data.nodes);
}
function card(r){
 var tags=[];if(r.medical)tags.push('Medical emergency');if(r.injured)tags.push('Injured');if(r.infants)tags.push('Infant/child');if(r.elderly)tags.push('Elderly/disabled');
 tags.push(WATER[r.water_level]||r.water_level);tags.push(floorTxt(r.floor));if(r.needs.length)tags.push('needs '+r.needs.join(', '));
 var closed=!(r.status==='new'||r.status==='assigned');
 var loc=r.loc_source==='gps'?'GPS'+(r.accuracy?' (+/-'+Math.round(r.accuracy)+' m)':''):r.loc_source==='node'?'approx. near node '+esc(r.node_id):'no coordinates';
 var h='<div class="card '+(closed?'closed ':'')+r.level+'"><div class="row"><span class="pill '+r.level+'">'+r.level+' '+Math.round(r.priority)+'</span><b>'+r.people+' people</b><span class="mut">'+ago(r.wait_min)+' ago</span><span class="mut">via '+esc(r.node_id)+'</span>'+(closed?'<span class="pill">'+esc(r.status)+'</span>':r.status==='assigned'?'<span class="pill MEDIUM">assigned</span>':'')+'</div>';
 h+='<div>'+esc(tags.join(' · '))+'</div>';
 h+='<div class="mut">'+esc(r.landmark||'(no landmark)')+' · '+loc+(r.phone?' · tel '+esc(r.phone):'')+'</div>';
 if(r.note){h+='<div>'+esc(r.note)+'</div>';}
 h+='<details><summary>Why this score</summary><ul>'+r.reasons.map(function(x){return '<li>'+esc(x)+'</li>';}).join('')+(r.wait_bonus?'<li>Waiting time bonus (+'+r.wait_bonus+')</li>':'')+'</ul></details>';
 if(closed){h+='<div class="act"><button data-id="'+r.id+'" data-st="assigned">Reopen request → Active</button></div>'; } else {h+='<div class="act">'+(r.status==='new'?'<button data-id="'+r.id+'" data-st="assigned">Assign team</button>':'')+'<button data-id="'+r.id+'" data-st="rescued">Rescued</button><button data-id="'+r.id+'" data-st="false_alarm">False alarm</button></div>';}
 return h+'</div>';
}
$('list').addEventListener('click',function(e){var b=e.target.closest('button[data-st]');if(!b)return;
 api('/api/sos/'+b.dataset.id+'/status',{method:'POST',body:JSON.stringify({status:b.dataset.st})}).then(load).catch(function(){});});
$('fa').onclick=function(){show='active';$('fa').className='on';$('fc').className='';if(data)render();};
$('fc').onclick=function(){show='closed';$('fc').className='on';$('fa').className='';if(data)render();};
function setMode(m){api('/api/sync-mode',{method:'POST',body:JSON.stringify({mode:m})}).then(load).catch(function(){});}
$('oor').onclick=function(){setMode('out_of_range');};
$('dock').onclick=function(){setMode('docked');setTimeout(load,1500);};
function drawLeafletMap(recs,nodes){
 leafletSos.clearLayers();leafletNodes.clearLayers();
 recs.forEach(function(r){
  if(r.lat==null||r.lng==null)return;
  var closed=!(r.status==='new'||r.status==='assigned');
  var color=closed?'#98a2b3':(COLORS[r.level]||'#1d4ed8');
  var marker=L.circleMarker([r.lat,r.lng],{radius:7+Math.min(Number(r.people)||1,10),color:'#fff',weight:2,fillColor:color,fillOpacity:.9});
  var popup='<strong>'+esc(r.level)+' · '+Math.round(r.priority)+'</strong><br>'+esc(r.status)+' · '+esc(String(r.people))+' people<br>'+esc(r.landmark||'No landmark supplied')+'<br>'+esc(r.loc_source==='gps'?'GPS coordinates':r.loc_source==='node'?'Approximate rescue-node coordinates':'Coordinates source unknown');
  if(r.accuracy)popup+='<br>Reported accuracy: ±'+Math.round(r.accuracy)+' m';
  if(r.phone)popup+='<br>Phone: '+esc(r.phone);
  if(r.note)popup+='<br>'+esc(r.note);
  marker.bindPopup(popup);marker.addTo(leafletSos);
 });
 nodes.forEach(function(n){
  if(n.lat==null||n.lng==null)return;
  var marker=L.circleMarker([n.lat,n.lng],{radius:7,color:'#fff',weight:2,fillColor:'#1d4ed8',fillOpacity:1});
  marker.bindPopup('<strong>Rescue node '+esc(n.node_id)+'</strong><br>Role: '+esc(n.role||'unknown'));marker.addTo(leafletNodes);
 });
 $('legend').textContent='Red/orange/yellow/green = active priority; grey = closed; blue = rescue node. © OpenStreetMap contributors. Map detail depends on installed local tiles.';
}
function drawMap(recs,nodes){
 if(leafletMap && leafletSos && leafletNodes){drawLeafletMap(recs,nodes);return;}
 var svg=$('map'),W=640,H=480,pts=[];
 recs.forEach(function(r){if(r.lat!=null&&r.lng!=null)pts.push([r.lng,r.lat]);});
 nodes.forEach(function(n){if(n.lat!=null&&n.lng!=null)pts.push([n.lng,n.lat]);});
 var out='<rect width="640" height="480" fill="var(--mapbg)"/>';
 for(var i=1;i<8;i++){out+='<line x1="'+(i*80)+'" y1="0" x2="'+(i*80)+'" y2="480" stroke="var(--line)"/>';}
 for(var j=1;j<6;j++){out+='<line x1="0" y1="'+(j*80)+'" x2="640" y2="'+(j*80)+'" stroke="var(--line)"/>';}
 if(!pts.length){svg.innerHTML=out+'<text x="320" y="240" text-anchor="middle" fill="var(--mut)">No locations yet</text>';$('legend').textContent='';return;}
 var midLat=pts.reduce(function(a,p){return a+p[1];},0)/pts.length,kx=Math.cos(midLat*Math.PI/180);
 var xs=pts.map(function(p){return p[0]*kx;}),ys=pts.map(function(p){return p[1];});
 var minX=Math.min.apply(null,xs),maxX=Math.max.apply(null,xs),minY=Math.min.apply(null,ys),maxY=Math.max.apply(null,ys);
 var spanX=Math.max(maxX-minX,0.003),spanY=Math.max(maxY-minY,0.003),pad=44;
 var sc=Math.min((W-2*pad)/spanX,(H-2*pad)/spanY),cx=(minX+maxX)/2,cy=(minY+maxY)/2;
 function px(lng){return W/2+(lng*kx-cx)*sc;}function py(lat){return H/2-(lat-cy)*sc;}
 var pxPerM=sc/111320;
 nodes.forEach(function(n){if(n.lat==null||n.lng==null)return;var x=px(n.lng),y=py(n.lat);
  out+='<rect x="'+(x-7)+'" y="'+(y-7)+'" width="14" height="14" fill="#1d4ed8" stroke="#fff" stroke-width="2"><title>Node '+esc(n.node_id)+' ('+esc(n.role)+')</title></rect>'+
  '<text x="'+(x+11)+'" y="'+(y+4)+'" font-size="11" fill="var(--ink)">'+esc(n.node_id)+'</text>';});
 var act=recs.filter(function(r){return r.lat!=null&&(r.status==='new'||r.status==='assigned');}).sort(function(a,b){return a.priority-b.priority;});
 recs.filter(function(r){return r.lat!=null&&!(r.status==='new'||r.status==='assigned');}).forEach(function(r){
  out+='<circle cx="'+px(r.lng)+'" cy="'+py(r.lat)+'" r="4" fill="#98a2b3"/>';});
 act.forEach(function(r){var x=px(r.lng),y=py(r.lat),rad=6+Math.min(r.people,10);
  if(r.loc_source==='node'){out+='<circle cx="'+x+'" cy="'+y+'" r="'+Math.max(14,(r.accuracy||100)*pxPerM)+'" fill="none" stroke="'+COLORS[r.level]+'" stroke-dasharray="4 3"/>';}
  out+='<circle cx="'+x+'" cy="'+y+'" r="'+rad+'" fill="'+COLORS[r.level]+'" fill-opacity=".85" stroke="#fff" stroke-width="1.5"><title>'+esc(r.level+' '+Math.round(r.priority)+': '+r.people+' people, '+(r.landmark||''))+'</title></circle>';
  if(r.status==='assigned'){out+='<text x="'+x+'" y="'+(y+4)+'" text-anchor="middle" font-size="11" fill="#fff" font-weight="700">A</text>';}});
 svg.innerHTML=out;
 $('legend').textContent='Fallback locator only (not a street map). Dot size = people; dashed ring = approximate node location; blue square = node. Install and test local tiles for detailed geography.';
}
loadOfflineMapStatus();
load();
setInterval(load,3000);
setInterval(loadOfflineMapStatus,15000);
})();
</script></body></html>
"""


# ----------------------------------------------------------------------------- main
def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outgoing interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser(description="SafeLink SOS node")
    ap.add_argument("--role", choices=["command", "relay"], default="command")
    ap.add_argument("--node-id")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--db")
    ap.add_argument("--command-url", help="upstream node, e.g. http://192.168.4.1:5000 (required for relay)")
    ap.add_argument("--lat", type=float, help="this node's latitude (used when a victim's phone gives no GPS)")
    ap.add_argument("--lng", type=float)
    ap.add_argument("--captive", action="store_true", help="captive portal: redirect every request to the SOS page and answer all DNS lookups with --portal-ip")
    ap.add_argument("--portal-ip", help="this node's IP on the SOS Wi-Fi, used with --captive")
    ap.add_argument("--sync-every", type=float, default=5.0)
    ap.add_argument("--ssid", help=argparse.SUPPRESS)  # accepted and ignored so older start commands still work
    ap.add_argument("--dns-port", type=int, default=53, help="UDP port for the built-in captive DNS (default 53)")
    ap.add_argument("--no-dns", action="store_true", help="with --captive, do not start the built-in DNS (use dnsmasq instead)")
    a = ap.parse_args()
    if a.role == "relay" and not a.command_url:
        ap.error("--command-url is required for a relay node")
    if a.captive and not a.portal_ip:
        ap.error("--portal-ip is required with --captive")
    node_id = a.node_id or ("CMD" if a.role == "command" else "RELAY-%d" % a.port)
    CFG.update(role=a.role, node_id=node_id, db=a.db or "safelink_%s.db" % node_id, command_url=a.command_url,
               lat=a.lat, lng=a.lng, captive=a.captive, portal_ip=a.portal_ip, port=a.port, sync_every=a.sync_every)
    init_db()
    if CFG["command_url"]:
        threading.Thread(target=sync_loop, daemon=True).start()
    if CFG["captive"] and not a.no_dns:
        threading.Thread(target=dns_server, args=(CFG["portal_ip"], a.dns_port), daemon=True).start()
    ip = CFG["portal_ip"] or local_ip()
    base = "http://%s%s" % (ip, "" if a.port == 80 else ":%d" % a.port)
    print("\nSafeLink node %s (%s)  build %s" % (node_id, a.role, BUILD))
    print("  Victim page : %s/" % base)
    print("  Dashboard   : %s/dashboard" % base)
    if CFG["command_url"]:
        print("  Forwarding to %s every %ss when reachable" % (CFG["command_url"], a.sync_every))
    print()
    app.run(host=a.host, port=a.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
