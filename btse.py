import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading
from core.wrappers import try_exc_regular, try_exc_async


class BtseClient:
    PUBLIC_WS_ENDPOINT = 'wss://ws.btse.com/ws/oss/futures'
    EXCHANGE_NAME = 'BTSE'

    def __init__(self, keys=None, leverage=None, markets_list=[], max_pos_part=20):
        self.headers = {'Content-Type': 'application/json'}
        self.markets = self.get_markets()
        self._loop_public = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=[self._loop_public])
        self.requestLimit = 1200
        self.orderbook = {}
        self.taker_fee = 0.0004

    @try_exc_regular
    def get_markets(self):
        way = "https://api.btse.com/futures/api/v2.1/market_summary"
        resp = requests.get(url=way, headers=self.headers).json()
        markets = {}
        for market in resp:
            if market['active'] and 'PFC' in market['symbol']:
                markets.update({market['base']: market['symbol']})
        return markets

    @try_exc_regular
    def get_all_tops(self):
        tops = {}
        for coin, symbol in self.markets.items():
            orderbook = self.get_orderbook(symbol)
            if len(orderbook['bids']) and len(orderbook['asks']):
                tops.update({self.EXCHANGE_NAME + '__' + coin: {
                    'top_bid': orderbook['bids'][0][0], 'top_ask': orderbook['asks'][0][0],
                    'bid_vol': orderbook['bids'][0][1], 'ask_vol': orderbook['asks'][0][1],
                    'ts_exchange': orderbook['timestamp']}})
        return tops

    @try_exc_regular
    def _run_ws_forever(self, loop):
        while True:
            loop.run_until_complete(self._run_ws_loop())

    @try_exc_async
    async def _run_ws_loop(self):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(self.PUBLIC_WS_ENDPOINT) as ws:
                self._connected.set()
                self._ws_public = ws
                self._loop_public.create_task(self.subscribe_orderbooks())
                async for msg in ws:
                    data = json.loads(msg.data)
                    if data.get('data') and data['data']['type'] == 'snapshot':
                        self.update_orderbook_snapshot(data)
                    elif data.get('data') and data['data']['type'] == 'delta':
                        self.update_orderbook(data)

    @try_exc_async
    async def subscribe_orderbooks(self):
        args = [f"update:{x}_0" for x in self.markets.values()]
        method = {"op": "subscribe",
                  "args": args}
        await self._connected.wait()
        await self._ws_public.send_json(method)

    @try_exc_regular
    def update_orderbook(self, data):
        symbol = data['data']['symbol']
        for new_bid in data['data']['bids']:
            res = self.orderbook[symbol]['bids']
            if res.get(new_bid[0]) and new_bid[1] == '0':
                del res[new_bid[0]]
            else:
                self.orderbook[symbol]['bids'][new_bid[0]] = new_bid[1]
        for new_ask in data['data']['asks']:
            res = self.orderbook[symbol]['asks']
            if res.get(new_ask[0]) and new_ask[1] == '0':
                del res[new_ask[0]]
            else:
                self.orderbook[symbol]['asks'][new_ask[0]] = new_ask[1]
        self.orderbook[symbol]['timestamp'] = data['data']['timestamp']

    @try_exc_regular
    def update_orderbook_snapshot(self, data):
        symbol = data['data']['symbol']
        self.orderbook[symbol] = {'asks': {x[0]: x[1] for x in data['data']['asks']},
                                  'bids': {x[0]: x[1] for x in data['data']['bids']},
                                  'timestamp': data['data']['timestamp']}

    @try_exc_regular

    def get_orderbook(self, symbol) -> dict:
        snap = self.orderbook[symbol]
        orderbook = {'timestamp': self.orderbook[symbol.upper()]['timestamp'],
                     'asks': [[float(x), float(snap['asks'][x])] for x in sorted(snap['asks']) if snap['asks'].get(x)],
                     'bids': [[float(x), float(snap['bids'][x])] for x in sorted(snap['bids']) if snap['bids'].get(x)][::-1]}
        return orderbook

    @try_exc_regular
    def run_updater(self):
        self.wst_public.daemon = True
        self.wst_public.start()


if __name__ == '__main__':
    client = BtseClient()
    # print(client.get_markets())
    # print(len(client.get_markets()))
    client.run_updater()
    while True:
        time.sleep(5)
        print(client.get_all_tops())

