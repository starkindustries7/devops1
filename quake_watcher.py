"""
Earthquake Feed First-Appearance Watcher
=========================================
Polls USGS GeoJSON feed every 15 seconds.
Records EXACTLY when each earthquake event
first appeared in the feed (not origin time).

Storage : quake_watch.db  (SQLite)
Log      : quake_watch.log
"""

import sqlite3
import requests
import time
import logging
import json
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
FEED_URL      = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
POLL_INTERVAL = 15          # seconds — balances latency vs. rate-limit risk
DB_PATH       = "quake_watch.db"
LOG_PATH      = "quake_watch.log"
REQUEST_TIMEOUT = 10        # seconds

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()          # also print to terminal
    ]
)
log = logging.getLogger(__name__)

# ── Database ──────────────────────────────────────────────────────────────────
def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS earthquakes (
            id               TEXT PRIMARY KEY,   -- USGS event id  e.g. us7000abc1
            first_seen_utc   TEXT NOT NULL,       -- when WE first saw it in the feed
            first_seen_ts    REAL NOT NULL,       -- same, as unix timestamp (for fast sorting)
            origin_time_utc  TEXT,                -- earthquake origin time from feed
            origin_time_ts   REAL,                -- origin time as unix timestamp
            lag_seconds      REAL,                -- first_seen − origin_time  (feed publish lag)
            magnitude        REAL,
            mag_type         TEXT,
            place            TEXT,
            latitude         REAL,
            longitude        REAL,
            depth_km         REAL,
            status           TEXT,                -- reviewed / automatic
            raw_properties   TEXT                 -- full JSON blob for future use
        )
    """)
    # Table to track feed-level metadata per poll
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poll_log (
            poll_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            polled_utc       TEXT NOT NULL,
            polled_ts        REAL NOT NULL,
            http_status      INTEGER,
            events_in_feed   INTEGER,
            new_events_found INTEGER,
            feed_generated   TEXT        -- feed's own "generated" timestamp if present
        )
    """)
    conn.commit()

def get_known_ids(conn):
    cur = conn.execute("SELECT id FROM earthquakes")
    return {row[0] for row in cur.fetchall()}

def insert_event(conn, event_id, props, first_seen_dt, first_seen_ts):
    origin_ts  = props.get("time")          # milliseconds since epoch
    origin_ts_s = origin_ts / 1000.0 if origin_ts else None
    origin_dt  = (datetime.fromtimestamp(origin_ts_s, tz=timezone.utc).isoformat()
                  if origin_ts_s else None)

    lag = round(first_seen_ts - origin_ts_s, 2) if origin_ts_s else None

    coords = None
    try:
        coords = props.get("_coords")       # injected below
    except Exception:
        pass

    conn.execute("""
        INSERT OR IGNORE INTO earthquakes
        (id, first_seen_utc, first_seen_ts, origin_time_utc, origin_time_ts,
         lag_seconds, magnitude, mag_type, place,
         latitude, longitude, depth_km, status, raw_properties)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        event_id,
        first_seen_dt.isoformat(),
        first_seen_ts,
        origin_dt,
        origin_ts_s,
        lag,
        props.get("mag"),
        props.get("magType"),
        props.get("place"),
        coords[1] if coords else None,      # GeoJSON: [lon, lat, depth]
        coords[0] if coords else None,
        coords[2] if coords else None,
        props.get("status"),
        json.dumps(props)
    ))
    conn.commit()

def log_poll(conn, polled_dt, polled_ts, http_status,
             total, new_count, feed_generated):
    conn.execute("""
        INSERT INTO poll_log
        (polled_utc, polled_ts, http_status, events_in_feed,
         new_events_found, feed_generated)
        VALUES (?,?,?,?,?,?)
    """, (polled_dt.isoformat(), polled_ts, http_status,
          total, new_count, feed_generated))
    conn.commit()

# ── Feed fetch ────────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "quake-watcher/1.0 (personal research)"})

def fetch_feed():
    """Returns (geojson_dict, http_status_code)"""
    resp = SESSION.get(FEED_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json(), resp.status_code

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_db(conn)

    log.info("=" * 60)
    log.info("Earthquake Feed Watcher started")
    log.info(f"Feed     : {FEED_URL}")
    log.info(f"Interval : {POLL_INTERVAL}s")
    log.info(f"Database : {DB_PATH}")
    log.info("=" * 60)

    while True:
        poll_start    = time.time()
        poll_dt       = datetime.now(tz=timezone.utc)
        http_status   = None
        total_events  = 0
        new_events    = 0
        feed_gen_str  = None

        try:
            data, http_status = fetch_feed()

            # Feed metadata
            meta         = data.get("metadata", {})
            feed_gen_ms  = meta.get("generated")
            feed_gen_str = (datetime.fromtimestamp(feed_gen_ms / 1000, tz=timezone.utc).isoformat()
                            if feed_gen_ms else None)

            features     = data.get("features", [])
            total_events = len(features)
            known_ids    = get_known_ids(conn)

            for feat in features:
                event_id = feat.get("id")
                if not event_id or event_id in known_ids:
                    continue

                # ── NEW EVENT — stamp it immediately ──────────────────────
                now_ts = time.time()
                now_dt = datetime.now(tz=timezone.utc)

                props = feat.get("properties", {})
                coords = feat.get("geometry", {}).get("coordinates")  # [lon,lat,depth]
                props["_coords"] = coords   # piggyback for insert_event

                insert_event(conn, event_id, props, now_dt, now_ts)
                known_ids.add(event_id)
                new_events += 1

                mag   = props.get("mag", "?")
                place = props.get("place", "unknown")
                origin_ms = props.get("time")
                origin_str = (datetime.fromtimestamp(origin_ms / 1000, tz=timezone.utc)
                              .strftime("%H:%M:%S UTC") if origin_ms else "unknown")
                lag_s = round(now_ts - origin_ms / 1000, 1) if origin_ms else "?"

                log.info(
                    f"🆕 NEW  id={event_id}  M{mag}  {place}\n"
                    f"        origin={origin_str}  "
                    f"first_seen={now_dt.strftime('%H:%M:%S UTC')}  "
                    f"lag={lag_s}s"
                )

            log_poll(conn, poll_dt, poll_start, http_status,
                     total_events, new_events, feed_gen_str)

            if new_events == 0:
                log.info(f"Poll OK — {total_events} events in feed, 0 new")

        except requests.exceptions.RequestException as e:
            log.warning(f"Network error: {e}")
            log_poll(conn, poll_dt, poll_start, http_status,
                     0, 0, None)

        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)

        # ── Sleep for remainder of interval ───────────────────────────────
        elapsed = time.time() - poll_start
        sleep_for = max(0, POLL_INTERVAL - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Watcher stopped by user.")
