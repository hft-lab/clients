import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading


class HitbtcClient:
    PUBLIC_WS_ENDPOINT = 'wss://api.hitbtc.com/api/3/ws/public'
    EXCHANGE_NAME = 'HITBTC'

    def __init__(self):
        self.headers = {'Content-Type': 'application/json'}
        self.markets = self.get_markets()
        self._loop_public = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=[self._loop_public])
        self.orderbook = {}
        self.taker_fee = 0.0007

    def get_markets(self):
        way = "https://api.hitbtc.com/api/3/public/symbol"
        resp = requests.get(url=way, headers=self.headers).json()
        markets = {}
        for symbol, market in resp.items():
            if market['type'] == 'futures' and market['status'] == 'working':
                markets.update({market['underlying']: symbol})
        return markets

    def get_all_tops(self):
        tops = {}
        for symbol, orderbook in self.orderbook.items():
            coin = symbol.upper().split('USD')[0]
            if len(orderbook['bids']) and len(orderbook['asks']):
                tops.update({self.EXCHANGE_NAME + '__' + coin: {
                    'top_bid': orderbook['bids'][0][0], 'top_ask': orderbook['asks'][0][0],
                    'bid_vol': orderbook['bids'][0][1], 'ask_vol': orderbook['asks'][0][1],
                    'ts_exchange': orderbook['timestamp']}})
        return tops

    def _run_ws_forever(self, loop):
        while True:
            loop.run_until_complete(self._run_ws_loop())

    async def _run_ws_loop(self):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(self.PUBLIC_WS_ENDPOINT) as ws:
                self._connected.set()
                self._ws_public = ws
                self._loop_public.create_task(self.subscribe_orderbooks())
                async for msg in ws:
                    self.update_orderbook(json.loads(msg.data))

    def get_orderbook(self, symbol):
        return self.orderbook[symbol]

    async def subscribe_orderbooks(self):
        method = {
            "method": "subscribe",
            "ch": "orderbook/D5/100ms",
            "params": {"symbols": list(self.markets.values())},
            "id": '123'
        }
        await self._connected.wait()
        await self._ws_public.send_json(method)

    def update_orderbook(self, data):
        if not data.get('data'):
            return
        for market, orders in data['data'].items():
            if not self.orderbook.get(market):
                self.orderbook.update({market: {'asks': [], 'bids': []}})
            self.orderbook[market].update({'asks': [[float(x[0]), float(x[1])] for x in orders['a']],
                                           'bids': [[float(x[0]), float(x[1])] for x in orders['b']],
                                           'timestamp': datetime.utcnow().timestamp()})

    def run_updater(self):
        self.wst_public.daemon = True
        self.wst_public.start()



if __name__ == '__main__':
    client = HitbtcClient()
    # print(len(list(client.get_markets())))
    client.run_updater()
    while True:
        time.sleep(5)
        print(client.get_all_tops())

