import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading
# from core.wrappers import try_exc_regular, try_exc_async
import hmac
import hashlib


class WhiteBitClient:
    PUBLIC_WS_ENDPOINT = 'wss://api.whitebit.com/ws'
    BASE_URL = 'https://globe.exchange'
    EXCHANGE_NAME = 'WHITEBIT'

    def __init__(self, keys=None, leverage=None, markets_list=[], max_pos_part=20):
        if keys:
            self.api_key = keys['API_KEY']
            self.api_secret = keys['API_SECRET']
        self.headers = {'Content-Type': 'application/json'}
        self.markets = self.get_markets()
        self._loop_public = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=[self._loop_public])
        self.requestLimit = 1200
        self.getting_ob = asyncio.Event()
        self.now_getting = ''
        self.orderbook = {}
        self.taker_fee = 0.0005

    # @try_exc_regular
    def get_markets(self):
        way = "https://whitebit.com/api/v4/public/markets"
        resp = requests.get(url=way, headers=self.headers).json()
        markets = {}
        for market in resp:
            if market['type'] == 'futures' and market['tradesEnabled']:
                markets.update({market['stock']: market['name']})
        return markets

#     @try_exc_regular
    def _run_ws_forever(self, loop):
        while True:
            loop.run_until_complete(self._run_ws_loop())

#     @try_exc_async
    async def _run_ws_loop(self):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(self.PUBLIC_WS_ENDPOINT) as ws:
                self._connected.set()
                self._ws_public = ws
                for market in self.markets.values():
                    await self._loop_public.create_task(self.subscribe_orderbooks(market))
                async for msg in ws:
                    data = json.loads(msg.data)
                    if data.get('params'):
                        if data['params'][2] == self.now_getting:
                            while self.getting_ob.is_set():
                                time.sleep(0.00001)
                        if data['params'][0]:
                            self.update_orderbook_snapshot(data)
                        else:
                            self.update_orderbook(data)

#     @try_exc_async
    async def subscribe_orderbooks(self, symbol):
        data = [symbol, 10, '0', True]
        method = {"id": 921841274981274,
                  "method": "depth_subscribe",
                  "params": data}
        await self._connected.wait()
        await self._ws_public.send_json(method)

        #     @try_exc_regular
    def get_all_tops(self):
        tops = {}
        for coin, symbol in self.markets.items():
            orderbook = self.get_orderbook(symbol)
            if orderbook and orderbook['bids'] and orderbook['asks']:
                tops.update({self.EXCHANGE_NAME + '__' + coin: {
                    'top_bid': orderbook['bids'][0][0], 'top_ask': orderbook['asks'][0][0],
                    'bid_vol': orderbook['bids'][0][1], 'ask_vol': orderbook['asks'][0][1],
                    'ts_exchange': orderbook['timestamp']*1000}})
        return tops

    # @try_exc_regular
    def update_orderbook(self, data):
        symbol = data['params'][2]
        for new_bid in data['params'][1].get('bids', []):
            res = self.orderbook[symbol]['bids']
            if res.get(new_bid[0]) and new_bid[1] == '0':
                del res[new_bid[0]]
            else:
                self.orderbook[symbol]['bids'][new_bid[0]] = new_bid[1]
        for new_ask in data['params'][1].get('asks', []):
            res = self.orderbook[symbol]['asks']
            if res.get(new_ask[0]) and new_ask[1] == '0':
                del res[new_ask[0]]
            else:
                self.orderbook[symbol]['asks'][new_ask[0]] = new_ask[1]
        self.orderbook[symbol]['timestamp'] = data['params'][1]['timestamp']

    # @try_exc_regular
    def update_orderbook_snapshot(self, data):
        symbol = data['params'][2]
        self.orderbook[symbol] = {'asks': {x[0]: x[1] for x in data['params'][1].get('asks', [])},
                                  'bids': {x[0]: x[1] for x in data['params'][1].get('bids', [])},
                                  'timestamp': data['params'][1]['timestamp']}

    # @try_exc_regular
    def get_orderbook(self, symbol) -> dict:
        if not self.orderbook.get(symbol):
            return {}
        self.getting_ob.set()
        self.now_getting = symbol
        snap = self.orderbook[symbol]
        ob = {'timestamp': self.orderbook[symbol]['timestamp'],
              'asks': [[float(x), float(snap['asks'][x])] for x in sorted(snap['asks']) if snap['asks'].get(x)],
              'bids': [[float(x), float(snap['bids'][x])] for x in sorted(snap['bids']) if snap['bids'].get(x)][::-1]}
        self.now_getting = ''
        self.getting_ob.clear()
        return ob

#     @try_exc_regular
    def run_updater(self):
        self.wst_public.daemon = True
        self.wst_public.start()


if __name__ == '__main__':
    client = WhiteBitClient()
    # print(client.get_markets())
    # print(len(client.get_markets()))
    client.run_updater()
    while True:
        time.sleep(5)
        print(client.get_all_tops())

