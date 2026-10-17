import asyncio
import unittest
from unittest import mock

from support import FakeHTTP, FakeResponse, PalmAsyncCase, PalmCase, httpx


class ShellyEM(PalmAsyncCase):
    async def read(self, response):
        http = FakeHTTP([response])
        shelly = self.palm.Shelly(http)
        ok = await shelly.read_em()
        return ok, shelly

    async def test_valid_readings(self):
        cases = [
            ({"emeters": [{"power": 3500.4, "is_valid": True}]}, 3500),
            ({"act_power": 120}, 120),
            ({"emeters": [{"power": -3}]}, 0),                          # small negative clamped
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                ok, shelly = await self.read(FakeResponse(json_data=payload))
                self.assertTrue(ok)
                self.assertEqual(shelly.ev_power.get(60), expected)

    async def test_invalid_readings_leave_value_unset(self):
        cases = [
            FakeResponse(json_data={"emeters": [{"power": 1, "is_valid": False}]}),
            FakeResponse(json_data={"emeters": [{"power": 30000}]}),    # impossible for a domestic EV
            FakeResponse(json_data={"emeters": []}),
            FakeResponse(json_data={"emeters": [{"power": "n/a"}]}),
            FakeResponse(),                                             # not JSON
            FakeResponse(500),
            httpx.HTTPError("unreachable"),
        ]
        for response in cases:
            with self.subTest(response=response):
                ok, shelly = await self.read(response)
                self.assertFalse(ok)
                self.assertIsNone(shelly.ev_power.get(60))

    async def test_unconfigured_meter_makes_no_request(self):
        self.cfg.Shelly.em0_url = None
        http = FakeHTTP()
        self.assertFalse(await self.palm.Shelly(http).read_em())
        self.assertEqual(http.calls, [])

    async def test_failure_keeps_last_value_until_it_goes_stale(self):
        shelly = self.palm.Shelly(FakeHTTP([httpx.HTTPError("x")]))
        shelly.ev_power.set(2000)
        await shelly.read_em()
        self.assertEqual(shelly.ev_power.get(60), 2000)
        shelly.ev_power.ts -= 1000
        self.assertIsNone(shelly.ev_power.get(self.palm.STALE_AFTER_S))


class ShellySwitch(PalmAsyncCase):
    async def test_set_switch_url(self):
        http = FakeHTTP()
        self.assertTrue(await self.palm.Shelly(http).set_switch("http://sw/", True))
        self.assertIn("/rpc/Switch.Set?id=0&on=true", http.calls[0][1])
        self.assertTrue(await self.palm.Shelly(http).set_switch("http://sw", False))
        self.assertIn("on=false", http.calls[1][1])

    async def test_set_switch_retries_then_succeeds(self):
        http = FakeHTTP([FakeResponse(500), httpx.HTTPError("x"), FakeResponse(200)])
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            self.assertTrue(await self.palm.Shelly(http).set_switch("http://sw", True))
        self.assertEqual(len(http.calls), 3)

    async def test_set_switch_gives_up_after_attempts(self):
        http = FakeHTTP(default=FakeResponse(500))
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()), self.assertLogs("PALM", "WARNING"):
            self.assertFalse(await self.palm.Shelly(http).set_switch("http://sw", True))
        self.assertEqual(len(http.calls), 3)

    async def test_test_mode_does_not_switch_anything(self):
        self.cfg.pg.test_mode = True
        http = FakeHTTP()
        self.assertTrue(await self.palm.Shelly(http).set_switch("http://sw", True))
        self.assertEqual(http.calls, [])

    async def test_read_switch(self):
        for payload, expected in (({"output": True}, "On"), ({"output": False}, "Off")):
            with self.subTest(payload=payload):
                http = FakeHTTP([FakeResponse(json_data=payload)])
                self.assertEqual(await self.palm.Shelly(http).read_switch("http://sw"), expected)
        for bad in (FakeResponse(500), FakeResponse(), httpx.HTTPError("x")):
            with self.subTest(bad=bad), self.assertLogs("PALM", "ERROR"):
                self.assertEqual(await self.palm.Shelly(FakeHTTP([bad])).read_switch("http://sw"), "Error")


class CarbonIntensity(PalmAsyncCase):
    GOOD = {"data": [{"data": [{"intensity": {"forecast": 123}}]}]}

    async def test_parses_forecast_and_builds_url(self):
        http = FakeHTTP([FakeResponse(json_data=self.GOOD)])
        env = self.palm.Env(http)
        await env.update_co2()
        self.assertEqual(env.co2.get(60), 123)
        self.assertTrue(http.calls[0][1].endswith("/regional/postcode/AB1"))

    async def test_malformed_payloads_do_not_raise_or_set_value(self):
        bad = [{"data": []}, {"data": [{"data": []}]}, {"data": [{"data": [{"intensity": {"forecast": None}}]}]},
               {"data": [{"data": [{"intensity": {"forecast": "high"}}]}]}, {}, "string"]
        for payload in bad:
            with self.subTest(payload=payload), self.assertLogs("PALM", "ERROR"):
                env = self.palm.Env(FakeHTTP([FakeResponse(json_data=payload)]))
                await env.update_co2()
                self.assertIsNone(env.co2.get(60))

    async def test_http_error_keeps_previous_value(self):
        env = self.palm.Env(FakeHTTP([httpx.HTTPError("x")]))
        env.co2.set(99)
        with self.assertLogs("PALM", "ERROR"):
            await env.update_co2()
        self.assertEqual(env.co2.get(60), 99)


class Weather(PalmAsyncCase):
    async def update(self, payload):
        env = self.palm.Env(FakeHTTP([FakeResponse(json_data=payload)]))
        await env.update_weather_curr()
        return env

    async def test_kelvin_conversion_and_symbol(self):
        env = await self.update({"current": {"temp": 288.15, "weather": [{"id": 800}]}})
        self.assertEqual(env.temp.get(60), 15.0)
        self.assertEqual(env.weather_symbol, "800")

    async def test_bad_data_is_ignored(self):
        for payload in ({"current": {}}, {}, {"current": {"temp": "warm"}}, {"current": {"temp": 400}}):
            with self.subTest(payload=payload), self.assertLogs("PALM", "ERROR"):
                env = await self.update(payload)
                self.assertIsNone(env.temp.get(60))

    async def test_missing_weather_list_is_tolerated(self):
        env = await self.update({"current": {"temp": 280.0}})
        self.assertEqual(env.weather_symbol, "0")
        self.assertIsNotNone(env.temp.get(60))


class DecisionValues(PalmCase):
    def test_disabled_feeds_fall_back_to_defaults(self):
        env = self.palm.Env(None)
        self.assertEqual(env.decision_co2(), self.palm.DEFAULT_CO2)
        self.assertEqual(env.decision_temp(), self.palm.DEFAULT_TEMP_C)

    def test_enabled_feeds_without_data_are_unknown(self):
        self.cfg.CarbonIntensity.enable = self.cfg.OpenWeatherMap.enable = True
        env = self.palm.Env(None)
        self.assertIsNone(env.decision_co2())
        self.assertIsNone(env.decision_temp())

    def test_enabled_feed_values_expire(self):
        self.cfg.CarbonIntensity.enable = True
        env = self.palm.Env(None)
        env.co2.set(150)
        self.assertEqual(env.decision_co2(), 150)
        env.co2.ts -= self.palm.ENV_STALE_AFTER_S + 1
        self.assertIsNone(env.decision_co2())


if __name__ == "__main__":
    unittest.main()
