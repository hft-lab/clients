import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading
import zlib
from core.wrappers import try_exc_regular, try_exc_async


class BitmakeClient:
    PUBLIC_WS_ENDPOINT = 'wss://ws.bitmake.com/t/v1/ws'
    EXCHANGE_NAME = 'BITMAKE'

    def __init__(self, keys=None, leverage=None, markets_list=[], max_pos_part=20):
        self.headers = {'Content-Type': 'application/json'}
        self.markets = self.get_markets()
        self._loop_public = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=[self._loop_public])
        self.requestLimit = 1200
        self.orderbook = {}
        self.taker_fee = 0.0006

    @try_exc_regular
    def get_markets(self):
        way = "https://api.bitmake.com/u/v1/base/symbols"
        resp = requests.get(url=way, headers=self.headers).json()
        markets = {}
        for market in resp:
            if market.get('contractConfig') and market['contractConfig']['isPerpetual']:
                markets.update({market['baseToken']: market['symbol']})
        return markets

    @try_exc_regular
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
                for symbol in self.markets.values():
                    self._loop_public.create_task(self.subscribe_orderbooks(symbol))
                async for msg in ws:
                    data = json.loads(self.decode_gzip_data(msg.data))
                    if data.get('d'):
                        self.update_orderbook(data)

    @staticmethod
    @try_exc_regular
    def decode_gzip_data(data):
        # Decompress the Gzip data
        decompressed_data = zlib.decompress(data, 16 + zlib.MAX_WBITS)

        # Convert decompressed data from binary to a UTF-8 encoded string
        string_data = decompressed_data.decode('utf-8')
        return string_data

    @try_exc_regular
    def get_orderbook(self, symbol):
        return self.orderbook[symbol]

    @try_exc_async
    async def subscribe_orderbooks(self, symbol):
        method = {"tp": "diffMergedDepth",
                  "e": "sub",
                  "ps": {"symbol": symbol,
                         "depthLevel": 2}}
        await self._connected.wait()
        await self._ws_public.send_json(method)

    @try_exc_regular
    def update_orderbook(self, data):
        for ob in data['d']:
            if data['f']:
                self.orderbook.update({ob['s']: {'asks': [[float(x[0]), float(x[1])] for x in ob['a']],
                                                 'bids': [[float(x[0]), float(x[1])] for x in ob['b']],
                                                 'timestamp': ob['t']}})
            else:
                for new_bid in ob['b']:
                    for old_bid in self.orderbook[ob['s']]['bids']:
                        if float(new_bid[0]) == old_bid[0] and new_bid[1] != '0':
                            ind = self.orderbook[ob['s']]['bids'].index(old_bid)
                            self.orderbook[ob['s']]['bids'][ind] = [float(new_bid[0]), float(new_bid[1])]
                        elif float(new_bid[0]) == old_bid[0] and new_bid[1] == '0':
                            self.orderbook[ob['s']]['bids'].remove(old_bid)
                for new_ask in ob['a']:
                    for old_ask in self.orderbook[ob['s']]['asks']:
                        if float(new_ask[0]) == old_ask[0] and new_ask[1] != '0':
                            ind = self.orderbook[ob['s']]['asks'].index(old_ask)
                            self.orderbook[ob['s']]['asks'][ind] = [float(new_ask[0]), float(new_ask[1])]
                        elif float(new_ask[0]) == old_ask[0] and new_ask[1] == '0':
                            self.orderbook[ob['s']]['asks'].remove(old_ask)

    @try_exc_regular
    def run_updater(self):
        self.wst_public.daemon = True
        self.wst_public.start()


# if __name__ == '__main__':
#     client = BitmakeClient()
#     # print(client.get_markets())
#     client.run_updater()
#     while True:
#         time.sleep(5)
#         print(client.get_all_tops())
