"""Tests for monitor.py's alert logic. Run: python3 -m unittest -v test_monitor"""

import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import monitor as m

FIFTH, SOHO, WBURG = "R095", "R032", "R594"
NS512, SW512 = "MK1H4LL/A", "MK1G4LL/A"
T0 = datetime(2026, 11, 17, 9, 0, tzinfo=m.TZ).timestamp()  # 9 AM Eastern


class Recorder:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, title, message, priority, **kw):
        if title == "iPhone Duo monitor is watching":
            return True
        self.sent.append({"title": title, "message": message, "priority": priority, **kw})
        return self.ok

    def titles(self):
        return [s["title"] for s in self.sent]


def observe(available, missing_stores=()):
    return m.parse_availability(m.extract_stores(m.fake_response(available, missing_stores)))[0]


class StockAlerts(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        self.mon = m.Monitor(m.new_state(), send=self.rec, stock_only=False)

    def test_single_flip_alerts_once_with_link_and_store(self):
        self.mon.on_success(observe(set()), T0)
        for i in range(1, 6):
            self.mon.on_success(observe({(FIFTH, NS512)}), T0 + 30 * i)
        self.assertEqual(len(self.rec.sent), 1)
        alert = self.rec.sent[0]
        self.assertIn("512GB Night Sky", alert["title"])
        self.assertIn("Fifth Avenue", alert["message"])
        self.assertEqual(alert["priority"], 5)
        self.assertEqual(alert["click"], m.buy_url(NS512))
        self.assertTrue(alert["click"].endswith("/iphone-duo/7.6-inch-display-512gb-night-sky-unlocked"))

    def test_already_available_on_first_ever_check_alerts(self):
        self.mon.on_success(observe({(FIFTH, NS512)}), T0)
        self.assertEqual(len(self.rec.sent), 1)

    def test_unwatched_store_never_alerts(self):
        self.mon.on_success(observe({(WBURG, NS512)}), T0)
        self.assertEqual(self.rec.sent, [])

    def test_new_stores_for_same_config_grouped_into_one_push(self):
        self.mon.on_success(observe({(FIFTH, NS512), (SOHO, NS512), (FIFTH, SW512)}), T0)
        self.assertEqual(len(self.rec.sent), 2)  # one per config
        ns = next(s for s in self.rec.sent if "512GB Night Sky" in s["title"])
        self.assertIn("Fifth Avenue, SoHo", ns["message"])

    def test_second_store_later_is_a_new_alert(self):
        self.mon.on_success(observe({(FIFTH, NS512)}), T0)
        self.mon.on_success(observe({(FIFTH, NS512), (SOHO, NS512)}), T0 + 30)
        self.assertEqual(len(self.rec.sent), 2)
        self.assertIn("SoHo", self.rec.sent[1]["message"])
        self.assertNotIn("Fifth", self.rec.sent[1]["message"])

    def test_gone_after_two_unavailable_with_duration(self):
        self.mon.on_success(observe({(FIFTH, NS512)}), T0)
        self.mon.on_success(observe({(FIFTH, NS512)}), T0 + 60)
        self.mon.on_success(observe(set()), T0 + 90)
        self.assertEqual(self.rec.titles(), ["iPhone Duo 512GB Night Sky in stock for pickup"])
        self.mon.on_success(observe(set()), T0 + 120)
        self.assertEqual(self.rec.sent[-1]["title"], "Sold out")
        self.assertIn("Fifth Avenue: 512GB Night Sky (lasted about 1 min)", self.rec.sent[-1]["message"])

    def test_failed_check_is_unknown_not_sold_out(self):
        self.mon.on_success(observe({(FIFTH, NS512)}), T0)
        for i in range(1, 10):
            self.mon.on_failure("HTTP 541", T0 + 30 * i)
        self.mon.on_success(observe({(FIFTH, NS512)}), T0 + 300)
        titles = self.rec.titles()
        self.assertNotIn("Sold out", titles)
        self.assertEqual(titles.count("iPhone Duo 512GB Night Sky in stock for pickup"), 1)

    def test_missing_store_is_unknown_not_sold_out(self):
        self.mon.on_success(observe({(FIFTH, NS512)}), T0)
        for i in range(1, 5):
            self.mon.on_success(observe(set(), missing_stores={FIFTH}), T0 + 30 * i)
        self.assertNotIn("Sold out", self.rec.titles())
        self.assertIn(f"{FIFTH}|{NS512}", self.mon.state["pairs"])

    def test_ntfy_down_retries_alert_next_check(self):
        self.rec.ok = False
        self.mon.on_success(observe({(FIFTH, NS512)}), T0)
        self.rec.ok = True
        self.mon.on_success(observe({(FIFTH, NS512)}), T0 + 30)
        self.mon.on_success(observe({(FIFTH, NS512)}), T0 + 60)
        self.assertEqual(len(self.rec.sent), 2)  # failed attempt + successful retry, then quiet

    def test_never_alerted_pair_gets_no_gone_message(self):
        self.rec.ok = False
        self.mon.on_success(observe({(FIFTH, NS512)}), T0)
        self.rec.ok = True
        self.rec.sent.clear()
        self.mon.state["pairs"][f"{FIFTH}|{NS512}"]["alerted"] = False
        self.mon.on_success(observe(set()), T0 + 30)
        self.mon.on_success(observe(set()), T0 + 60)
        self.assertEqual(self.rec.sent, [])

    def test_restart_with_saved_state_does_not_realert(self):
        self.mon.on_success(observe({(FIFTH, NS512)}), T0)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            m.save_state(path, self.mon.state)
            restarted = m.Monitor(m.load_state(path), send=self.rec, stock_only=False)
        restarted.on_success(observe({(FIFTH, NS512)}), T0 + 60)
        self.assertEqual(len(self.rec.sent), 1)

    def test_stale_state_is_forgotten_silently(self):
        self.mon.on_success(observe({(FIFTH, NS512)}), T0)
        self.mon.on_success(observe({(FIFTH, NS512)}), T0 + 10 * 3600)  # next morning
        self.assertEqual(len(self.rec.sent), 2)  # re-announced; no bogus "Sold out"
        self.assertNotIn("Sold out", self.rec.titles())

    def test_preferred_color_alert_goes_first(self):
        self.mon.on_success(observe({(SOHO, SW512), (FIFTH, NS512)}), T0)
        self.assertEqual([s["title"] for s in self.rec.sent],
                         ["iPhone Duo 512GB Night Sky in stock for pickup",
                          "iPhone Duo 512GB Star White in stock for pickup"])

    def test_only_512gb_watched(self):
        self.assertEqual(set(m.PARTS), {"MK1G4LL/A", "MK1H4LL/A"})
        self.assertNotIn("MK1L4LL/A", m.fake_response(set())["body"]["stores"][0]["partsAvailability"])


class FailureAlerts(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        self.mon = m.Monitor(m.new_state(), send=self.rec, stock_only=False)

    def test_alert_after_five_failures_once(self):
        for i in range(4):
            self.mon.on_failure("HTTP 541", T0 + 30 * i)
        self.assertEqual(self.rec.sent, [])
        for i in range(4, 20):
            self.mon.on_failure("HTTP 541", T0 + 30 * i)
        self.assertEqual(self.rec.titles(), ["iPhone Duo monitor is failing"])
        self.assertIn("HTTP 541", self.rec.sent[0]["message"])

    def test_at_most_once_per_day(self):
        for i in range(5):
            self.mon.on_failure("x", T0 + i)
        self.mon.on_success(observe(set()), T0 + 100)
        self.assertEqual(self.rec.titles()[-1], "iPhone Duo monitor recovered")
        for i in range(10):
            self.mon.on_failure("x", T0 + 200 + i)
        self.assertEqual(self.rec.titles().count("iPhone Duo monitor is failing"), 1)
        for i in range(5):
            self.mon.on_failure("x", T0 + 86400 + i)  # next day
        self.assertEqual(self.rec.titles().count("iPhone Duo monitor is failing"), 2)

    def test_success_resets_streak(self):
        for i in range(4):
            self.mon.on_failure("x", T0 + i)
        self.mon.on_success(observe(set()), T0 + 10)
        for i in range(4):
            self.mon.on_failure("x", T0 + 20 + i)
        self.assertEqual(self.rec.sent, [])

    def test_failing_alert_retried_if_ntfy_down(self):
        self.rec.ok = False
        for i in range(5):
            self.mon.on_failure("x", T0 + i)
        self.rec.ok = True
        self.mon.on_failure("x", T0 + 10)
        self.assertEqual(self.rec.titles().count("iPhone Duo monitor is failing"), 2)
        self.mon.on_failure("x", T0 + 20)
        self.assertEqual(self.rec.titles().count("iPhone Duo monitor is failing"), 2)


class StockAlertsOnly(unittest.TestCase):
    """Default mode: the phone only ever gets 'in stock' pushes."""

    def test_default_is_stock_only(self):
        self.assertTrue(m.Monitor(m.new_state()).stock_only)

    def test_only_stock_alerts_reach_the_phone(self):
        sent = []
        mon = m.Monitor(m.new_state(), send=lambda t, msg, p, **kw: sent.append(t) or True,
                        stock_only=True)
        mon.on_success(observe(set()), T0)                    # would send the daily heartbeat
        mon.on_success(observe({(FIFTH, NS512)}), T0 + 30)    # in stock -> pushed
        mon.on_success(observe(set()), T0 + 60)
        mon.on_success(observe(set()), T0 + 90)               # sold out -> logged only
        for i in range(8):
            mon.on_failure("HTTP 541", T0 + 120 + i)          # failing -> logged only
        mon.on_success(observe(set()), T0 + 300)              # recovered -> logged only
        mon.on_success(observe({(SOHO, SW512)}), T0 + 330)    # in stock -> pushed
        self.assertEqual(sent, ["iPhone Duo 512GB Night Sky in stock for pickup",
                                "iPhone Duo 512GB Star White in stock for pickup"])
        self.assertEqual(mon.state["fail_alert_day"], m.local_day(T0))  # muted, not retried
        self.assertNotIn(f"{FIFTH}|{NS512}", mon.state["pairs"])       # sold out still tracked


class Parsing(unittest.TestCase):
    def test_error_body_is_failure(self):
        bad = {"head": {"status": "200"}, "body": {"errorMessage": "Product(s) Invalid or not buyable"}}
        with self.assertRaisesRegex(m.CheckFailed, "Invalid or not buyable"):
            m.extract_stores(bad)

    def test_too_few_watched_stores_is_failure(self):
        resp = m.fake_response(set(), missing_stores=set(list(m.STORES)[:5]))
        with self.assertRaisesRegex(m.CheckFailed, "watched stores"):
            m.parse_availability(m.extract_stores(resp))

    def test_unknown_display_value_is_unknown(self):
        resp = m.fake_response(set())
        resp["body"]["stores"][0]["partsAvailability"][NS512]["pickupDisplay"] = "weird"
        observed, _ = m.parse_availability(m.extract_stores(resp))
        self.assertNotIn((resp["body"]["stores"][0]["storeNumber"], NS512), observed)

    def _fake_urlopen(self, body, status=200):
        class Resp(io.BytesIO):
            headers = {}
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        if status != 200:
            def raiser(*a, **k):
                raise m.urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(body))
            return raiser
        return lambda *a, **k: Resp(body)

    def test_bot_challenge_html_is_failure(self):
        with mock.patch.object(m.urllib.request, "urlopen", self._fake_urlopen(b"<html>challenge</html>")):
            with self.assertRaisesRegex(m.CheckFailed, "non-JSON"):
                m.fetch_stores()

    def test_http_541_is_failure(self):
        with mock.patch.object(m.urllib.request, "urlopen", self._fake_urlopen(b"", status=541)):
            with self.assertRaisesRegex(m.CheckFailed, "HTTP 541"):
                m.fetch_stores()

    def test_real_shape_parses(self):
        body = json.dumps(m.fake_response({(FIFTH, NS512)})).encode()
        with mock.patch.object(m.urllib.request, "urlopen", self._fake_urlopen(body)):
            observed, seen = m.parse_availability(m.fetch_stores())
        self.assertEqual(len(seen), len(m.STORES))
        self.assertTrue(observed[(FIFTH, NS512)])
        self.assertEqual(sum(observed.values()), 1)


class Window(unittest.TestCase):
    def test_bounds(self):
        now = datetime(2026, 11, 17, 12, 0, tzinfo=m.TZ)
        start, end = m.window_bounds(now)
        self.assertEqual((start.hour, end.hour), (7, 22))


class TopicNeverLogged(unittest.TestCase):
    def test_log_redacts_topic(self):
        with mock.patch.object(m, "NTFY_TOPIC", "secret-topic-123"), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            m.log("posting to https://ntfy.sh/secret-topic-123 failed")
        self.assertNotIn("secret-topic-123", out.getvalue())


if __name__ == "__main__":
    unittest.main()
