import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading
from core.wrappers import try_exc_regular, try_exc_async


class OrangexClient:
    PUBLIC_WS_ENDPOINT = 'wss://api.orangex.com/ws/api/v1'
    BASE_URL = 'https://api.orangex.com/api/v1'
    EXCHANGE_NAME = 'ORANGEX'

    def __init__(self, keys=None, leverage=None, state='Bot', markets_list=[], max_pos_part=20):
        if keys:
            self.api_key = keys['API_KEY']
            self.api_secret = keys['API_SECRET']
        self.headers = {'Content-Type': 'application/json'}
        self.markets_list = markets_list
        self.markets = self.get_markets()
        self._loop_public = asyncio.new_event_loop()
        self.message_queue = asyncio.Queue(loop=self._loop_public)
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=[self._loop_public])
        self._wst_orderbooks = threading.Thread(target=self._process_ws_line)
        self.requestLimit = 1200
        self.getting_ob = asyncio.Event()
        self.now_getting = ''
        self.orderbook = {}
        self.taker_fee = 0.0005

    @try_exc_regular
    def get_markets(self):
        path = "/public/get_instruments"
        resp = requests.get(url=self.BASE_URL + path, headers=self.headers).json()
        markets = {}
        for market in resp['result']:
            if market['currency'] == 'PERPETUAL' and market['is_active'] and '1000' not in market['instrument_name']:
                markets.update({market['quote_currency']: market['instrument_name']})
        return markets
        # example = {'jsonrpc': '2.0', 'usIn': 1704140719190, 'usOut': 1704140719199, 'usDiff': 9, 'result': [
        #     {'instrId': 3, 'currency': 'PERPETUAL', 'newListing': False, 'base_currency': 'USDT', 'contract_size': '1',
        #      'creation_timestamp': '1669651200000', 'expiration_timestamp': '2114352000000',
        #      'instrument_name': '1INCH-USDT-PERPETUAL', 'show_name': '1INCHUSDT', 'is_active': True,
        #      'kind': 'perpetual', 'leverage': 25, 'maker_commission': '0.0002', 'taker_commission': '0.0005',
        #      'min_trade_amount': '1', 'option_type': 'init', 'quote_currency': '1INCH', 'strike': '0',
        #      'tick_size': '0.0001', 'instr_multiple': '1', 'order_price_low_rate': '0.5',
        #      'order_price_high_rate': '1.5', 'order_price_limit_type': 1, 'min_qty': '18', 'min_notional': '10',
        #      'support_trace_trade': False}]}

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
                await self._loop_public.create_task(self.subscribe_orderbooks())
                async for msg in ws:
                    await self.message_queue.put(msg)

    @try_exc_regular
    def _process_ws_line(self):
        # self._loop.create_task(self.process_messages())
        asyncio.run_coroutine_threadsafe(self.process_messages(), self._loop_public)

    @try_exc_async
    async def process_messages(self):
        while True:
            msg = await self.message_queue.get()
            if ',instrument_name:' in str(msg.data):
                parts = str(msg.data).split(',instrument_name:')
                data = json.loads(parts[0] + '}}}')
                symbol = parts[1].split('PERPETUAL')[0] + 'PERPETUAL'
                self.update_orderbook(data, symbol)

    @try_exc_async
    async def subscribe_orderbooks(self):
        method = {"jsonrpc": "2.0",
                  "id": 1,
                  "method": "/public/subscribe",
                  "params": {
                      "channels": ['book.' + x + '.raw' for x in self.markets_list if self.markets.get(x)]}}
        await self._connected.wait()
        await self._ws_public.send_json(method)

    @try_exc_regular
    def update_orderbook(self, data, symbol):
        ob = data['params']['data']
        if not self.orderbook.get(symbol):
            self.orderbook.update({symbol: {'asks': {}, 'bids': {}, 'timestamp': ob['timestamp']}})
        for new_bid in ob['bids']:
            res = self.orderbook[symbol]['bids']
            if res.get(new_bid[1]) and new_bid[0] == 'delete':
                del res[new_bid[1]]
            else:
                self.orderbook[symbol]['bids'][new_bid[1]] = new_bid[2]
        for new_ask in ob['asks']:
            res = self.orderbook[symbol]['asks']
            if res.get(new_ask[1]) and new_ask[0] == 'delete':
                del res[new_ask[1]]
            else:
                self.orderbook[symbol]['asks'][new_ask[1]] = new_ask[2]
        self.orderbook[symbol]['timestamp'] = ob['timestamp']
        self.orderbook[symbol]['ts_ms'] = time.time()
        # example = {"params": {"data":
        #                           {"timestamp": 1704141351600, "change_id": 34369840,
        #                            "asks": [["delete", "2.34060000", "0"],
        #                                     ["new", "2.35000000", "7167.00000000"]],
        #                            "bids": [["new", "2.33130000", "4591.00000000"]],
        #                            "instrument_name": "GAL-USDT-PERPETUAL"},
        #                       "channel": "book.GAL-USDT-PERPETUAL.raw"},
        #            "method": "subscription", "jsonrpc": "2.0"}

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
    client = OrangexClient()
    print(len(client.get_markets()))
    # print(client.get_markets())
    client.run_updater()
    while True:
        time.sleep(5)
        print(client.get_all_tops())
