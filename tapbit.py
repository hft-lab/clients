import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading
from core.wrappers import try_exc_regular, try_exc_async


class TapbitClient:
    PUBLIC_WS_ENDPOINT = 'wss://ws-openapi.tapbit.com/stream/ws'
    BASE_URL = 'https://openapi.tapbit.com/swap/'
    EXCHANGE_NAME = 'TAPBIT'

    def __init__(self, keys=None, leverage=None, state='Bot', markets_list=[], max_pos_part=20):
        if keys:
            self.api_key = keys['API_KEY']
            self.api_secret = keys['API_SECRET']
        self.markets_list = markets_list
        self.headers = {'Content-Type': 'application/json'}
        self.markets = self.get_markets()
        self._loop_public = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=[self._loop_public])
        self._wst_orderbooks = threading.Thread(target=self._process_ws_line)
        self.message_queue = asyncio.Queue(loop=self._loop_public)
        self.requestLimit = 1200
        self.getting_ob = asyncio.Event()
        self.now_getting = ''
        self.orderbook = {}
        self.taker_fee = 0.0006

    @try_exc_regular
    def get_markets(self):
        path = "/api/usdt/instruments/list"
        resp = requests.get(url=self.BASE_URL + path, headers=self.headers).json()
        markets = {}
        for market in resp['data']:
            markets.update({market['contract_code'].split('-')[0]: market['contract_code']})
        return markets
        # example = {'code': 200, 'message': None, 'data': [
        #     {'contract_code': 'FTT-SWAP', 'multiplier': '1', 'min_amount': '1', 'max_amount': '10000',
        #      'min_price_change': '0.0001', 'price_precision': '4', 'leverages': '2,3,5,10,20', 'max_leverage': '20'},
        #     {'contract_code': 'ORDI-SWAP', 'multiplier': '0.1', 'min_amount': '1', 'max_amount': '15000',
        #      'min_price_change': '0.001', 'price_precision': '3', 'leverages': '2,3,5,10,20', 'max_leverage': '20'},
        #     ]}

    @try_exc_regular
    def _process_ws_line(self):
        # self._loop.create_task(self.process_messages())
        asyncio.run_coroutine_threadsafe(self.process_messages(), self._loop_public)

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
                asyncio.run_coroutine_threadsafe(self.subscribe_orderbooks(), self._loop_public)
                async for msg in ws:
                    if msg.data == 'ping':
                        await ws.pong(b'pong')
                        continue
                    await self.message_queue.put(msg)

    @try_exc_async
    async def process_messages(self):
        while True:
            msg = await self.message_queue.get()
            data = json.loads(msg.data)
            if data.get('action') == 'insert':
                self.update_orderbook_snapshot(data)
            elif data.get('action') == 'update':
                self.update_orderbook(data)

                    # example = [{"topic": "usdt/orderBook.1000FLOKI-SWAP", "action": "update", "data": [{
                    #         "bids": [["0.03616", "50768"], ["0.03614", "116165"]],
                    #         "asks": [["0.03621", "59245"], ["0.03622", "0"]],
                    #         "version": 26818052, "timestamp": 1704134381758}]},
                    #     {"topic": "usdt/orderBook.FTT-SWAP", "action": "insert", "data": [{
                    #        "bids": [["3.1095", "10"], ["3.1090", "31"]],
                    #        "asks": [["3.1158", "12"], ["3.1161", "15"]],
                    #        "version": 3775392, "timestamp": 1704134367804}]}]
    @try_exc_async
    async def subscribe_orderbooks(self):
        met = {"op": "subscribe",
               "args": ['usdt/orderBook.' + self.markets[x] + '.10' for x in self.markets_list if self.markets.get(x)]}
        await self._connected.wait()
        await self._ws_public.send_json(met)

    @try_exc_regular
    def update_orderbook(self, data):
        ob = data['data'][0]
        symbol = data['topic'].split('.')[1]
        for new_bid in ob['bids']:
            res = self.orderbook[symbol]['bids']
            if res.get(new_bid[0]) and new_bid[1] == '0':
                del res[new_bid[0]]
            else:
                self.orderbook[symbol]['bids'][new_bid[0]] = new_bid[1]
        for new_ask in ob['asks']:
            res = self.orderbook[symbol]['asks']
            if res.get(new_ask[0]) and new_ask[1] == '0':
                del res[new_ask[0]]
            else:
                self.orderbook[symbol]['asks'][new_ask[0]] = new_ask[1]
        self.orderbook[symbol]['timestamp'] = ob['timestamp']
        self.orderbook[symbol]['ts_ms'] = time.time()

    @try_exc_regular
    def update_orderbook_snapshot(self, data):
        ob = data['data'][0]
        symbol = data['topic'].split('.')[1]
        self.orderbook[symbol] = {'asks': {x[0]: x[1] for x in ob['asks']},
                                  'bids': {x[0]: x[1] for x in ob['bids']},
                                  'timestamp': ob['timestamp'],
                                  'ts_ms': time.time()}

    @try_exc_regular
    def get_orderbook(self, symbol) -> dict:
        if not self.orderbook.get(symbol):
            return {}
        self.getting_ob.set()
        self.now_getting = symbol
        snap = self.orderbook[symbol]
        ob = {'timestamp': snap['timestamp'],
              'asks': [[float(x), float(snap['asks'][x])] for x in sorted(snap['asks']) if snap['asks'].get(x)],
              'bids': [[float(x), float(snap['bids'][x])] for x in sorted(snap['bids']) if snap['bids'].get(x)][::-1],
              'ts_ms': snap['ts_ms']}
        self.now_getting = ''
        self.getting_ob.clear()
        return ob

    @try_exc_regular
    def run_updater(self):
        self.wst_public.daemon = True
        self.wst_public.start()
        self._wst_orderbooks.daemon = True
        self._wst_orderbooks.start()


if __name__ == '__main__':
    client = TapbitClient()
    # print(len(client.get_markets()))
    client.run_updater()
    while True:
        time.sleep(5)
        print(client.get_all_tops())
