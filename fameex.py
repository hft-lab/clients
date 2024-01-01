
import time
import aiohttp
import json
import requests
from datetime import datetime
import asyncio
import threading
import zlib
# from core.wrappers import try_exc_regular, try_exc_async


class FameexClient:
    PUBLIC_WS_ENDPOINT = 'wss://www.fameex.com/ws/swap-api/v2/orderbook'
    BASE_URL = 'https://api.fameex.com'
    EXCHANGE_NAME = 'FAMEEX'

    def __init__(self, keys=None, leverage=None, markets_list=[], max_pos_part=20):
        self.headers = {'Content-Type': 'application/json'}
        # self.markets = self.get_markets()
        self._loop_public = asyncio.new_event_loop()
        self._connected = asyncio.Event()
        self.wst_public = threading.Thread(target=self._run_ws_forever, args=[self._loop_public])
        self.requestLimit = 1200
        self.getting_ob = asyncio.Event()
        self.now_getting = ''
        self.orderbook = {}
        self.taker_fee = 0.0006

    # @try_exc_regular
    def get_markets(self):
        way = "https://api.bitmake.com/u/v1/base/symbols"
        resp = requests.get(url=way, headers=self.headers).json()
        markets = {}
        for market in resp:
            if market.get('contractConfig') and market['contractConfig']['isPerpetual']:
                markets.update({market['baseToken']: market['symbol']})
        return markets

#     @try_exc_regular
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

#     @try_exc_regular
    def _run_ws_forever(self, loop):
        while True:
            loop.run_until_complete(self._run_ws_loop())

#     @try_exc_async
    async def _run_ws_loop(self):
        path = '/swap-api/v2/tickers'
        async with aiohttp.ClientSession() as s:
            while True:
                await asyncio.sleep(0.01)
                async with s.get(url=self.BASE_URL + path, headers=self.headers) as resp:
                    data = await resp.json()
                    print(data)
                    # data = json.loads(self.decode_gzip_data(msg.data))
                    # self.update_orderbook(data)

        # example = [{'ticker_id': 'XLM-USDT', 'base_currency': 'XLM', 'quote_currency': 'USDT', 'last_price': '0.12992',
        #   'base_volume': '8160891.139', 'quote_volume': '1054094.12991651', 'bid': '0.12992', 'ask': '0.12994',
        #   'high': '0.13071', 'low': '0.12715', 'product_type': 'Perpetual', 'open_interest': '0', 'index_price': '0',
        #   'funding_rate': '0', 'next_funding_rate_timestam': 1704153600000},
        #  {'ticker_id': 'ETC-USDT', 'base_currency': 'ETC', 'quote_currency': 'USDT', 'last_price': '22.228',
        #   'base_volume': '342195.96', 'quote_volume': '7508802.33391', 'bid': '22.226', 'ask': '22.23', 'high': '22.353',
        #   'low': '21.303', 'product_type': 'Perpetual', 'open_interest': '0', 'index_price': '0', 'funding_rate': '0',
        #   'next_funding_rate_timestam': 1704153600000}]

    @staticmethod
#     @try_exc_regular
    def decode_gzip_data(data):
        # Decompress the Gzip data
        decompressed_data = zlib.decompress(data, 16 + zlib.MAX_WBITS)

        # Convert decompressed data from binary to a UTF-8 encoded string
        string_data = decompressed_data.decode('utf-8')
        return string_data

#     @try_exc_regular
    def update_orderbook(self, data):
        ob = data['d'][0]
        symbol = ob['s']
        for new_bid in ob['b']:
            res = self.orderbook[symbol]['bids']
            if res.get(new_bid[0]) and new_bid[1] == '0':
                del res[new_bid[0]]
            else:
                self.orderbook[symbol]['bids'][new_bid[0]] = new_bid[1]
        for new_ask in ob['a']:
            res = self.orderbook[symbol]['asks']
            if res.get(new_ask[0]) and new_ask[1] == '0':
                del res[new_ask[0]]
            else:
                self.orderbook[symbol]['asks'][new_ask[0]] = new_ask[1]
        self.orderbook[symbol]['timestamp'] = ob['t']

#     @try_exc_regular
    def update_orderbook_snapshot(self, data):
        ob = data['d'][0]
        symbol = ob['s']
        self.orderbook[symbol] = {'asks': {x[0]: x[1] for x in ob['a']},
                                  'bids': {x[0]: x[1] for x in ob['b']},
                                  'timestamp': ob['t']}

#     @try_exc_regular
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
    client = FameexClient()
    # print(client.get_markets())
    client.run_updater()
    while True:
        time.sleep(5)
        # print(client.get_all_tops())
